"""Stage-2 calibration test: launch a game, force-foreground the window,
sample screenshots at fixed intervals so we can see exactly what menu
state the recorded menu_skip must start from (sets settle_seconds).

Output: out/auto_nav_calibration/<scenario>/<run>/NN_tNNNs.png

Usage:
    python scripts/calibration/test_auto_launch_calibrate.py --scenario brotato --interval 3 --duration 45
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import psutil
import win32gui
import win32process

from coding4game_gui.benchmark.windows import save_state as ss
from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.startup import enforce_live_window
from coding4game_gui.benchmark.windows.startup import prepare_launch_state
from coding4game_gui.benchmark.windows.drivers.registry import make_env
from coding4game_gui.benchmark.windows.games_config import load_game_config
from coding4game_gui.benchmark.windows.prelaunch import _close_quietly
from coding4game_gui.benchmark.windows.prelaunch import env_pad_kwargs
from coding4game_gui.benchmark.windows.prelaunch import launch_with_pads
from coding4game_gui.benchmark.windows.auto_nav import wait_for_game_hwnd
from coding4game_gui.benchmark.windows.auto_nav import wait_for_stable_game_window
from coding4game_gui.benchmark.windows.auto_nav import force_foreground


def _find_game_hwnd(process_name: str):
    pids = {
        p.info["pid"] for p in psutil.process_iter(["pid", "name"])
        if (p.info.get("name") or "").lower() == process_name.lower()
    }
    found = []
    def cb(hwnd, _):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in pids and win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title:
                found.append((hwnd, title))
        return True
    win32gui.EnumWindows(cb, None)
    return found[0] if found else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", help="Windows benchmark scenario slug")
    ap.add_argument("--game", help="deprecated alias for --scenario")
    ap.add_argument("--interval", type=float, default=4.0,
                    help="Seconds between screenshots (default 4).")
    ap.add_argument("--duration", type=float, default=60.0,
                    help="Total sampling duration (default 60).")
    ap.add_argument("--out-dir", type=Path,
                    help="Fresh directory for this calibration's evidence.")
    ap.add_argument("--native-geometry", action="store_true",
                    help="Observe startup without resizing or toggling the native window.")
    args = ap.parse_args()
    if args.interval <= 0 or args.duration < args.interval:
        ap.error("duration must be at least one positive interval")
    if args.scenario and args.game and args.scenario != args.game:
        ap.error("--scenario and legacy --game disagree")
    scenario = args.scenario or args.game
    if not scenario:
        ap.error("--scenario is required")

    spec = load_game_spec(scenario)
    cfg = load_game_config(spec["driver_game"])
    al = cfg.auto_launch
    if al is None or not al.launch_path:
        sys.exit(
            f"driver YAML '{spec['driver_game']}' has no auto_launch.launch_path.")

    if any((p.info.get("name") or "").lower() == cfg.process.lower()
           for p in psutil.process_iter(["name"])):
        print(f"NOTE: {cfg.process} already running — close it and rerun.")
        return 2

    out_dir = args.out_dir or (ROOT / "out" / "auto_nav_calibration" / scenario /
                               datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    out_dir.mkdir(parents=True, exist_ok=False)
    rows: list[str] = []

    prepare_launch_state(spec, cfg)

    # Pads before the launch, then the launch, then the window wait — all owned
    # by prelaunch.launch_with_pads. This tool only takes screenshots, but it
    # still creates pads, and a pad this game never enumerated changes what the
    # settle it is calibrating actually looks like.
    n_players = 1 + max((step.pad for step in cfg.menu_skip), default=0)
    print(f"[1] Opening {al.launch_path}")
    try:
        prepared = launch_with_pads(
            al, cfg.process, n_pads=n_players, attempts=1,
            platform_name=cfg.platform or "auto",
            window_ok=wait_for_stable_game_window,
        )
    except RuntimeError as exc:
        ss.kill_process(cfg.process)
        print(f"[FAIL] {exc}")
        return 1
    pads = prepared.pads

    # Find game hwnd + force it to foreground so dxcam captures the game,
    # not whatever's on top of it.
    try:
        # A Steam bootstrap may replace its initial process/window. Match the
        # formal launch path's survival window before attaching the renderer.
        time.sleep(10.0)
        hit = wait_for_game_hwnd(cfg.process, timeout=10.0)
        if hit is None:
            print("[FAIL] couldn't locate game hwnd")
            _close_quietly(pads)
            ss.kill_process(cfg.process)
            return 1
        hwnd, title = hit
        print(f"[3] Game hwnd={hwnd} title={title!r}; forcing to foreground")
        if args.native_geometry:
            print("    native geometry observation; presentation setup is not applied")
        else:
            enforce_live_window(spec, cfg, hwnd)
        force_foreground(hwnd)
        time.sleep(0.5)

        env = make_env(
            cfg.platform or "auto", cfg.process, env_fps=10,
            screenshot_backend=(None if (cfg.screenshot_backend or "auto")
                                == "auto" else cfg.screenshot_backend),
            use_speedhack=False, n_players=n_players,
            **env_pad_kwargs(pads),
        )
    except BaseException:
        _close_quietly(pads)
        ss.kill_process(cfg.process)
        raise
    print(f"    backend={env.screenshot_backend_name} "
          f"window={env.window_width}x{env.window_height}")
    # env's __init__ called activate() — it may have grabbed focus to itself
    # via pwc, but our terminal output is sneaking focus back. Re-force here
    # before each sample below.

    interval = args.interval
    duration = args.duration
    n = int(duration / interval)
    print(f"[4] Sampling {n} screenshots @ {interval}s intervals...")

    import asyncio
    async def snap():
        started = time.perf_counter()
        for i in range(n):
            t = (i + 1) * interval
            await asyncio.sleep(max(0.0, started + t - time.perf_counter()))
            # Re-force foreground each cycle — dxcam grabs the SCREEN at
            # game's window rect, so anything occluding it will get
            # captured instead.
            force_foreground(hwnd)
            await asyncio.sleep(0.15)
            pil = await env.render_locked()
            path = out_dir / f"{i:02d}_t{int(t):03d}s.png"
            pil.save(path)
            rows.append(f"{i:02d}  t={int(t):3d}s  -> {path.name}")
            print(f"    [{i + 1}/{n}] {path.name}")
    try:
        asyncio.run(snap())
        print(f"[5] Wrote {len(rows)} screenshots to {out_dir}")
    finally:
        (out_dir / "manifest.txt").write_text("\n".join(rows), encoding="utf-8")
        try:
            env.close()
        finally:
            print(">>> Killing game.")
            ss.kill_process(cfg.process)
    return 0


if __name__ == "__main__":
    sys.exit(main())
