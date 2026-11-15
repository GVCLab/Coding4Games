"""Record a human gameplay session for Windows scorer calibration.

The script performs the same cold-start chain as the Windows benchmark:
kill -> optional golden restore -> direct launch -> settle -> menu_skip. It then
unpauses the game for a human to play while saving screenshots and optional
semantic markers. Navigation may use the registered OCR checks; recording
does not run a scorer or an LLM.

The operator stops recording by creating the printed stop-request file.
Use --until-stopped for human sessions without an automatic duration stop;
this mode also leaves the game open after recording ends.
Use --start-file to keep the validated start frozen until operator release.
Closing the game also ends recording cleanly. F6-F11 are optional semantic
markers (``--marker F6=label``); F12 is left to the game's screenshot shortcut.
After human handoff, a recording failure releases capture/input resources but
does not terminate the game. The partial recording still fails and keeps its error.

Usage:
  python .agents/skills/add-new-game/scripts/record_scorer_session.py --scenario black_myth
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "coding4game_gui").is_dir() and (parent / "games").is_dir():
            return parent
    raise RuntimeError("could not locate the Coding4Game_GUI repository root")


ROOT = _repo_root()
sys.path.insert(0, str(ROOT))

from coding4game_gui.benchmark.windows import save_state as ss
from coding4game_gui.benchmark.windows.auto_nav import force_foreground
from coding4game_gui.benchmark.windows.auto_nav import start_auto_launch_session
from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.env import ResetController
from coding4game_gui.benchmark.windows.startup import enforce_live_window
from coding4game_gui.benchmark.windows.startup import prepare_launch_state
from coding4game_gui.benchmark.windows.games_config import load_game_config


FUNCTION_KEYS = {
    "F6": 0x75,
    "F7": 0x76,
    "F8": 0x77,
    "F9": 0x78,
    "F10": 0x79,
    "F11": 0x7A,
}
DEFAULT_MARKERS = {FUNCTION_KEYS[f"F{i}"]: f"state_{i - 5}"
                   for i in range(6, 11)}


def parse_markers(values: list[str] | None) -> dict[int, str]:
    """Parse repeated ``F6=label`` options into virtual-key labels."""
    if not values:
        return dict(DEFAULT_MARKERS)
    result: dict[int, str] = {}
    for raw in values:
        key, sep, label = raw.partition("=")
        key = key.strip().upper()
        label = label.strip()
        if not sep or key not in FUNCTION_KEYS or not label:
            raise ValueError(
                f"invalid marker {raw!r}; expected F6=label through F11=label")
        vk = FUNCTION_KEYS[key]
        if vk in result:
            raise ValueError(f"duplicate marker hotkey {key}")
        result[vk] = label
    return result


def marker_names(markers: dict[int, str]) -> dict[str, str]:
    by_vk = {vk: name for name, vk in FUNCTION_KEYS.items()}
    return {by_vk[vk]: label for vk, label in markers.items()}


def _pressed(vk: int) -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def _save_frame(img, frames_dir: Path, index: int) -> str:
    name = f"{index:06d}.png"
    # PIL image returned by the renderer is RGB; PNG preserves bar colours.
    img.save(frames_dir / name, format="PNG")
    return name


def _window_exists(hwnd: int) -> bool:
    is_window = ctypes.windll.user32.IsWindow
    is_window.argtypes = [ctypes.c_void_p]
    is_window.restype = ctypes.c_int
    return bool(is_window(hwnd))


def _record_frames(env, hwnd: int, frames_dir: Path, events, *, fps: float,
                   max_seconds: float | None, markers: dict[int, str],
                   stop_file: Path, meta: dict, on_first_frame=None,
                   on_progress=None) -> None:
    """Record until operator stop, normal window closure, or duration limit.

    Visibility guards create explicit capture gaps and keep the session alive.
    Other capture failures propagate. Only observed closure is a game exit.
    """
    start = time.perf_counter()
    next_frame = start
    next_progress = start
    interval = 1.0 / fps
    last_keys = {vk: False for vk in markers}
    frame_no = 0
    gap_started = None
    meta['frames'] = 0
    meta['last_frame_elapsed_seconds'] = 0.0
    events.write(json.dumps({"t": 0.0, "type": "start"}) + "\n")
    while True:
        now = time.perf_counter()
        elapsed = now - start
        if stop_file.exists():
            reason = "operator_stop_file"
            break
        if not _window_exists(hwnd):
            reason = "game_exit"
            break
        if max_seconds is not None and elapsed >= max_seconds:
            reason = "max_duration"
            break
        for vk, label in markers.items():
            pressed = _pressed(vk)
            if pressed and not last_keys[vk]:
                events.write(json.dumps({
                    "t": round(elapsed, 3), "type": "marker", "label": label,
                }, ensure_ascii=False) + "\n")
                print(f"[calib] marker {label} @ {elapsed:.2f}s", flush=True)
            last_keys[vk] = pressed
        if now >= next_frame:
            try:
                img = env.render()
            except Exception as exc:
                if not _window_exists(hwnd):
                    reason = "game_exit"
                    break
                visibility_pause = isinstance(exc, RuntimeError) and str(exc).startswith((
                    'visible capture requires the game foreground',
                    'desktop capture requires the game foreground',
                    'visible capture client is obscured at ',
                    'desktop capture client is obscured at ',
                    'desktop capture requires a non-minimized game window'))
                if visibility_pause:
                    if gap_started is None:
                        gap_started = time.perf_counter()
                        meta['capture_gaps'] = meta.get('capture_gaps', 0) + 1
                        meta['capture_status'] = 'waiting_for_visible_game'
                        events.write(json.dumps({
                            't': round(elapsed, 3), 'type': 'capture_gap_started',
                            'reason': 'game_not_visible'}) + '\n')
                        print('[calib] Capture paused: return to the visible game to resume.', flush=True)
                    if on_progress is not None and now >= next_progress:
                        on_progress()
                        next_progress = now + 5.0
                    next_frame = now + interval
                    time.sleep(.05)
                    continue
                raise
            if gap_started is not None:
                ended = time.perf_counter()
                events.write(json.dumps({
                    't': round(ended - start, 3), 'type': 'capture_gap_ended',
                    'duration_seconds': round(ended - gap_started, 3)}) + '\n')
                gap_started = None
                print('[calib] Capture resumed: the game is visible.', flush=True)
            meta['capture_status'] = 'recording'
            name = _save_frame(img, frames_dir, frame_no)
            events.write(json.dumps({
                "t": round(elapsed, 3), "type": "frame", "file": name,
            }) + "\n")
            frame_no += 1
            meta["frames"] = frame_no
            captured_at = time.perf_counter()
            meta["last_frame_elapsed_seconds"] = round(captured_at - start, 3)
            meta["last_frame_at"] = datetime.now().isoformat(timespec="seconds")
            if frame_no == 1 and on_first_frame is not None:
                on_first_frame()
            if on_progress is not None and captured_at >= next_progress:
                on_progress()
                next_progress = captured_at + 5.0
            next_frame += interval
            if next_frame < now:
                next_frame = now + interval
        time.sleep(0.005)
    meta["stop_reason"] = reason
    meta['capture_status'] = 'stopped'
    meta['capture_gap_active_at_stop'] = gap_started is not None
    events.write(json.dumps({
        "t": round(time.perf_counter() - start, 3), "type": "end",
        "frames": frame_no, "reason": reason,
    }) + "\n")
    print(f"[calib] recording stopped: {reason}", flush=True)


def _write_metadata(path: Path, meta: dict) -> None:
    """Publish complete status snapshots for an operator monitoring capture."""
    pending = path.with_name(path.name + ".tmp")
    pending.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(5):
        try:
            pending.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05)


def _require_foreground(hwnd: int, attempts: int = 6) -> None:
    for _ in range(attempts):
        if force_foreground(hwnd):
            return
        time.sleep(0.3)
    raise RuntimeError("could not keep the game window in the foreground")


def _wait_for_operator_start(start_file: Path, stop_file: Path, hwnd: int) -> None:
    """Wait without resuming the validated game or publishing any input."""
    while True:
        if stop_file.exists():
            raise RuntimeError("recording stopped before operator start")
        if not _window_exists(hwnd):
            raise RuntimeError("game closed before operator start")
        if start_file.is_file():
            return
        time.sleep(0.1)


def _capture_milestone(
        env, hwnd: int, path: Path, *, require_focus: bool = True) -> None:
    if require_focus:
        _require_foreground(hwnd)
        time.sleep(0.5)
    image = asyncio.run(env.render_locked())
    image.save(path, format="PNG")
    print(f"[calib] saved {path.name} ({image.size[0]}x{image.size[1]})",
          flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", help="Windows benchmark scenario slug")
    ap.add_argument("--game", help="deprecated alias for --scenario")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--fps", type=float, default=10.0)
    duration = ap.add_mutually_exclusive_group()
    duration.add_argument("--max-seconds", type=float, default=300.0)
    duration.add_argument("--until-stopped", action="store_true",
                          help="Record until the operator stop file or game exit; leave the game open.")
    ap.add_argument("--stop-file", type=Path,
                    help="Operator stop-request path (default: OUT/stop.request).")
    ap.add_argument("--start-file", type=Path,
                    help="Keep the validated start frozen until this fresh operator request exists.")
    ap.add_argument("--inspect-seconds", type=float, default=0.0,
                    help="Hold the validated start frozen for 0..300 seconds before recording.")
    ap.add_argument(
        "--marker", action="append", metavar="F6=LABEL",
        help="semantic marker; repeat as needed (valid hotkeys: F6-F11)")
    args = ap.parse_args()
    if args.scenario and args.game and args.scenario != args.game:
        ap.error("--scenario and legacy --game disagree")
    scenario = args.scenario or args.game
    if not scenario:
        ap.error("--scenario is required")
    if (not math.isfinite(args.fps) or args.fps <= 0
            or not math.isfinite(args.max_seconds) or args.max_seconds <= 0):
        ap.error("--fps and --max-seconds must be finite and positive")
    if not 0 <= args.inspect_seconds <= 300:
        ap.error("--inspect-seconds must be between 0 and 300")
    try:
        markers = parse_markers(args.marker)
    except ValueError as exc:
        ap.error(str(exc))

    spec = load_game_spec(scenario)
    cfg = load_game_config(spec["driver_game"])
    al = cfg.auto_launch
    if al is None or not al.launch_path:
        raise RuntimeError(
            f"games/{spec['driver_game']}.yaml has no auto_launch.launch_path")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = (args.out or (ROOT / "out" / "scorer_calibration" /
                        scenario / stamp)).resolve()
    stop_file = (args.stop_file or (out / "stop.request")).resolve()
    start_file = args.start_file.resolve() if args.start_file else None
    if stop_file.exists():
        ap.error(f"stop-request file already exists: {stop_file}")
    if start_file is not None and (start_file.exists() or start_file == stop_file):
        ap.error("--start-file must be fresh and distinct from the stop-request path")
    frames_dir = out / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    events_path = out / "events.jsonl"
    meta_path = out / "meta.json"

    meta = {
        "game": spec["driver_game"],
        "scenario": scenario,
        "fps": args.fps,
        "max_seconds": None if args.until_stopped else args.max_seconds,
        "stop_policy": ("operator_file_or_game_exit" if args.until_stopped
                        else "duration_operator_file_or_game_exit"),
        "status": "starting",
        "recorder_pid": os.getpid(),
        "inspect_seconds": args.inspect_seconds,
        "hotkeys": marker_names(markers),
        "stop_file": str(stop_file),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_metadata(meta_path, meta)

    env = None
    frame_no = 0
    human_handoff = False
    recording_completed = False

    print(f"[calib] output: {out}", flush=True)
    print("[calib] cold-starting: state setup -> auto-launch -> menu_skip ...",
          flush=True)
    controller = ResetController(spec)

    def prepare_launch() -> None:
        meta["state_setup"] = prepare_launch_state(spec, cfg)

    def capture_post_settle(session_env, hwnd: int) -> None:
        if not controller._force_foreground(session_env):
            raise RuntimeError("could not focus game before startup capture")
        time.sleep(0.5)
        _capture_milestone(
            session_env, hwnd, out / "01_post_settle.png", require_focus=False)

    try:
        env, hwnd, _title = start_auto_launch_session(
            cfg,
            before_launch=prepare_launch,
            after_window=lambda live_hwnd: enforce_live_window(
                spec, cfg, live_hwnd),
            post_settle=capture_post_settle,
            menu_skip_replayer=controller.replay_menu_skip,
            require_menu_skip=True,
            platform_name=controller.platform,
            env_fps=controller.tics_per_second,
            use_speedhack=bool(getattr(cfg, "use_speedhack", False)),
        )
        if not controller.prepare_policy_handoff(env):
            raise RuntimeError(
                "start_check failed after menu_skip; calibration handoff is "
                "not ready")
        # The verified start frame is hard-frozen.  Never focus a suspended
        # HWND: BringWindowToTop can block forever against its stopped thread.
        _capture_milestone(
            env, hwnd, out / "02_after_menu_skip.png", require_focus=False)
        if args.inspect_seconds:
            print(f"[calib] INSPECT: validated start is frozen for "
                  f"{args.inspect_seconds:g}s before human recording.", flush=True)
            time.sleep(args.inspect_seconds)
        if start_file is not None:
            meta["status"] = "waiting_for_operator_start"
            meta["start_file"] = str(start_file)
            _write_metadata(meta_path, meta)
            print(f"[calib] START GATE: game remains frozen; operator release: "
                  f"{start_file}", flush=True)
            _wait_for_operator_start(start_file, stop_file, hwnd)

        # The TUI full test closes here. Calibration deliberately keeps the
        # same env/pads alive and hands the resulting in-game state to a human.
        env.hard_pause = False
        env.unpause()
        human_handoff = True
        _require_foreground(hwnd)
        print("[calib] Starting capture; wait for READY before playing.", flush=True)
        labels = ", ".join(
            f"{key} {label}" for key, label in marker_names(markers).items())
        print(f"[calib] Optional markers: {labels}", flush=True)
        print(f"[calib] Operator stop: create {stop_file}", flush=True)
        if args.until_stopped:
            print("[calib] Manual-stop mode: no automatic duration stop; "
                  "the game stays open after recording.", flush=True)

        def recording_ready():
            meta["handoff_at"] = datetime.now().isoformat(timespec="seconds")
            meta["status"] = "recording"
            _write_metadata(meta_path, meta)
            print("[calib] READY: recording is active; first frame saved. "
                  "Play the encounter now.", flush=True)

        def recording_progress():
            _write_metadata(meta_path, meta)
            state = meta.get('capture_status', 'recording').upper()
            print(f"[calib] {state}: {meta['frames']} frames saved, "
                  f"{meta['last_frame_elapsed_seconds']:.1f}s captured.", flush=True)

        with events_path.open("w", encoding="utf-8", buffering=1) as events:
            _record_frames(env, hwnd, frames_dir, events, fps=args.fps,
                           max_seconds=meta["max_seconds"], markers=markers,
                           stop_file=stop_file, meta=meta,
                           on_first_frame=recording_ready,
                           on_progress=recording_progress)
        recording_completed = True
    except (Exception, KeyboardInterrupt) as exc:
        meta["status"] = "failed"
        meta["error"] = f"{type(exc).__name__}: {exc}"
        try:
            if env is not None:
                env.render().save(out / "failure.png")
            else:
                from PIL import ImageGrab
                ImageGrab.grab(all_screens=True).save(out / "failure.png")
        except Exception as capture_exc:
            meta["failure_capture_error"] = str(capture_exc)
        raise
    finally:
        frame_no = meta.get("frames", 0)
        meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
        meta["frames"] = frame_no
        if recording_completed:
            meta["status"] = "stopped"
        preserve_game = human_handoff and (args.until_stopped or not recording_completed)
        meta["cleanup"] = {
            "game_termination_requested": not preserve_game,
            "reason": ("recording_failed_after_handoff" if human_handoff and not recording_completed
                       else "manual_recording_stopped" if preserve_game
                       else "recording_completed" if recording_completed
                       else "startup_failed"),
        }
        if env is not None:
            try:
                env.close()
            except Exception as exc:  # noqa: BLE001 - cleanup must continue
                meta["cleanup"]["env_close_error"] = repr(exc)
                print(f"[calib] WARN env close failed: {exc!r}", flush=True)
        if preserve_game:
            print("[calib] Game termination was not requested; capture/input "
                  "resources were released. See metadata for recording status.", flush=True)
        else:
            try:
                ss.kill_process(cfg.process)
            except Exception as exc:  # noqa: BLE001 - preserve recording artifacts
                meta["cleanup"]["game_termination_error"] = repr(exc)
                print(f"[calib] WARN game cleanup failed: {exc!r}", flush=True)
        _write_metadata(meta_path, meta)
    print(f"[calib] saved {frame_no} frames to {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
