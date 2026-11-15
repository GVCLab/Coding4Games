"""Stage-1 dry test for the auto_launch path.

Launches the target game directly via its YAML's auto_launch.launch_path,
waits for the game window, prints what it sees, then KILLS the game before
any unverified pad sequence (menu_skip) would run.

Goal: prove launch + window detection works end-to-end, WITHOUT risking
seconds of guessed pad timing pressing buttons into a game whose menu state
we haven't observed yet.

Usage:
    python scripts/calibration/test_auto_launch_dry.py --game brotato
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import psutil

from coding4game_gui.benchmark.windows import save_state as ss
from coding4game_gui.benchmark.windows.games_config import load_game_config


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--game", required=True,
                   help="YAML game name (without .yaml). Must have auto_launch:")
    p.add_argument("--out-dir", type=Path,
                   help="Fresh evidence directory; never overwrites an earlier test.")
    args = p.parse_args()
    from coding4game_gui.benchmark.windows.drivers.windows_xinput.lifecycle import set_dpi_awareness
    set_dpi_awareness()

    # A launch-only probe neither reads nor restores task checkpoints.
    cfg = load_game_config(args.game, validate_files=False)
    al = cfg.auto_launch
    if al is None or not al.launch_path:
        sys.exit(f"YAML '{args.game}' has no auto_launch.launch_path.")

    already = any(
        (p.info.get("name") or "").lower() == cfg.process.lower()
        for p in psutil.process_iter(["name"])
    )
    if already:
        print(f"NOTE: {cfg.process} already running; will NOT kill it. "
              "Close it manually and rerun for a clean test.")
        return 2

    out = args.out_dir or (ROOT / "out" / "auto_launch_dry" / args.game /
                           datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    out.mkdir(parents=True, exist_ok=False)
    from PIL import ImageGrab
    report = {"game": args.game, "process": cfg.process,
              "launch_path": al.launch_path, "launch_only": True,
              "window_found": False}

    try:
        code = _probe(cfg, out, report)
        report["returncode"] = code
        return code
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            ImageGrab.grab().save(out / "desktop.png")
        except Exception as exc:
            report["capture_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            report["killed_processes"] = ss.kill_process(cfg.process)
            (out / "result.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8")
            print(f"Evidence: {out}")


def _probe(cfg, out: Path, report: dict) -> int:
    al = cfg.auto_launch

    print(f"[1/4] Opening {al.launch_path}")
    t0 = time.perf_counter()
    from coding4game_gui.benchmark.windows.game_launch import launch_game
    launch_game(al)

    print(f"[2/4] Waiting up to {al.window_wait_seconds:.0f}s for "
          f"{cfg.process} window...")
    from coding4game_gui.benchmark.windows.auto_nav import wait_for_stable_game_window
    ok = wait_for_stable_game_window(cfg.process, timeout=al.window_wait_seconds)
    elapsed = time.perf_counter() - t0
    if not ok:
        print(f"[FAIL] no window after {elapsed:.0f}s")
        return 1
    report["window_found"] = True
    report["window_wait_seconds"] = elapsed
    print(f"[3/4] Window appeared after {elapsed:.0f}s")

    # Settle 8 seconds (much shorter than the 25s YAML default — enough to
    # see whether the game's actually rendering, not enough to finish
    # intros).
    time.sleep(8.0)

    # Inspect what we see.
    import win32gui
    import win32process

    found = []
    target_pids = {
        p.info["pid"] for p in psutil.process_iter(["pid", "name"])
        if (p.info.get("name") or "").lower() == cfg.process.lower()
    }

    def enum_cb(hwnd, _):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in target_pids:
            title = win32gui.GetWindowText(hwnd)
            if title and win32gui.IsWindowVisible(hwnd):
                rect = win32gui.GetWindowRect(hwnd)
                found.append((hwnd, title, rect))
        return True

    win32gui.EnumWindows(enum_cb, None)
    print(f"[4/4] Found {len(found)} visible window(s) for {cfg.process}:")
    report["windows"] = []
    for hwnd, title, rect in found:
        l, t, r, b = rect
        print(f"  hwnd={hwnd} title={title!r} rect={l},{t} {r - l}x{b - t}")
        report["windows"].append({"hwnd": hwnd, "title": title,
                                  "rect": list(rect)})
    if found:
        from coding4game_gui.benchmark.windows.auto_nav import force_foreground
        report["foreground"] = force_foreground(found[0][0])
        time.sleep(0.5)

    print()
    print(">>> Killing the game now (this was a dry test).")
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
