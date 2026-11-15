"""Verify the shared scenario startup and capture its frozen handoff without models."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from coding4game_gui.benchmark.windows import save_state as ss
from coding4game_gui.benchmark.windows.auto_nav import start_auto_launch_session
from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.env import ResetController
from coding4game_gui.benchmark.windows.startup import enforce_live_window
from coding4game_gui.benchmark.windows.startup import prepare_launch_state
from coding4game_gui.benchmark.windows.games_config import load_game_config


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", help="Windows benchmark scenario slug")
    ap.add_argument("--game", help="deprecated alias for --scenario")
    ap.add_argument("--out-dir", type=Path,
                    help="Fresh directory for this verification's evidence.")
    args = ap.parse_args()
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

    out_dir = args.out_dir or (ROOT / "out" / "auto_launch_full_test" / scenario /
                               datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    out_dir.mkdir(parents=True, exist_ok=False)

    t_launch = time.perf_counter()
    env = None
    controller = ResetController(spec)
    report = {"scenario": scenario, "driver_game": spec["driver_game"],
              "status": "running", "paid_model_calls": 0,
              "start_check_declared": bool(spec.get("startup", {}).get("start_check"))}

    def capture_post_settle(session_env, hwnd):
        if not controller._force_foreground(session_env):
            raise RuntimeError("could not focus game before startup capture")
        time.sleep(0.5)
        pil = asyncio.run(session_env.render_locked())
        p_pre = out_dir / "01_post_settle.png"
        pil.save(p_pre)
        print(f"    saved {p_pre.name} ({pil.size[0]}x{pil.size[1]})")

    try:
        env, hwnd, _title = start_auto_launch_session(
            cfg,
            before_launch=lambda: prepare_launch_state(spec, cfg),
            after_window=lambda live_hwnd: enforce_live_window(
                spec, cfg, live_hwnd),
            post_settle=capture_post_settle,
            menu_skip_replayer=controller.replay_menu_skip,
            platform_name=controller.platform,
            env_fps=controller.tics_per_second,
            use_speedhack=bool(getattr(cfg, "use_speedhack", False)),
            require_menu_skip=True,
        )
        before_check = asyncio.run(env.render_locked())
        before_check.save(out_dir / "02_before_start_check.png")
        if not controller.prepare_policy_handoff(env):
            raise RuntimeError("start_check failed after menu_skip; policy handoff is not ready")
        # The validated game remains frozen. Capture without refocusing it.
        pil = asyncio.run(env.render_locked())
        handoff = out_dir / "02_after_menu_skip.png"
        pil.save(handoff)
        print(f"\n    saved {handoff.name}")

        elapsed = time.perf_counter() - t_launch
        report.update(status="completed_pending_visual_review", elapsed_seconds=elapsed)
        print(f"\n[6] Total elapsed: {elapsed:.1f}s")
        print(f"    Screenshots: {out_dir}")
        return 0
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        try:
            if env is not None:
                pil = asyncio.run(env.render_locked())
            else:
                from PIL import ImageGrab
                pil = ImageGrab.grab()
            pil.save(out_dir / "failure.png")
        except Exception as capture_exc:
            report["failure_capture_error"] = str(capture_exc)
        print(f"[FAIL] {exc}")
        return 1
    finally:
        try:
            if env is not None:
                try:
                    env.unpause()
                finally:
                    env.close()
        finally:
            print("\n>>> Killing game.")
            report["killed_processes"] = ss.kill_process(cfg.process)
            (out_dir / "result.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
