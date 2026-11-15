"""Token-free, real-game smoke gate for any Windows benchmark scenario.

The smoke checks freeze containment and repeated serial cold starts using a
safe action (``NEUTRAL`` by default). Parameterized analog controls use the
same raw-action validation as production. It validates infrastructure; a human must
still confirm that representative gameplay actions have their intended effect.

Run::

    python -m coding4game_gui.benchmark.windows.diagnostics.smoke --scenario brotato
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.env import WindowsGameEnv
from coding4game_gui.benchmark.windows.raw_actions import normalize_windows_input_mode, resolve_raw_action
from coding4game_gui.benchmark.windows.timing import resolve_max_action_tics, validate_action_tics
from coding4game_gui.paths import REPO_ROOT


def frame_delta(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16))))


def _phase(env: WindowsGameEnv) -> str | None:
    classify = getattr(getattr(env, "_scorer", None), "classify_phase", None)
    return classify(env.frame()) if classify is not None else None


def _capture_frozen_probe(env: WindowsGameEnv) -> tuple[np.ndarray, dict]:
    """Read visible desktop pixels without asking the suspended game to paint."""
    if not env._env._suspended:
        raise RuntimeError("freeze probe requires a hard-suspended game")
    import win32api
    import win32con
    import win32gui
    from PIL import ImageGrab
    from coding4game_gui.benchmark.windows.auto_nav import find_game_hwnd

    hit = find_game_hwnd(env._controller.cfg.process)
    if hit is None:
        raise RuntimeError("freeze probe cannot locate the game window")
    hwnd = hit[0]

    def owned(handle):
        return handle == hwnd or bool(win32gui.IsChild(hwnd, handle))

    if not owned(win32gui.GetForegroundWindow()):
        raise RuntimeError("freeze probe requires the game in the foreground")
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (left, top))
    right, bottom = win32gui.ClientToScreen(hwnd, (right, bottom))
    client = (left, top, right, bottom)
    monitor = win32api.GetMonitorInfo(win32api.MonitorFromWindow(
        hwnd, win32con.MONITOR_DEFAULTTONEAREST))["Monitor"]
    box = (max(left, monitor[0]), max(top, monitor[1]),
           min(right, monitor[2]), min(bottom, monitor[3]))
    left, top, right, bottom = box
    if right <= left or bottom <= top:
        raise RuntimeError("freeze probe has no visible game client pixels")
    # Check the sampled surface without focusing or messaging the stopped HWND.
    # This is a best-effort overlay guard, not a proof against every occlusion.
    for x in (left, (left + right) // 2, right - 1):
        for y in (top, (top + bottom) // 2, bottom - 1):
            observed = win32gui.WindowFromPoint((x, y))
            if not owned(observed):
                raise RuntimeError(
                    f"freeze probe game surface is covered at {(x, y)}; "
                    f"game HWND={hwnd}, observed HWND={observed}, "
                    f"class={win32gui.GetClassName(observed)!r}")
    frame = ImageGrab.grab(bbox=box, all_screens=True,
                           include_layered_windows=True).convert("RGB")
    return np.asarray(frame).copy(), {
        "method": "desktop_client_intersection",
        "client_box": list(client),
        "captured_box": list(box),
        "partial_client": box != client,
    }


def run_smoke(*, scenario: str, episodes: int = 3,
              max_tics: int | None = None, action: str = "NEUTRAL",
              probe_gap_seconds: float = 5.0,
              windows_input: str = "gamepad",
              evidence_dir: Path | None = None,
              action_tics: int | None = None,
              tail_tics: int = 0,
              skip_freeze_probe: bool = False) -> dict:
    if episodes <= 0:
        raise ValueError("episodes must be positive")
    if max_tics is not None and max_tics <= 0:
        raise ValueError("max_tics must be positive")
    if probe_gap_seconds < 0:
        raise ValueError("probe_gap_seconds must be non-negative")
    if action_tics is not None and not 1 <= action_tics <= 1000:
        raise ValueError("action_tics must be between 1 and 1000")
    if tail_tics < 0:
        raise ValueError("tail_tics must be non-negative")

    input_mode = normalize_windows_input_mode(windows_input)
    spec = load_game_spec(scenario, windows_input=input_mode)
    available = {row["name"] for row in spec.get("action_catalog", [])}
    action = action.strip().upper()
    if input_mode == "gamepad-kbm" or action not in available:
        try:
            resolve_raw_action([action], {}, input_mode=input_mode,
                               content_size=(1280, 768))
        except ValueError:
            if input_mode == "gamepad-kbm":
                raise
        else:
            available.add(action)
    if action not in available:
        raise ValueError(
            f"action {action!r} is not declared for {scenario!r}; "
            f"available: {sorted(available)}")
    tics_per_second = int(spec["runtime"]["tics_per_second"])
    episode_cap = max_tics or 60 * tics_per_second
    action_cap = resolve_max_action_tics(spec.get("runtime"))
    probe_tics = min(action_cap, max(1, tics_per_second // 2))
    action_tics = min(action_cap, tics_per_second) if action_tics is None else action_tics
    validate_action_tics(action_tics, action_cap)

    report = {
        "scenario": scenario,
        "driver_game": spec["driver_game"],
        "action": action,
        "windows_input": input_mode,
        "tics_per_second": tics_per_second,
        "max_tics": episode_cap,
        "terminal_detection": (spec.get("lifecycle") or {}).get(
            "terminal_detection", True),
        "action_tics": action_tics,
        "max_action_tics": action_cap,
        "tail_tics": tail_tics,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "freeze_probe": {},
        "episodes": [],
    }
    if evidence_dir is not None:
        evidence_dir = Path(evidence_dir)
        evidence_dir.mkdir(parents=True, exist_ok=False)

    def capture(label, frame, *, rgb=False):
        if evidence_dir is not None:
            Image.fromarray(frame if rgb else frame[:, :, ::-1]).save(
                evidence_dir / f"{label}.png")

    construction_started = time.perf_counter()
    env = WindowsGameEnv(scenario, windows_input=input_mode)
    construction_s = time.perf_counter() - construction_started
    report["environment_setup_s"] = construction_s
    try:
        if skip_freeze_probe:
            report["freeze_probe"] = {"skipped": True, "consumed_tics": 0}
            print("== freeze-leak probe explicitly skipped ==")
        else:
            print("== freeze-leak probe ==")
            env.reset(episode_id=-1)
            capture("probe_start", env.frame())
            env.act([action], probe_tics)
            f1 = env.frame().copy()
            # Cached render() cannot prove a freeze. PrintWindow also cannot run
            # against a suspended message loop; use independent desktop capture.
            frozen_before, probe_capture = _capture_frozen_probe(env)
            time.sleep(probe_gap_seconds)
            frozen_after, later_capture = _capture_frozen_probe(env)
            if probe_capture != later_capture:
                raise RuntimeError("freeze probe game geometry changed during the gap")
            capture("probe_frozen_before", frozen_before, rgb=True)
            capture("probe_frozen_after", frozen_after, rgb=True)
            env.act([action], 1)
            f2 = env.frame().copy()
            env.act([action], probe_tics)
            f3 = env.frame().copy()
            frozen_delta = frame_delta(f1, f2)
            active_delta = frame_delta(f2, f3)
            report["freeze_probe"] = {
                "capture": probe_capture,
                "gap_seconds": probe_gap_seconds,
                "frozen_gap_delta": frame_delta(frozen_before, frozen_after),
                "frozen_gap_plus_one_tic_delta": frozen_delta,
                "active_delta": active_delta,
                "consumed_tics": env.tic,
            }
            capture("probe_active_after", f3)
            print(f"delta across pure frozen gap: {report['freeze_probe']['frozen_gap_delta']:.2f}")
            print(f"delta across {probe_gap_seconds:g}s FROZEN gap + 1 tic: "
                  f"{frozen_delta:.2f}")
            print(f"delta across a normal {probe_tics}-tic act: "
                  f"{active_delta:.2f}")
            env.finish_episode()

        for ep in range(episodes):
            t0 = time.perf_counter()
            env.reset(episode_id=ep)
            capture(f"episode_{ep}_start", env.frame())
            cold_start_s = time.perf_counter() - t0
            if ep == 0 and skip_freeze_probe:
                # The constructor already performs the initial cold launch.
                cold_start_s += construction_s
            print(f"== episode {ep}: cold start took {cold_start_s:.1f}s ==")
            acts = 0
            while not env.done and env.tic < episode_cap:
                remaining = episode_cap - env.tic
                if tail_tics and remaining > tail_tics:
                    tics = min(action_tics, remaining - tail_tics)
                elif tail_tics:
                    tics = min(tics_per_second, remaining, action_cap)
                else:
                    tics = min(action_tics, remaining)
                reward = env.act([action], tics)
                acts += 1
                if evidence_dir is not None:
                    label = f"episode_{ep}_tic_{env.tic:05d}"
                    capture(label, env.frame())
                    # Preserve observations as they arrive, including failures
                    # before the final report. The game stays frozen throughout.
                    with (evidence_dir / "observations.jsonl").open(
                            "a", encoding="utf-8") as stream:
                        stream.write(json.dumps({
                            "episode": ep, "act": acts, "tic": env.tic,
                            "done": env.done, "score": None,
                            "file": f"{label}.png",
                        }) + "\n")
                print(f"  tic {env.tic:4d} act={[action]} score=pending")
            env.finalize_score()
            capture(f"episode_{ep}_end", env.frame())
            settled_phase = _phase(env)
            settlement = None
            save_settlement = getattr(env, "save_score_evidence", None)
            if evidence_dir is not None and callable(save_settlement):
                settlement = save_settlement(
                    evidence_dir / f"episode_{ep}_settlement")
            cap_frozen_surface = None
            if evidence_dir is not None and not env.done and env.tic >= episode_cap:
                # Observe the stopped desktop independently after settlement.
                # This does not resume the target or change its settled score.
                try:
                    surface, meta = _capture_frozen_probe(env)
                    capture(f"episode_{ep}_cap_desktop", surface, rgb=True)
                    cached = env.frame()[:, :, ::-1]
                    cap_frozen_surface = {
                        "capture": meta,
                        "cached_vs_desktop_delta": (
                            frame_delta(cached, surface)
                            if cached.shape == surface.shape else None),
                        "consumed_tics": env.tic,
                    }
                except Exception as exc:
                    cap_frozen_surface = {"capture_error": str(exc),
                                          "consumed_tics": env.tic}
            row = {
                "episode": ep,
                "cold_start_s": cold_start_s,
                "done": env.done,
                "tic": env.tic,
                "score": env.total_reward,
                "settled_phase": settled_phase,
                "settlement": settlement,
                "end_reason": env.end_reason or "smoke_max_tics",
                "timing_warnings": env._timing_warned,
                "acts": acts,
                "cap_frozen_surface": cap_frozen_surface,
            }
            report["episodes"].append(row)
            score_text = "null" if row["score"] is None else f"{row['score']:.0f}"
            print(f"  episode {ep}: done={row['done']} tic={row['tic']} "
                  f"score={score_text} end={row['end_reason']}")
            env.finish_episode()
    except Exception as exc:
        if evidence_dir is not None:
            report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            report["in_progress_episode"] = {
                "episode": getattr(env, "_episode_id", None),
                "consumed_tics": getattr(env, "tic", None),
                "done": getattr(env, "done", None),
                "end_reason": getattr(env, "end_reason", None),
            }
            try:
                from PIL import ImageGrab
                ImageGrab.grab(all_screens=True).save(
                    evidence_dir / "failure_desktop.png")
            except Exception as capture_exc:
                report["failure_capture_error"] = str(capture_exc)
            (evidence_dir / "partial_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    finally:
        env.close()

    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True,
                    help="Windows benchmark scenario slug")
    ap.add_argument("--episodes", type=int, default=3)
    ap.add_argument("--max-tics", type=int,
                    help="per-episode smoke cap; default: 60 real seconds")
    ap.add_argument("--action", default="NEUTRAL",
                    help="declared safe action token (default: NEUTRAL)")
    ap.add_argument("--windows-input", choices=["gamepad", "gamepad-kbm"],
                    default="gamepad", help="Production input protocol to test.")
    ap.add_argument("--probe-gap-seconds", type=float, default=5.0)
    ap.add_argument("--action-tics", type=int,
                    help="Tics per ordinary diagnostic ACT; default: one second.")
    ap.add_argument("--tail-tics", type=int, default=0,
                    help="Use one-second ACTs for this final tic window.")
    ap.add_argument("--skip-freeze-probe", action="store_true",
                    help="Skip the separate probe and record it as untested here.")
    ap.add_argument("--out", type=Path,
                    help="report JSON path; default: out/windows_smoke/<scenario>.json")
    ap.add_argument("--evidence-dir", type=Path,
                    help="Fresh directory for frozen and episode milestone images.")
    args = ap.parse_args()
    try:
        report = run_smoke(
            scenario=args.scenario,
            episodes=args.episodes,
            max_tics=args.max_tics,
            action=args.action,
            probe_gap_seconds=args.probe_gap_seconds,
            windows_input=args.windows_input,
            evidence_dir=args.evidence_dir,
            action_tics=args.action_tics,
            tail_tics=args.tail_tics,
            skip_freeze_probe=args.skip_freeze_probe,
        )
    except ValueError as exc:
        ap.error(str(exc))

    root = REPO_ROOT
    out = args.out or root / "out" / "windows_smoke" / f"{args.scenario}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"SMOKE COMPLETE - inspect readouts and report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
