"""WindowsGameEnv — stepped adapter over the real-game driver stack.

The world advances ONLY inside act(): the driver Env
keeps the game hard-frozen (NtSuspendProcess) between acts, so model thinking
costs no game time. The scenario runtime config defines the tic rate; omitted
values retain the historical 10 Hz default. Reward and done come from a pure-CV
scorer, never from game memory."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import cv2

from coding4game_gui.benchmark.evidence.event_log import emit as emit_windows_event
from coding4game_gui.benchmark.evidence.event_log import span as windows_event_span
from coding4game_gui.benchmark.windows.diagnostics.foreground_failure import record_foreground_failure
from coding4game_gui.paths import REPO_ROOT


def _pin_dpi_awareness() -> None:
    """Make all Windows geometry observations use physical pixels."""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        if not user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001 — non-Windows or already configured
        pass


_pin_dpi_awareness()

import numpy as np

from coding4game_gui.benchmark.windows.verbs import build_action_table
from coding4game_gui.benchmark.windows.timing import DEFAULT_TICS_PER_SECOND
from coding4game_gui.benchmark.windows.timing import resolve_tics_per_second
from coding4game_gui.benchmark.windows.timing import resolve_max_action_tics

TICRATE = DEFAULT_TICS_PER_SECOND
TIMING_WARN_FRAC = 0.05      # per-act wall-vs-game-time deviation warning
TIMING_GRACE_S = 0.05        # fixed overhead allowance (pause/render)
STARTUP_FAILURE_DIR = REPO_ROOT / "out/windows_startup_failures"


def _to_bgr(pil_img) -> np.ndarray:
    a = np.asarray(pil_img.convert("RGB"))
    return np.ascontiguousarray(a[:, :, ::-1])


ASPECT_TOLERANCE = 0.01


def _conform(frame: np.ndarray, target) -> np.ndarray:
    """Resize a frame to the calibrated geometry.

    Calibrated scorer rects and click coordinates are expressed in the
    game's RENDER size, which is not a property anyone configures directly:
    a DPI-unaware game in fullscreen renders at the LOGICAL desktop size,
    i.e. monitor resolution / DPI scale. Our reference box is 1920x1080 at
    125%, hence 1536x864 — a second machine matches only if BOTH its
    resolution and its scaling match.

    Uniform scaling lifts that: any resolution with the same aspect ratio
    now works, because the game lays its UI out proportionally. A different
    ASPECT ratio is a genuine mismatch (the UI reflows, so no scaling can
    map the coordinates) and must fail loudly rather than silently misclick.
    """
    if not target:
        return frame
    tw, th = int(target[0]), int(target[1])
    h, w = frame.shape[:2]
    if (w, h) == (tw, th):
        return frame
    if abs((w / max(1, h)) - (tw / max(1, th))) > ASPECT_TOLERANCE:
        raise ValueError(
            f"frame {w}x{h} has a different ASPECT RATIO than the "
            f"calibration {tw}x{th}; the game re-lays out its UI, so "
            f"coordinates cannot be rescaled. Match the calibrated aspect "
            f"(e.g. set the game to a 16:9 mode) or recalibrate.")
    return cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)


def _calibrated_size(spec: dict):
    """The render geometry a scenario's coordinates were measured in."""
    cal = ((spec or {}).get("scorer") or {}).get("calibrated_client_size")
    return (int(cal[0]), int(cal[1])) if cal else None


def capture_progress_score(progress: dict | None, tics: int,
                           scorer_spec: dict):
    """Score saved targets, completion speed, and consumable efficiency."""
    cfg = (scorer_spec or {}).get("capture_progress") or {}
    max_tics = int(cfg.get("max_tics", 1800))
    tics_per_second = float(cfg.get("tics_per_second", 10.0))
    target_points = float(cfg.get("points_per_target", 30.0))
    first_speed_points = float(cfg.get("first_capture_speed_points", 0.0))
    completion_speed_points = float(
        cfg.get("full_completion_speed_points", 40.0))
    waste_penalty_per_item = float(cfg.get("waste_penalty_per_item", 0.0))
    max_waste_penalty = float(cfg.get("max_waste_penalty", 0.0))
    if (max_tics <= 0 or tics_per_second <= 0
            or target_points < 0 or first_speed_points < 0
            or completion_speed_points < 0
            or waste_penalty_per_item < 0 or max_waste_penalty < 0):
        raise ValueError("invalid scorer.capture_progress configuration")
    progress = dict(progress or {})
    required = tuple(dict.fromkeys(
        str(item) for item in progress.get("target_character_ids", [])))
    captured_set = {
        str(item) for item in
        progress.get("captured_target_character_ids", [])
    }
    captured = tuple(target for target in required if target in captured_set)
    captured_count = len(captured)
    complete = bool(required) and captured_count == len(required)
    base_points = target_points * captured_count
    first_capture_raw = progress.get("first_capture_tic")
    first_capture_tic = (
        int(first_capture_raw)
        if captured_count > 0 and isinstance(first_capture_raw, (int, float))
        else int(tics) if captured_count > 0 else None
    )
    bounded_first_tics = (
        min(max(first_capture_tic, 0), max_tics)
        if first_capture_tic is not None else max_tics
    )
    first_speed_fraction = (
        1.0 - bounded_first_tics / max_tics if captured_count > 0 else 0.0
    )
    bounded_completion_tics = min(max(int(tics), 0), max_tics)
    completion_speed_fraction = (
        1.0 - bounded_completion_tics / max_tics if complete else 0.0
    )
    earned_first_speed_points = first_speed_points * first_speed_fraction
    earned_completion_speed_points = (
        completion_speed_points * completion_speed_fraction)
    earned_speed_points = (
        earned_first_speed_points + earned_completion_speed_points)
    consumed_raw = progress.get("consumable_consumed_count")
    consumable_tracking_available = isinstance(consumed_raw, (int, float))
    consumed_count = max(int(consumed_raw), 0) if consumable_tracking_available else 0
    # One sphere per durable capture is necessary, so only surplus consumption
    # is waste. This remains valid for partial credit and interrupted episodes.
    wasted_count = max(consumed_count - captured_count, 0)
    waste_penalty = min(
        wasted_count * waste_penalty_per_item, max_waste_penalty)
    score = max(base_points + earned_speed_points - waste_penalty, 0.0)
    return round(score, 6), {
        "captured_target_character_ids": list(captured),
        "captured_target_count": captured_count,
        "required_target_count": len(required),
        "complete": complete,
        "first_capture_tics": first_capture_tic,
        "first_capture_seconds": (
            first_capture_tic / tics_per_second
            if first_capture_tic is not None else None),
        "first_capture_speed_fraction": first_speed_fraction,
        "first_capture_speed_points": earned_first_speed_points,
        "completion_tics": int(tics) if complete else None,
        "completion_seconds": (
            int(tics) / tics_per_second if complete else None),
        "base_points": base_points,
        "completion_speed_fraction": completion_speed_fraction,
        "completion_speed_points": earned_completion_speed_points,
        "speed_fraction": completion_speed_fraction,
        "speed_points": earned_speed_points,
        "consumable_item_id": progress.get("consumable_item_id"),
        "consumable_tracking_available": consumable_tracking_available,
        "consumable_consumed_count": consumed_count,
        "necessary_consumable_count": captured_count,
        "wasted_consumable_count": wasted_count,
        "waste_penalty": waste_penalty,
        "score": round(score, 6),
    }


# ---------------------------------------------------------------------------
# Crash-safe teardown. The freeze primitive (NtSuspendProcess) accumulates a
# suspend count on the TARGET game process that SURVIVES the suspender's death:
# a run killed/fused/crashed mid-pause() leaves the game frozen for the NEXT
# run, and the pile-up of leaked handles + frozen games is what progressively
# wedges the box (black screen, launches that never complete). These hooks
# guarantee the game is resumed on any *catchable* exit; the boot-time drain
# (ResetController._drain_by_name) is the backstop for a hard taskkill /F.
# ---------------------------------------------------------------------------
import atexit

_CLEANUP_PROC_NAMES: set[str] = set()
_CLEANUP_INSTALLED = False


def resume_game_by_name(proc_name: str, max_resumes: int = 32) -> None:
    """Idempotent: resume every process named `proc_name` until its OS suspend
    count is fully drained. Resuming a running (count-0) process is a harmless
    no-op; NtResumeProcess reports non-success once the count hits 0, so we stop
    there. Bounded so a bogus name can't spin."""
    try:
        import psutil
        from coding4game_gui.benchmark.windows.drivers.windows_xinput.proc_freeze import resume_process
    except Exception:  # noqa: BLE001 — non-windows / fake driver
        return
    for proc in psutil.process_iter(["name", "pid"]):
        if (proc.info.get("name") or "").lower() != proc_name.lower():
            continue
        for _ in range(max_resumes):
            try:
                if not resume_process(proc.info["pid"]):
                    break        # non-success == suspend count already drained
            except Exception:  # noqa: BLE001
                break


def _run_exit_cleanup() -> None:
    for name in list(_CLEANUP_PROC_NAMES):
        resume_game_by_name(name)


def _install_exit_cleanup(proc_name: str) -> None:
    """Register (once) an atexit hook that un-freezes every registered game on
    interpreter shutdown — normal return, sys.exit, or an unhandled exception.
    A hard `taskkill /F` / os._exit can't be caught; the boot drain covers it."""
    global _CLEANUP_INSTALLED
    _CLEANUP_PROC_NAMES.add(proc_name)
    if not _CLEANUP_INSTALLED:
        _CLEANUP_INSTALLED = True
        atexit.register(_run_exit_cleanup)


class StartupConstructionCleanupError(Exception):
    """A failed environment construction could not safely release its game."""


def make_bench_env(scenario, *, windows_input=None):
    """Load the registered scenario and create its Windows game environment."""
    return WindowsGameEnv(scenario, windows_input=windows_input)


class WindowsGameEnv:
    TICRATE = TICRATE

    def __init__(self, scenario: str, *, driver_env=None, scorer=None,
                 actions=None, controller=None, spec=None,
                 windows_input: str | None = None, save_verifier=None):
        if spec is None:
            from coding4game_gui.benchmark.spec import load_game_spec
            spec = load_game_spec(scenario, windows_input=windows_input)
        self.spec = spec
        from coding4game_gui.benchmark.windows.terminal import validate_terminal_detection
        self.terminal_detection = validate_terminal_detection(spec)
        self.stepped_terminal_sampling = (spec.get('lifecycle') or {}).get(
            'stepped_terminal_sampling', 'action_end_v1')
        from coding4game_gui.benchmark.windows.raw_actions import WINDOWS_INPUT_GAMEPAD_KBM
        from coding4game_gui.benchmark.windows.raw_actions import normalize_windows_input_mode
        configured_input = windows_input
        if configured_input is None:
            configured_input = (spec.get("runtime") or {}).get("windows_input")
        self.windows_input = normalize_windows_input_mode(configured_input)
        self.tics_per_second = resolve_tics_per_second(spec.get("runtime"))
        self.max_action_tics = resolve_max_action_tics(spec.get("runtime"))
        from coding4game_gui.benchmark.windows.timing import resolve_passive_score_interval_tics
        self._passive_score_interval_tics = resolve_passive_score_interval_tics(spec.get("runtime"))
        refresh_delay = float((spec.get("runtime") or {}).get(
            "post_action_observation_delay_seconds", 0.0))
        if not math.isfinite(refresh_delay) or not 0.0 <= refresh_delay <= 1.0:
            raise ValueError(
                "runtime.post_action_observation_delay_seconds must be in [0, 1]")
        self._post_action_observation_delay_seconds = refresh_delay
        # Generic rollout/render code reads the historical class attribute from
        # the instance. Shadow it so every consumer sees this scenario's rate.
        self.TICRATE = self.tics_per_second
        # Every frame is conformed to this, so scorer rects and click
        # coordinates stay valid on a machine whose resolution or DPI
        # scaling differs from the one they were measured on.
        # Keep native pixels sharp; scorers scale calibrated rects to frames.
        self._render_size = None
        self._controller = controller
        if driver_env is None or scorer is None or actions is None:
            if self._controller is None:
                platform = ("windows-kbm"
                            if self.windows_input == WINDOWS_INPUT_GAMEPAD_KBM
                            else "auto")
                self._controller = ResetController(spec, platform=platform)
            if (getattr(self._controller.cfg, "platform", None) == "windows-kbm"
                    and self.windows_input != WINDOWS_INPUT_GAMEPAD_KBM):
                raise ValueError("this scenario requires --windows-input gamepad-kbm")
            try:
                driver_env = self._controller.ensure_env()
                scorer = self._controller.make_scorer(driver_env)
                actions = self._controller.action_table()
            except BaseException as startup_error:
                # The caller cannot close an adapter whose constructor failed.
                # Its controller still owns the registered cold-start cleanup.
                try:
                    self._controller.shutdown()
                except BaseException as cleanup_error:
                    raise StartupConstructionCleanupError(
                        "startup_cleanup_failed: environment construction failed "
                        f"({type(startup_error).__name__}); controller cleanup "
                        f"failed ({type(cleanup_error).__name__})") from cleanup_error
                raise
        self._env = driver_env
        self._scorer = scorer
        self._actions = actions        # UPPER token -> ActionSpec
        self._tic = 0
        self._game_running_seconds = 0.0
        self.last_action_timing = None
        self._total = 0.0
        self._score_breakdown = None
        self._done = True              # force reset() before the first act()
        self._episode_id: int | None = None
        self._last_frame: np.ndarray | None = None
        self._timing_warned = 0
        # Registered terminal screens end the episode; stillness is not terminal.
        self.end_reason: str | None = None
        self._save_verifier = save_verifier
        self._verified_completion: dict | None = None
        self._first_capture_tic: int | None = None
        self._completion_tic: int | None = None
        self._terminal_monitor = None
        self._terminal_directory = None
        self._terminal_episode_uid = None
        self._action_id = 0
        self._batch_id = None
        self._process_observer = None
        self._milestone_evidence = []
        self._terminal_evidence = []
        if self._save_verifier is None:
            from coding4game_gui.benchmark.windows.save_verification import (
                SaveVerificationMonitor, parse_save_verification_spec,
            )
            verification_spec = parse_save_verification_spec(
                spec.get("lifecycle"))
            if verification_spec is not None:
                save_state = getattr(
                    getattr(self._controller, "cfg", None), "save_state", None)
                if save_state is None:
                    raise ValueError(
                        "save verification requires driver save_state")
                self._save_verifier = SaveVerificationMonitor(
                    verification_spec, save_state.restore_to)

    # -- episode control (infrastructure only) --
    def reset(self, episode_id: int = 0) -> None:
        """Start one fresh unseeded episode from the recorded cold-start path."""
        emit_windows_event(
            "windows_env_reset", "begin", episode_id=int(episode_id),
            scenario=(self.spec.get("runtime") or {}).get("scenario"))
        if self._terminal_monitor is not None:
            self._terminal_monitor.close()
        if self._process_observer is not None:
            self._process_observer.result()
        self._terminal_monitor = None
        self._terminal_directory = None
        self._terminal_episode_uid = None
        self._action_id = 0
        self._batch_id = None
        self._process_observer = None
        self._milestone_evidence = []
        self._terminal_evidence = []
        from collections import deque
        confirmations = max([2] + [int(value.get('confirm_frames', 1))
            for value in (self.spec.get('scorer') or {}).values()
            if isinstance(value, dict)])
        self._terminal_recent = deque(maxlen=confirmations)
        if self._save_verifier is not None:
            self._save_verifier.stop()
        self._episode_id = int(episode_id)
        self._tic = 0
        self._game_running_seconds = 0.0
        self.last_action_timing = None
        self._action_halted = False
        self._total = 0.0
        self._score_breakdown = None
        if getattr(self, '_deferred_score', None) is not None:
            self._deferred_score.close()
        self._deferred_score = None
        self._done = False
        self.end_reason = None
        self._last_frame = None
        self._verified_completion = None
        self._first_capture_tic = None
        self._completion_tic = None
        if self._controller is not None:
            env = self._controller.ensure_env()
            if env is not self._env:
                self._env = env
                self._scorer = self._controller.make_scorer(env)
        if self._controller is None:
            self._scorer.reset()
        elif not self._controller.prepare_policy_handoff(self._env):
            env = self._controller.cold_restart()
            self._env = env
            self._scorer = self._controller.make_scorer(env)
            if not self._controller.prepare_policy_handoff(self._env):
                raise RuntimeError(
                    "episode start_check failed after cold-start retry")
        self._last_frame = _conform(_to_bgr(self._env.render()),
                                    self._render_size)
        begin_episode = getattr(self._scorer, "begin_episode", None)
        if callable(begin_episode):
            begin_episode(self._last_frame)
        if (self.spec.get('scorer') or {}).get('score_version') == 'overcooked_2_2_1_composite_v1':
            from coding4game_gui.benchmark.windows.scorers.overcooked_process import ProcessObserver
            self._process_observer = ProcessObserver()
        if self._save_verifier is not None:
            self._save_verifier.start()
        emit_windows_event(
            "windows_env_reset", "end", episode_id=self._episode_id,
            game_pid=getattr(self._env, "game_pid", None), tic=self._tic)

    def finish_episode(self) -> None:
        """Close the real game; the next reset always performs a cold start."""
        emit_windows_event(
            "finish_episode", "begin", episode_id=self._episode_id,
            tic=self._tic, done=self._done, end_reason=self.end_reason,
            source="WindowsGameEnv.finish_episode")
        try:
            if self._save_verifier is not None:
                self._save_verifier.final_check()
                self._consume_save_verification()
            self.finalize_score()
        finally:
            if self._save_verifier is not None:
                self._save_verifier.stop()
            if self._controller is not None:
                self._controller.shutdown()
                self._env = None
            self._done = True
        emit_windows_event(
            "finish_episode", "end", episode_id=self._episode_id,
            tic=self._tic, done=self._done,
            source="WindowsGameEnv.finish_episode")

    def close(self) -> None:
        self.finish_episode()

    def window_info(self) -> dict:
        """Public window geometry only; never launch/save/scorer configuration."""
        renderer = getattr(self._env, "renderer", None)
        hwnd = getattr(renderer, "hwnd", None)
        if not hwnd:
            # Screen-region renderers such as dxcam intentionally store only a
            # capture bbox. The lifecycle still owns the authoritative HWND
            # discovered for that same process.
            lifecycle = getattr(self._env, "lifecycle", None)
            info = getattr(lifecycle, "_found_cache", None)
            hwnd = (getattr(info, "extras", None) or {}).get("hwnd")
        if hwnd:
            import win32gui
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            x, y = win32gui.ClientToScreen(hwnd, (left, top))
            rect = [x, y, right - left, bottom - top]
        else:
            rect = list(getattr(self._env, "bbox", (0, 0, 0, 0)))
        return {"hwnd": hwnd, "pid": getattr(self._env, "game_pid", None),
                "client_rect": rect, "observation_size": [1280, 768]}

    def halt_actions(self) -> None:
        """Cancel the active pulse and freeze without waiting for capture."""
        self._action_halted = True
        if self._env is not None:
            self._env.freeze_now()

    def _consume_save_verification(self) -> None:
        if self._save_verifier is None or self._verified_completion is not None:
            return
        progress = getattr(self._save_verifier, "progress", None)
        if progress is not None and self._first_capture_tic is None:
            captured = progress.get("captured_target_character_ids") or []
            if captured:
                recorded_tic = progress.get("first_capture_tic")
                self._first_capture_tic = (
                    int(recorded_tic)
                    if isinstance(recorded_tic, (int, float)) else self._tic)
                emit_windows_event(
                    "save_verification", "first_capture_detected",
                    episode_id=self._episode_id, tic=self._tic,
                    first_capture_tic=self._first_capture_tic,
                    evidence=dict(progress))
        result = self._save_verifier.result
        if result is None:
            return
        # The capture attempt is not proof of success.  Score the first game tic
        # at which the host has parsed durable save evidence containing every
        # required new Palbox instance.
        self._completion_tic = self._tic
        if self._first_capture_tic is None:
            self._first_capture_tic = self._completion_tic
        self._verified_completion = dict(
            result, first_capture_tic=self._first_capture_tic,
            verified_tic=self._completion_tic)
        self._done = True
        self.end_reason = "terminal:save_verified_capture"
        emit_windows_event(
            "save_verification", "detected", episode_id=self._episode_id,
            tic=self._tic, completion_tic=self._completion_tic,
            end_reason=self.end_reason, evidence=self._verified_completion)

    def _save_verification_ready(self) -> bool:
        if self._save_verifier is None:
            return False
        completed = getattr(self._save_verifier, "completed", None)
        if completed is not None:
            return bool(completed)
        return self._save_verifier.result is not None

    def _check_terminal(self, frame, *, source):
        """Inspect registered terminal evidence without settling any reward."""
        observer = getattr(self, '_process_observer', None)
        if observer is not None:
            try:
                observer.observe(frame, source)
            except BaseException:
                self._done = self._action_halted = True
                self.end_reason = 'process_evidence_failure'
                raise
        if (getattr(self, 'stepped_terminal_sampling', None) == 'during_action_v2'
                and self.terminal_detection):
            try:
                monitor = self._get_terminal_monitor()
                confirmed = monitor.capture(
                    frame, source=source, action_id=self._action_id,
                    batch_id=self._batch_id,
                    tic=None if source == 'held_action_sample' else self._tic,
                    force=source == 'frozen_action_frame')
            except BaseException:
                self._done = self._action_halted = True
                self.end_reason = 'terminal_evidence_or_detection_failure'
                raise
            if confirmed and not self._done:
                self._done = self._action_halted = True
                self._completion_tic = self._tic
                self.end_reason = 'terminal:results'
                emit_windows_event('terminal_detection', 'detected',
                    episode_id=self._episode_id, source=source,
                    sampling_version=self.stepped_terminal_sampling,
                    recognition_version='overcooked_results_v2',
                    confirmed_frame_id=monitor.confirmed_frame['frame_id'],
                    tic=None if source == 'held_action_sample' else self._tic,
                    end_reason=self.end_reason)
            return
        if self._done or not self.terminal_detection:
            return
        if getattr(self, 'stepped_terminal_sampling', None) == 'during_action_v1':
            self._terminal_recent.append(dict(
                frame=frame, source=source,
                tic=None if source == 'held_action_sample' else self._tic,
                captured_monotonic=time.perf_counter()))
        if not self._scorer.is_terminal(frame):
            return
        phase = self._scorer.classify_phase(frame)
        self._done = True
        if getattr(self, 'stepped_terminal_sampling', None) == 'during_action_v1':
            self._terminal_evidence = list(self._terminal_recent)
        self._completion_tic = self._tic
        self.end_reason = ('game_over' if phase in {'death', 'defeat', 'gameover', 'mission_failed', 'failure'}
                           else f'terminal:{phase or "unknown"}')
        emit_windows_event('terminal_detection', 'detected', episode_id=self._episode_id,
                           tic=self._tic, terminal_phase=phase, source=source,
                           end_reason=self.end_reason)

    def set_action_context(self, *, batch_id):
        """Attach the trusted caller's batch identity to native evidence."""
        self._batch_id = batch_id

    def start_terminal_evidence(self, directory: Path, episode_uid: str) -> None:
        if self.stepped_terminal_sampling == 'during_action_v2':
            self._terminal_directory = directory
            self._terminal_episode_uid = episode_uid
            self._get_terminal_monitor().capture(
                self._last_frame, source='initial_frozen_frame', tic=self._tic)

    def _get_terminal_monitor(self):
        if self._terminal_monitor is None:
            from coding4game_gui.benchmark.windows.terminal_evidence import TerminalMonitor, EvidenceFailure
            import tempfile
            import uuid
            directory = self._terminal_directory or Path(tempfile.mkdtemp(prefix='c4g-terminal-'))
            uid = self._terminal_episode_uid or uuid.uuid4().hex
            try:
                self._terminal_monitor = TerminalMonitor(
                    self._scorer, directory, uid, self.TICRATE)
            except OSError as exc:
                raise EvidenceFailure(f'cannot initialize native evidence: {exc}') from exc
        return self._terminal_monitor

    # -- the frozen primitive surface (what policies get) --
    def frame(self) -> np.ndarray:
        if self._last_frame is None:
            self._last_frame = _conform(_to_bgr(self._env.render()), self._render_size)
        return self._last_frame

    def validate_action_plan(
            self, acts: list[tuple[list[str], int]]) -> None:
        """Validate a complete stepped Direct reply without touching the live game.

        ``play_one`` calls this before executing the first ACT line, making a
        multi-line Player reply transactional: one malformed raw control rejects
        the whole reply instead of applying an earlier valid prefix.
        """
        from coding4game_gui.benchmark.windows.raw_actions import resolve_raw_action
        from coding4game_gui.benchmark.windows.timing import validate_action_tics
        for buttons, _tics in acts:
            validate_action_tics(_tics, self.max_action_tics)
            resolve_raw_action(
                [str(button).upper() for button in buttons], self._actions,
                input_mode=self.windows_input,
                content_size=_calibrated_size(self.spec))

    def _locate_text(self, pattern: str):
        """Resolve a text-targeted click against the live screen."""
        locate = getattr(self._scorer, "locate_text", None)
        if locate is None:      # scorer without OCR (template/health kinds)
            return None
        try:
            point = locate(_to_bgr(self._env.render()), pattern)
            if point is None:
                return None
            back = getattr(self._scorer, "to_calibrated", None)
            return back(point[0], point[1]) if back else point
        except Exception as e:  # noqa: BLE001 — a miss must not kill the run
            print(f"[wge] locate_text({pattern!r}) failed: "
                  f"{type(e).__name__}: {e}")
            return None

    def _do_clicks(self, points) -> None:
        proc = getattr(getattr(self, "_controller", None), "cfg", None)
        coordinate_size = _calibrated_size(self.spec)
        _click_game_points(getattr(proc, "process", ""), self._env, points,
                           coordinate_size, atomic=True,
                           coordinate_size=coordinate_size,
                           require_game_target=True)

    def _do_click_current(self) -> None:
        proc = getattr(getattr(self, "_controller", None), "cfg", None)
        _click_game_current(getattr(proc, "process", ""), self._env, atomic=True)

    def _do_moves(self, points) -> None:
        proc = getattr(getattr(self, "_controller", None), "cfg", None)
        coordinate_size = _calibrated_size(self.spec)
        _move_game_points(getattr(proc, "process", ""), self._env, points,
                          coordinate_size, coordinate_size=coordinate_size)

    def _do_relative_moves(self, deltas) -> None:
        proc = getattr(getattr(self, "_controller", None), "cfg", None)
        _relative_move_game(
            getattr(proc, "process", ""), self._env, deltas,
            _calibrated_size(self.spec))

    def _do_right_clicks(self, points) -> None:
        proc = getattr(getattr(self, "_controller", None), "cfg", None)
        coordinate_size = _calibrated_size(self.spec)
        _right_click_game_points(
            getattr(proc, "process", ""), self._env, points,
            coordinate_size, coordinate_size=coordinate_size,
            require_game_target=True)

    def _do_middle_clicks(self, points) -> None:
        proc = getattr(getattr(self, "_controller", None), "cfg", None)
        coordinate_size = _calibrated_size(self.spec)
        _middle_click_game_points(
            getattr(proc, "process", ""), self._env, points,
            coordinate_size, coordinate_size=coordinate_size,
            require_game_target=True)

    def _do_double_clicks(self, points) -> None:
        proc = getattr(getattr(self, "_controller", None), "cfg", None)
        coordinate_size = _calibrated_size(self.spec)
        _double_click_game_points(
            getattr(proc, "process", ""), self._env, points,
            coordinate_size, coordinate_size=coordinate_size,
            require_game_target=True)

    def _do_drags(self, drags, duration: float) -> None:
        proc = getattr(getattr(self, "_controller", None), "cfg", None)
        coordinate_size = _calibrated_size(self.spec)
        _drag_game_paths(
            getattr(proc, "process", ""), self._env, drags,
            coordinate_size, duration=duration,
            coordinate_size=coordinate_size, require_game_target=True)

    def _do_scrolls(self, scrolls) -> None:
        proc = getattr(getattr(self, "_controller", None), "cfg", None)
        coordinate_size = _calibrated_size(self.spec)
        _scroll_game_points(
            getattr(proc, "process", ""), self._env, scrolls,
            coordinate_size, coordinate_size=coordinate_size,
            require_game_target=True)

    def act(self, buttons: list[str], tics: int) -> float | None:
        from coding4game_gui.benchmark.windows.timing import validate_action_tics
        validate_action_tics(tics, self.max_action_tics)
        self.last_action_timing = None
        if self._done:
            raise RuntimeError('episode has ended; no further input is permitted')
        if getattr(self, "_action_halted", False):
            raise RuntimeError("game action halted by host")
        toks = [str(b).upper() for b in buttons]
        # Raw Windows actions may include parameterised controls such as
        # ``LS[0.1,0.1]``.  Keep the resolver at this boundary so every caller
        # (Lang, Code, 2P helpers, and tests) gets identical validation and
        # exact analogue values.  Fixed YAML actions remain supported.
        from coding4game_gui.benchmark.windows.raw_actions import resolve_raw_action
        emit_windows_event(
            "windows_env_act", "begin", episode_id=self._episode_id,
            tic_before=self._tic, buttons=toks, requested_tics=tics)
        # Resolve before touching the game.  This also gives malformed or
        # unknown dynamic tokens an explicit action error rather than letting
        # them be misreported as a missing ACT line upstream.
        resolved = resolve_raw_action(
            toks, self._actions, input_mode=self.windows_input,
            content_size=_calibrated_size(self.spec))
        if self._save_verifier is not None:
            self._save_verifier.note_action(toks, self._tic)
        state = resolved.state
        fixed_toks = [b for b in toks if b in self._actions]
        if self._controller is not None:
            if not self._controller.alive():
                emit_windows_event(
                    "game_watchdog", "error", episode_id=self._episode_id,
                    tic=self._tic, process=self._controller.cfg.process,
                    error="game process died mid-episode")
                raise RuntimeError("watchdog: game process died mid-episode")
        requested_tics = tics
        requested_duration = requested_tics / self.TICRATE
        charged_tics = requested_tics
        specs = [self._actions[b] for b in fixed_toks]
        # A pointer action is positional rather than a held pad state. Its
        # event is injected near the start of one complete pulse, while ACT
        # tics set the total live duration (delivery plus any remaining
        # settle/hover). The (buttons, tics) contract therefore stays shared
        # by Code and Lang.
        clicks = [s.click for s in specs if s.click is not None]
        clicks.extend(resolved.clicks)
        right_clicks = list(resolved.right_clicks)
        middle_clicks = list(resolved.middle_clicks)
        double_clicks = list(resolved.double_clicks)
        moves = list(resolved.moves)
        relative_moves = list(resolved.relative_moves)
        click_current = resolved.click_current
        drags = list(resolved.drags)
        scrolls = list(resolved.scrolls)
        # Text-targeted actions resolve their coordinate from the CURRENT
        # screen, so one stable token can drive a control that moves —
        # Balatro's blind "Select" sits in a different column for each blind
        # of an ante, which stalled the reference policy on the second one.
        text_targets = [s for s in specs if s.click_text]
        pointer_delivery_s = 0.0
        pointer_kind = (
            "right_click" if right_clicks else
            "middle_click" if middle_clicks else
            "double_click" if double_clicks else
            "move" if moves else
            "relative_move" if relative_moves else
            "click_current" if click_current else
            "drag" if drags else
            "scroll" if scrolls else "click"
        )
        live_action = None
        if (clicks or right_clicks or middle_clicks or double_clicks or moves or
                relative_moves or click_current or drags or scrolls or text_targets):
            # The callback runs only after Env.step has resumed the game. A
            # pointer event delivered while NtSuspend'd is consumed too late.
            def deliver_pointer_action() -> None:
                nonlocal pointer_delivery_s
                started = time.perf_counter()
                # Resolve text targets on a LIVE frame. render() serves the
                # cached pre-freeze frame while the game is suspended, so
                # doing this before the unpause searched whatever was on
                # screen when the freeze landed — often a transition — and
                # "no on-screen text matching 'Select'" was reported on a
                # blind-selection screen that plainly showed it. Third time
                # this exact shape has bitten: clicks delivered to a frozen
                # process did nothing, waiting for a frozen screen to change
                # never returned, and now reading a frozen screen read the
                # past. Anything that must see the game as it IS has to run
                # unfrozen.
                for s in text_targets:
                    pt = self._locate_text(s.click_text)
                    if pt is None:
                        print(f"[wge] action {s.name!r}: no on-screen text "
                              f"matching {s.click_text!r}; skipping the click")
                        continue
                    clicks.append(pt)
                if clicks:
                    self._do_clicks(clicks)
                elif right_clicks:
                    self._do_right_clicks(right_clicks)
                elif middle_clicks:
                    self._do_middle_clicks(middle_clicks)
                elif double_clicks:
                    self._do_double_clicks(double_clicks)
                elif moves:
                    # MOVE deliberately does not park: the pointer remains at
                    # the target for the rest of the ACT and the final frame
                    # exposes any hover-dependent UI to the policy.
                    self._do_moves(moves)
                elif relative_moves:
                    self._do_relative_moves(relative_moves)
                elif click_current:
                    self._do_click_current()
                elif drags:
                    self._do_drags(drags, requested_duration)
                elif scrolls:
                    self._do_scrolls(scrolls)
                pointer_delivery_s = time.perf_counter() - started
            # Delivery is measured inside the live pulse below. If it exceeds
            # the requested duration, the actual time is charged after step.
            live_action = deliver_pointer_action
            specs = [s for s in specs
                     if s.click is None and not s.click_text]
        # ``state`` came from resolve_raw_state so parameterised controls are
        # preserved exactly.  Click/text actions have already been removed
        # from the game-driving list; the resolver still included any fixed
        # non-click tokens alongside dynamic controls.
        duration = requested_duration
        sample_work_s = 0.0
        passive_sample_tics = []
        score_interval = (self._passive_score_interval_tics / self.TICRATE
                          if self._passive_score_interval_tics is not None else None)
        next_score_sample = score_interval
        running_before = self._game_running_seconds
        self._action_id += 1
        scan_v2 = self.stepped_terminal_sampling == 'during_action_v2'
        scan_during_action = (self.terminal_detection and
                             self.stepped_terminal_sampling in {'during_action_v1', 'during_action_v2'})
        scan_interval = (self.spec.get('lifecycle') or {}).get('terminal_scan_tics', 2) / self.TICRATE
        last_scan = time.perf_counter()

        def should_stop():
            return (getattr(self, "_action_halted", False)
                    or (scan_during_action and self._done)
                    or self._save_verification_ready())

        def consume_frame(pil_frame):
            nonlocal sample_work_s, last_scan
            started = time.perf_counter()
            try:
                if scan_during_action and (scan_v2 or started - last_scan >= scan_interval):
                    last_scan = started
                    self._check_terminal(_to_bgr(pil_frame), source='held_action_sample')
                return should_stop()
            finally:
                sample_work_s += time.perf_counter() - started

        def preserve_score_sample(pil_frame, elapsed):
            nonlocal next_score_sample, sample_work_s
            if elapsed + 1e-9 < next_score_sample:
                return
            started = time.perf_counter()
            try:
                sample_tic = math.floor((running_before + elapsed) * self.TICRATE + .5)
                self._queue_score(_conform(_to_bgr(pil_frame), self._render_size), tics=sample_tic)
                passive_sample_tics.append(sample_tic)
                # Missed slots stay missing; never fabricate frames or catch up.
                next_score_sample += (max(1, math.floor((elapsed - next_score_sample) / score_interval) + 1)
                                      * score_interval)
            finally:
                sample_work_s += time.perf_counter() - started

        t0 = time.perf_counter()
        # Default samples only preserve pixels. Explicit archived protocols may
        # detect a registered terminal within this same unsplit input hold.
        # Keep the registered in-action detector's capture cadence.
        interval = 1.0 / self.TICRATE if scan_during_action else 0.
        live_kwargs = ({"live_action": live_action}
                       if live_action is not None else {})
        if score_interval is not None:
            interval = min(interval, score_interval) if interval else score_interval
            live_kwargs["on_sample"] = preserve_score_sample
        if self._controller is not None:
            live_kwargs["before_input"] = (
                lambda: self._controller.prepare_input_foreground(self._env))
        try:
            pil = self._env.step(**state, duration_seconds=duration,
                                 on_frame=consume_frame if scan_during_action else None,
                                 should_stop=should_stop,
                                 sample_interval=interval,
                                 **live_kwargs)
        except BaseException:
            timing = getattr(self._env, "last_step_timing", None)
            if timing is not None:
                self._charge_running_time(timing.live_window_s)
            raise
        wall = time.perf_counter() - t0
        step_timing = getattr(self._env, "last_step_timing", None)
        timing_matches = (
            step_timing is not None and
            math.isclose(
                float(getattr(step_timing, "requested_s", -1.0)),
                requested_duration, rel_tol=0.0, abs_tol=1e-6)
        )
        live_window_s = (
            float(step_timing.live_window_s) if timing_matches else wall)
        step_overhead_s = max(0.0, wall - live_window_s)
        interrupted = timing_matches and getattr(step_timing, "interrupted", False)
        # Injected test adapters may not expose timing. Production Env always does.
        running_s = live_window_s if timing_matches else max(requested_duration, pointer_delivery_s)
        tic_before = self._tic
        self.last_action_timing = dict(
            requested_seconds=requested_duration, game_running_seconds=running_s,
            driver_wall_seconds=wall, interrupted=bool(interrupted),
            timing_source=(getattr(step_timing, "timing_source", "host_pause_control_boundaries")
                           if timing_matches else "unmeasured_adapter_duration"),
            control_transition_seconds=getattr(step_timing, "control_transition_s", None),
        )
        if (live_window_s >
                requested_duration * (1 + TIMING_WARN_FRAC) + TIMING_GRACE_S):
            self._timing_warned += 1
            pointer_detail = (
                f", {pointer_kind} delivery {pointer_delivery_s:.2f}s"
                if live_action is not None else "")
            print(f"[wge] act timing: asked {requested_duration:.2f}s "
                  f"live {live_window_s:.2f}s, wall {wall:.2f}s"
                  f"{pointer_detail} "
                  f"(warn #{self._timing_warned})")
        self._charge_running_time(running_s)
        charged_tics = self._tic - tic_before
        observation_refresh_s = 0.0
        if self._post_action_observation_delay_seconds:
            refresh_started = time.perf_counter()
            time.sleep(self._post_action_observation_delay_seconds)
            pil = self._env.refresh_frozen_frame()
            observation_refresh_s = time.perf_counter() - refresh_started
        frame = _conform(_to_bgr(pil), self._render_size)
        self._last_frame = frame
        if not scan_during_action:
            self._queue_score(frame)
        self._check_terminal(frame, source='frozen_action_frame')
        if self._done and scan_during_action:
            for sample in self._terminal_evidence:
                if sample['source'] == 'held_action_sample':
                    self._queue_score(sample['frame'])
        if scan_during_action:
            self._queue_score(frame)
        self._consume_save_verification()
        reward = None
        emit_windows_event(
            "windows_env_act", "end", episode_id=self._episode_id,
            tic_after=self._tic, buttons=toks, charged_tics=charged_tics,
            requested_tics=requested_tics,
            requested_duration_s=round(requested_duration, 6),
            action_wall_s=round(wall, 6),
            live_window_s=round(live_window_s, 6),
            step_overhead_s=round(step_overhead_s, 6),
            interrupted=bool(interrupted), sample_work_s=round(sample_work_s, 6),
            passive_score_interval_tics=self._passive_score_interval_tics,
            passive_score_sample_tics=passive_sample_tics,
            terminal_detection=('registered_terminal' if self.terminal_detection else 'disabled'),
            game_running_seconds=running_s,
            game_running_seconds_total=self._game_running_seconds,
            timing_source=self.last_action_timing["timing_source"],
            control_transition_seconds=self.last_action_timing["control_transition_seconds"],
            click_delivery_s=(round(pointer_delivery_s, 6)
                              if pointer_kind == "click" and
                              live_action is not None else None),
            move_delivery_s=(round(pointer_delivery_s, 6)
                             if pointer_kind == "move" and
                             live_action is not None else None),
            pointer_kind=(pointer_kind if live_action is not None else None),
            pointer_delivery_s=(round(pointer_delivery_s, 6)
                                if live_action is not None else None),
            observation_refresh_s=round(observation_refresh_s, 6),
            reward=reward, total_reward=self.total_reward, done=self._done,
            end_reason=self.end_reason)
        return reward

    # -- state queries (infrastructure only) --
    @property
    def done(self) -> bool:
        self._consume_save_verification()
        return self._done

    @property
    def tic(self) -> int:
        return self._tic

    @property
    def game_running_seconds(self) -> float:
        return self._game_running_seconds

    def _charge_running_time(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise RuntimeError("invalid measured game running time")
        self._game_running_seconds = math.fsum((self._game_running_seconds, seconds))
        self._tic = math.floor(self._game_running_seconds * self.TICRATE + .5)

    @property
    def total_reward(self) -> float | None:
        if getattr(self, "_score_breakdown", None) is not None:
            return self._total
        pending = getattr(self, '_deferred_score', None)
        if pending is None:
            return None
        return self._total if pending.settled else pending.value

    def _queue_score(self, frame, *, tics=None):
        if getattr(self, 'stepped_terminal_sampling', None) == 'during_action_v2':
            # Settlement must use the independently confirmed native capture.
            return
        # Durable save state is the complete scoring evidence for these tasks.
        # Do not retain or later OCR gameplay frames merely to overwrite that
        # visual score with the authoritative verifier result.
        if self._save_verifier is not None:
            return
        from coding4game_gui.benchmark.windows.deferred_score import DeferredScore
        if getattr(self, '_deferred_score', None) is None:
            self._deferred_score = DeferredScore(
                self._scorer, retain_replay_evidence=self._passive_score_interval_tics is not None)
        self._deferred_score.add(frame, tics=self._tic if tics is None else tics)

    def finalize_score(self) -> float | None:
        """Settle once after gameplay has stopped, using the observed sequence."""
        from coding4game_gui.benchmark.windows.deferred_score import DeferredScore
        observer = getattr(self, '_process_observer', None)
        if observer is not None and self._score_breakdown is not None:
            return self._total
        monitor = getattr(self, '_terminal_monitor', None)
        if monitor is not None:
            try:
                monitor.close()
            except BaseException:
                if observer is not None:
                    observer.result()
                raise
        if self._save_verifier is not None:
            self._save_verifier.final_check()
            self._consume_save_verification()
            progress = getattr(self._save_verifier, "progress", None)
            if self._verified_completion is not None:
                progress = self._verified_completion
            elif progress is not None and self._first_capture_tic is not None:
                progress = dict(
                    progress, first_capture_tic=self._first_capture_tic)
            scorer_spec = self.spec.get("scorer") or {}
            scored_tic = (self._completion_tic
                          if self._completion_tic is not None else self._tic)
            self._total, self._score_breakdown = capture_progress_score(
                progress, scored_tic, scorer_spec)
            if progress is not None:
                self._score_breakdown["save_verification"] = dict(progress)
            return self._total
        if getattr(self, '_deferred_score', None) is None:
            self._deferred_score = DeferredScore()
            if monitor is not None and monitor.confirmed_frame is not None:
                self._deferred_score.add(monitor.confirmed_frame['frame'])
        try:
            raw_score = self._deferred_score.settle(self._scorer)
        except BaseException:
            if observer is not None:
                observer.result()
            raise
        if observer is not None:
            from coding4game_gui.benchmark.windows.scorers.overcooked_process import breakdown
            import hashlib
            reading = self._deferred_score.details.get('reading')
            completed = None if reading is None else reading.get('completed_orders')
            process, evidence = observer.result(completed)
            if completed is not None and completed > 0:
                evidence[0].update(source='confirmed_result_page', image='selected.png',
                    selected_scoring_frame_index=self._deferred_score.details['selected_frame_index'],
                    rect=list(self._scorer.spec['result']['anchors'][1]['rect']),
                    sha256=hashlib.sha256(self._deferred_score.selected_png).hexdigest())
            self._milestone_evidence = evidence
            self._score_breakdown = breakdown(
                None if reading is None else reading['net_score'],
                self._scorer.spec['three_star_threshold'], process,
                valid_result=reading is not None)
            self._total = self._score_breakdown['combined_score']
            self._deferred_score.value = self._total
            self._deferred_score.details.update(score_breakdown=self._score_breakdown,
                                                milestone_evidence=evidence)
            return self._total
        self._total, self._score_breakdown = raw_score, None
        return self._total

    @property
    def score_breakdown(self):
        return None if self._score_breakdown is None else dict(self._score_breakdown)

    @property
    def milestone_evidence(self):
        return list(self._milestone_evidence)

    def save_score_evidence(self, directory):
        """Persist settlement metadata and the selected lossless snapshot."""
        pending = getattr(self, '_deferred_score', None)
        if ((pending is None or not pending.details)
                and self._score_breakdown is None
                and not getattr(self, '_terminal_evidence', [])):
            return None
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        details = dict(pending.details or {}) if pending is not None else {}
        if pending is not None and pending.selected_png is not None:
            (directory / 'selected.png').write_bytes(pending.selected_png)
            details['image'] = 'selected.png'
        endpoint = getattr(pending, 'endpoint_png', None)
        if endpoint is not None:
            (directory / 'endpoint.png').write_bytes(endpoint)
            details['endpoint_image'] = 'endpoint.png'
        if self._score_breakdown is not None:
            details['score_breakdown'] = dict(self._score_breakdown)
        observer = getattr(self, '_process_observer', None)
        if observer is not None:
            details['milestone_evidence'] = observer.save(directory, self._milestone_evidence)
        monitor = getattr(self, '_terminal_monitor', None)
        if monitor is not None:
            import os
            details['terminal_v2'] = monitor.summary()
            try:
                evidence_ref = os.path.relpath(monitor.writer.directory, directory)
            except ValueError:
                # Windows cannot make a relative path across volumes. Preserve
                # the finalized native evidence beside its settlement metadata.
                import shutil
                copied = directory / 'terminal_v2'
                shutil.copytree(monitor.writer.directory, copied, dirs_exist_ok=True)
                evidence_ref = copied.name
            details['terminal_v2']['evidence_directory'] = evidence_ref
        terminal = getattr(self, '_terminal_evidence', [])
        if terminal:
            import cv2
            native_frames = []
            for index, sample in enumerate(terminal):
                filename = f'terminal_{index:02d}.png'
                if not cv2.imwrite(str(directory / filename), sample['frame']):
                    raise RuntimeError('could not preserve native terminal frame')
                native_frames.append({k: v for k, v in sample.items() if k != 'frame'} |
                                     dict(image=filename, shape=list(sample['frame'].shape)))
            details['terminal_confirmation'] = dict(
                sampling=self.stepped_terminal_sampling, end_reason=self.end_reason,
                charged_tics=self._tic, frames=native_frames)
        import json
        (directory / 'settlement.json').write_text(
            json.dumps(details, indent=2), encoding='utf-8')
        return details

_MOUSE_DRIVERS: dict = {}


def _click_game_points(process: str, env, points,
                       content_size=None, *, atomic: bool = False,
                       coordinate_size=None,
                       require_game_target: bool = False) -> None:
    """Click GAME-SPACE points, reusing one MouseDriver per process.

    Shared by the act path and the reset/menu_skip path — both need it, and
    the driver caches a window handle, so they must not each build their own.

    Raises rather than returning False: mouse injection silently no-ops when
    the bench runs as SYSTEM (the session-isolation wall) or while the
    desktop is locked, and a quiet failure there is indistinguishable from a
    policy that simply scores nothing — a confusion that has already cost a
    day of debugging.
    """
    from coding4game_gui.benchmark.windows.mouse_driver import MouseDriver

    if not process:
        raise RuntimeError("click actions need the driver config's `process`")
    key = process.lower()
    drv = _MOUSE_DRIVERS.get(key)
    if drv is None:
        drv = MouseDriver(process.rsplit(".", 1)[0],
                          content_size or _frame_size(env))
        drv.focus()
        _MOUSE_DRIVERS[key] = drv
    for gx, gy in points:
        kwargs = ({"move_settle": 0.02, "button_hold": 0.03,
                   "settle": 0.0}
                  if atomic else {})
        if not drv.click(
                gx, gy, coordinate_size=coordinate_size,
                require_game_target=require_game_target, **kwargs):
            raise RuntimeError(
                f"mouse click at ({gx}, {gy}) failed — no game window. "
                "Mouse actions need an INTERACTIVE, UNLOCKED session; a bench "
                "started via PsExec as SYSTEM cannot inject clicks.")
    if not atomic:
        # Navigation callers keep the conservative click settle and park.
        drv.park_cursor()


def _click_game_current(process: str, env, *, atomic: bool = False) -> None:
    """Press the captured game's left mouse button without cursor movement."""
    from coding4game_gui.benchmark.windows.mouse_driver import MouseDriver

    key = process.lower()
    drv = _MOUSE_DRIVERS.get(key)
    if drv is None:
        drv = MouseDriver(process.rsplit(".", 1)[0], _frame_size(env))
        _MOUSE_DRIVERS[key] = drv
    options = dict(button_hold=0.0, require_game_target=True) if atomic else {}
    if not drv.click_current(**options):
        raise RuntimeError(f"mouse click_current failed for {process}")


def _type_game_text(text: str) -> None:
    """Enter a recorded ASCII search query into the focused game field."""
    from coding4game_gui.benchmark.windows.drivers.windows_kbm.input import type_startup_text

    type_startup_text(text)


def _move_game_points(process: str, env, points, content_size=None, *,
                      coordinate_size=None) -> None:
    """Move to GAME-SPACE points and leave the pointer at the final target."""
    from coding4game_gui.benchmark.windows.mouse_driver import MouseDriver

    if not process:
        raise RuntimeError("MOVE actions need the driver config's `process`")
    key = process.lower()
    drv = _MOUSE_DRIVERS.get(key)
    if drv is None:
        drv = MouseDriver(process.rsplit(".", 1)[0],
                          content_size or _frame_size(env))
        drv.focus()
        _MOUSE_DRIVERS[key] = drv
    if not drv.ensure_restored():
        raise RuntimeError(
            "mouse move failed — no game window. MOVE needs an INTERACTIVE, "
            "UNLOCKED session")
    for gx, gy in points:
        if not drv.move(gx, gy, coordinate_size=coordinate_size):
            raise RuntimeError(
                f"mouse move to ({gx}, {gy}) failed. MOVE needs an "
                "INTERACTIVE, UNLOCKED session")


def _relative_move_game(process: str, env, deltas, content_size=None) -> None:
    """Inject relative device deltas without converting screenshot coordinates."""
    from coding4game_gui.benchmark.windows.mouse_driver import MouseDriver

    if not process:
        raise RuntimeError("RMOVE actions need the driver config's `process`")
    key = process.lower()
    drv = _MOUSE_DRIVERS.get(key)
    if drv is None:
        drv = MouseDriver(
            process.rsplit(".", 1)[0], content_size or _frame_size(env))
        drv.focus()
        _MOUSE_DRIVERS[key] = drv
    for dx, dy in deltas:
        if not drv.relative_move(dx, dy):
            raise RuntimeError(
                f"relative mouse move ({dx}, {dy}) failed. RMOVE needs an "
                "INTERACTIVE, UNLOCKED session")


def _policy_mouse_driver(process: str, env, content_size, kind: str):
    """Return the process-scoped driver used by raw policy pointer actions."""
    from coding4game_gui.benchmark.windows.mouse_driver import MouseDriver

    if not process:
        raise RuntimeError(
            f"{kind} actions need the driver config's `process`")
    key = process.lower()
    drv = _MOUSE_DRIVERS.get(key)
    if drv is None:
        drv = MouseDriver(
            process.rsplit(".", 1)[0], content_size or _frame_size(env))
        drv.focus()
        _MOUSE_DRIVERS[key] = drv
    return drv


def _right_click_game_points(process: str, env, points,
                             content_size=None, *, coordinate_size=None,
                             require_game_target: bool = False) -> None:
    """Right-click policy points and leave the pointer at the target."""
    drv = _policy_mouse_driver(process, env, content_size, "RCLICK")
    for gx, gy in points:
        if not drv.right_click(
                gx, gy, move_settle=0.02, button_hold=0.03, settle=0.0,
                coordinate_size=coordinate_size,
                require_game_target=require_game_target):
            raise RuntimeError(
                f"mouse right-click at ({gx}, {gy}) failed. RCLICK needs an "
                "INTERACTIVE, UNLOCKED session")


def _middle_click_game_points(process: str, env, points,
                              content_size=None, *, coordinate_size=None,
                              require_game_target: bool = False) -> None:
    """Middle-click policy points and leave the pointer at the target."""
    drv = _policy_mouse_driver(process, env, content_size, "MCLICK")
    for gx, gy in points:
        if not drv.middle_click(
                gx, gy, move_settle=0.02, button_hold=0.03, settle=0.0,
                coordinate_size=coordinate_size,
                require_game_target=require_game_target):
            raise RuntimeError(
                f"mouse middle-click at ({gx}, {gy}) failed. MCLICK needs an "
                "INTERACTIVE, UNLOCKED session")


def _double_click_game_points(process: str, env, points,
                              content_size=None, *, coordinate_size=None,
                              require_game_target: bool = False) -> None:
    """Double-click policy points and leave the pointer at the target."""
    drv = _policy_mouse_driver(process, env, content_size, "DOUBLE_CLICK")
    for gx, gy in points:
        if not drv.double_click(
                gx, gy, move_settle=0.02, button_hold=0.02,
                inter_click=0.05, settle=0.0,
                coordinate_size=coordinate_size,
                require_game_target=require_game_target):
            raise RuntimeError(
                f"mouse double-click at ({gx}, {gy}) failed. DOUBLE_CLICK "
                "needs an INTERACTIVE, UNLOCKED session")


def _drag_game_paths(process: str, env, drags, content_size=None, *,
                     duration: float, coordinate_size=None,
                     require_game_target: bool = False) -> None:
    """Execute complete left-button drags and leave the pointer at the end."""
    drv = _policy_mouse_driver(process, env, content_size, "DRAG")
    for start, end in drags:
        if not drv.drag(start, end, duration=duration,
                        coordinate_size=coordinate_size,
                        require_game_target=require_game_target):
            raise RuntimeError(
                f"mouse drag from {start} to {end} failed. DRAG needs an "
                "INTERACTIVE, UNLOCKED session")


def _scroll_game_points(process: str, env, scrolls,
                        content_size=None, *, coordinate_size=None,
                        require_game_target: bool = False) -> None:
    """Scroll at policy points and leave the pointer over the target."""
    drv = _policy_mouse_driver(process, env, content_size, "SCROLL")
    for (gx, gy), dx, dy in scrolls:
        if not drv.scroll(gx, gy, dx=dx, dy=dy,
                          coordinate_size=coordinate_size,
                          require_game_target=require_game_target):
            raise RuntimeError(
                f"mouse scroll at ({gx}, {gy}) failed. SCROLL needs an "
                "INTERACTIVE, UNLOCKED session")


def _click_until_changed(process: str, env, point, settle: float = 1.0,
                         retries: int = 6, content_size=None) -> bool:
    """Click, confirm the screen reacted, retry if it did not.

    Unlike an in-episode action, a menu_skip / recovery click can land while
    the game is still loading — right after a relaunch there is a splash and
    a scene transition, and a click into either does nothing. Open-loop, that
    silently derails the whole cold path: the run ends up parked on the title
    screen and start_check fails with no indication of which step was lost.
    (This is the same reason the pad path uses press-until-changed.)
    """
    import numpy as np

    for _ in range(max(1, retries)):
        before = np.asarray(env.render().convert("L"), dtype=np.int16)
        _click_game_points(process, env, [point], content_size)
        time.sleep(max(0.2, settle))
        after = np.asarray(env.render().convert("L"), dtype=np.int16)
        if before.shape == after.shape and \
                float((np.abs(after - before) > 25).mean()) > 0.01:
            return True
        time.sleep(0.8)
    return False


def _click_until_text(process: str, env, point, expect: str, scorer,
                      settle: float = 1.0, retries: int = 6,
                      content_size=None, absent: str | None = None,
                      before_click=None) -> bool:
    """Click until the destination screen SAYS we arrived.

    _click_until_changed asks only "did the screen change", and a screen
    TRANSITION is itself change — so a click landing mid-animation reads as
    success and the sequence marches on from the wrong screen. That is how
    the three-click in-process reset silently derailed: it "ran", left the
    game somewhere unexpected, failed start_check, and fell through to
    full_restart — 6 times in one lang run at ~120s each, and hammering
    Steam that way took the client down once before.

    Verifying the DESTINATION instead makes the sequence self-checking: each
    step knows what it was aiming at.
    """
    verify = getattr(scorer, "check_text", None)
    if verify is None:      # no OCR scorer yet (first boot): best effort
        return _click_until_changed(process, env, point, settle=settle,
                                    retries=retries,
                                    content_size=content_size)

    def _arrived() -> bool:
        try:
            frame = _to_bgr(env.render())
            if absent and verify(frame, absent):
                return False
            return bool(verify(frame, expect)) if expect else True
        except Exception as e:  # A failed observation is not arrival evidence.
            print(f"[wge] expect {expect!r} check failed: "
                  f"{type(e).__name__}: {e}")
            return False

    # CHECK BEFORE CLICKING, and poll while waiting. Clicking first and
    # checking after re-clicks a coordinate that is only meaningful on the
    # screen we started from: menu_skip's (459,780) is PLAY on the main menu
    # but something else on the New Run page, so a first click that WORKED
    # and merely confirmed slowly got "retried" straight back off the
    # destination. The loop was undoing its own success.
    for attempt in range(max(1, retries)):
        if _arrived():
            return True
        # Lazy perception can take long enough for a startup modal to appear
        # after the step-level guard. Recheck immediately before actual input.
        if before_click is not None:
            before_click()
        _click_game_points(process, env, [point], content_size)
        waited = 0.0
        while waited < max(0.4, settle):
            time.sleep(0.4)
            waited += 0.4
            if _arrived():
                return True
    return False


def _press_until_text(env, step, reader, *, retries: int = 8) -> None:
    """Deliver recorded input with bounded retries until its text destination."""
    expect = getattr(step.spec, "expect_text", None)
    absent = getattr(step.spec, "expect_absent", None)
    verify = getattr(reader, "check_text", None)
    if verify is None:
        raise RuntimeError("menu text press requires an OCR text reader")
    for attempt in range(max(1, retries) + 1):
        try:
            frame = _to_bgr(env.render())
            arrived = ((not absent or not verify(frame, absent))
                       and (not expect or verify(frame, expect)))
        except Exception:
            arrived = False
        if arrived:
            return
        if attempt == max(1, retries):
            break
        retry_hold = getattr(step, "retry_duration_seconds", None)
        hold = (retry_hold if attempt > 0 and retry_hold is not None
                else step.spec.duration_seconds)
        if attempt > 0 and retry_hold is not None:
            print(f"[wge] navigation precision retry {attempt}/{retries}: "
                  f"{hold:.3f}s for {expect or ('not ' + str(absent))!r}",
                  flush=True)
        env.apply_state(step.pad, **step.spec.gamepad_state())
        try:
            time.sleep(hold)
        finally:
            env.apply_state(step.pad, buttons=[], left_stick=(0., 0.),
                            right_stick=(0., 0.), left_trigger=0., right_trigger=0.)
        time.sleep(0.6)
    raise RuntimeError(
        f"nav press never reached {expect or ('not ' + str(absent))!r}")


def _wait_until_text(env, expect: str | None, scorer, *, settle: float,
                     absent: str | None = None, retries: int = 6,
                     min_scene_luma: float | None = None) -> bool:
    """Preserve a recorded wait, then verify its destination without input."""
    verify = getattr(scorer, "check_text", None)
    if verify is None:
        raise RuntimeError("menu text wait requires an OCR text reader")
    duration = max(0.0, float(settle))
    deadline = time.monotonic() + max(.4, duration) * max(1, retries)
    time.sleep(duration)
    last_error = None
    while True:
        try:
            frame = _to_bgr(env.render())
            scene_ready = (min_scene_luma is None or
                           float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean()) >= min_scene_luma)
            if scene_ready and (not absent or not verify(frame, absent)) and (
                    not expect or verify(frame, expect)):
                return True
        except Exception as exc:  # Observation failure cannot authorize input.
            last_error = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if last_error is not None:
                print(f"[wge] menu wait observation failed: {last_error}")
            return False
        time.sleep(min(.4, remaining))


def _client_size_of(process: str):
    """Live client-area size of the game window, or None."""
    try:
        import win32gui

        from coding4game_gui.benchmark.windows.mouse_driver import find_game_window
        hwnd = find_game_window(process.rsplit(".", 1)[0])
        if not hwnd:
            return None
        _, _, width, height = win32gui.GetClientRect(hwnd)
        return ((int(width), int(height))
                if width > 0 and height > 0 else None)
    except Exception:  # noqa: BLE001 — geometry probing must not break boot
        return None


def _wait_for_stable_geometry(process: str, timeout: float = 40.0,
                              stable_for: float = 3.0,
                              interval: float = 0.5):
    """Wait until the game client stops changing size before renderer init."""
    deadline = time.time() + timeout
    last = _client_size_of(process)
    steady_since = time.time()
    while time.time() < deadline:
        time.sleep(interval)
        current = _client_size_of(process)
        if current != last:
            print(f"[wge] window geometry still moving: {last} -> {current}")
            last, steady_since = current, time.time()
            continue
        if current is not None and time.time() - steady_since >= stable_for:
            print(f"[wge] window geometry stable at "
                  f"{current[0]}x{current[1]}")
            return current
    print(f"[wge] window geometry never settled within {timeout}s "
          f"(last {last}) — continuing anyway")
    return last


def _frame_size(env):
    """Size of the frames the renderer actually EMITS.

    Scorer rects are in frame coordinates, so this — not the window
    bbox — is what a calibration must match. The two differ whenever the
    renderer crops: the client-area crop (bordered windows) or the
    DPI-unaware crop (a LOVE2D-style game rendering at logical size
    inside a physical-pixel window rect, e.g. Balatro 1536x864 in a
    1920x1080 rect under 125% scaling).
    """
    renderer = getattr(env, 'renderer', None)
    emitted_size = getattr(renderer, 'frame_size', None)
    if emitted_size is not None:
        return tuple(emitted_size)
    crop = getattr(renderer, '_crop', None)
    if crop:
        left, top, right, bottom = crop
        return (int(right - left), int(bottom - top))
    return (env.window_width, env.window_height)

class ResetController:
    """Cold-start controller for one Windows benchmark episode at a time."""

    def __init__(self, bench_spec: dict, *, platform: str = "auto"):
        from coding4game_gui.benchmark.windows.games_config import load_game_config
        self.spec = bench_spec
        self.tics_per_second = resolve_tics_per_second(
            bench_spec.get("runtime"))
        self.cfg = load_game_config(bench_spec["driver_game"])
        self.startup_spec = bench_spec.get("startup") or {}
        self._start_check = self.startup_spec.get("start_check")
        image_checks = (self._start_check or {}).get("image_templates")
        self._start_images = None
        if image_checks is not None:
            from coding4game_gui.benchmark.windows.startup_images import StartupImageChecks
            self._start_images = StartupImageChecks(
                image_checks, _calibrated_size(bench_spec))
        self._menu_skip_preserve_tail = self.startup_spec.get(
            "menu_skip_preserve_tail", True)
        if not isinstance(self._menu_skip_preserve_tail, bool):
            raise ValueError("startup.menu_skip_preserve_tail must be true or false")
        self._focus_alt_key = self.startup_spec.get("focus_alt_key", True)
        if not isinstance(self._focus_alt_key, bool):
            raise ValueError("startup.focus_alt_key must be true or false")
        self._menu_skip_park_cursor = self.startup_spec.get("menu_skip_park_cursor", False)
        if not isinstance(self._menu_skip_park_cursor, bool):
            raise ValueError("startup.menu_skip_park_cursor must be true or false")
        self._menu_skip_replay = str(
            self.startup_spec.get("menu_skip_replay", "closed_loop")
        ).strip().lower()
        if self._menu_skip_replay not in ("closed_loop", "open_loop"):
            raise ValueError(
                "startup.menu_skip_replay must be 'closed_loop' or 'open_loop'"
            )
        rapid_tail = self.startup_spec.get("menu_skip_open_loop_tail", 0)
        if isinstance(rapid_tail, bool):
            raise ValueError("startup.menu_skip_open_loop_tail must be an integer")
        self._menu_skip_open_loop_tail = int(rapid_tail)
        if self._menu_skip_open_loop_tail < 0:
            raise ValueError("startup.menu_skip_open_loop_tail must be nonnegative")
        self.platform = ((getattr(self.cfg, "platform", None) or platform)
                         if platform == "auto" else platform)
        self._env = None
        from coding4game_gui.benchmark.windows.scorers.factory import ScorerFactory
        self._scorer_factory = ScorerFactory()
        self._nav_scorer = None        # text reader for nav checks,
        # built during boot when the run's scorer does not exist yet
        self._scorer = None            # last scorer made (start_ok needs it)
        # Crash-safe: un-freeze the game on any catchable interpreter exit so a
        # fused / crashed run never strands it NtSuspendProcess'd for the next.
        _install_exit_cleanup(self.cfg.process)

    # -- wiring for the adapter --
    def action_table(self) -> dict:
        return build_action_table(self.cfg.actions)

    def nav_verifier(self, env):
        """A text reader for navigation checks, available DURING boot.

        menu_skip runs inside _boot, but make_scorer is called afterwards —
        so `self._scorer` was None exactly when the cold path needed it, and
        every `expect` silently degraded to change-detection. That is the one
        place the closed loop matters most (a mid-transition click during a
        relaunch is precisely what it exists to catch), and the failure was
        reported as "never reached <text>", which sent this investigation
        chasing OCR readability and click coordinates for two rounds. The
        verification never ran at all.

        Deliberately SEPARATE from the run's scorer: building that early
        would reset scoring state that the episode depends on. This one only
        ever answers "is this text on screen".
        """
        nav_rect = (getattr(self, "startup_spec", {}) or {}).get("menu_skip_ocr_rect")
        nav_ocr = (getattr(self, "startup_spec", {}) or {}).get("menu_skip_ocr")
        if nav_ocr is not None and not isinstance(nav_ocr, dict):
            raise ValueError("startup.menu_skip_ocr must be a mapping")
        if self._scorer is not None and nav_rect is None and nav_ocr is None:
            return self._scorer
        if self._nav_scorer is not None:
            return self._nav_scorer
        sc = self.spec["scorer"]
        if sc.get("kind", "ocr") != "ocr" and nav_ocr is None:
            return None            # non-OCR scorers cannot answer text checks
        if nav_ocr is not None:
            sc = {**sc, "ocr": dict(nav_ocr)}
        if nav_rect is not None:
            if (not isinstance(nav_rect, (list, tuple)) or len(nav_rect) != 4
                    or any(isinstance(v, bool) or not isinstance(v, (int, float))
                           or not math.isfinite(v) for v in nav_rect)
                    or nav_rect[2] <= 0 or nav_rect[3] <= 0):
                raise ValueError("startup.menu_skip_ocr_rect must be a finite [x,y,w,h] rectangle")
            sc = {**sc, "ocr": {**(sc.get("ocr") or {}), "rect": list(nav_rect)}}
        self._nav_scorer = self._scorer_factory.create_text_reader(
            sc, client_size=_frame_size(env))
        return self._nav_scorer

    def make_scorer(self, env):
        sc = self.spec["scorer"]
        kind = sc.get("kind") or getattr(self.cfg, "scorer_kind", None)
        implementation = (sc.get("implementation") or
                          getattr(self.cfg, "scorer_implementation", None))
        self._scorer = self._scorer_factory.create(
            spec=sc,
            client_size=_frame_size(env),
            kind=kind,
            implementation=implementation,
        )
        return self._scorer

    def prepare_policy_handoff(self, env) -> bool:
        """Validate tic-0 and leave the game frozen for policy inference.

        Full verification, scorer calibration, and formal episodes call this
        same boundary.  Scorer work and waiting happen only while the target
        process is hard-paused; the game advances solely in one-control-tic
        pulses until the first valid start frame appears.
        """
        if self._scorer is None:
            self.make_scorer(env)
        self._scorer.reset()
        timeout = float((self._start_check or {}).get("timeout_seconds", 20.0))
        if timeout < 0 or not math.isfinite(timeout):
            raise ValueError(
                "startup.start_check.timeout_seconds must be finite and nonnegative")
        return self.wait_start_ok(env, timeout=timeout)

    # -- boot / attach --
    def ensure_env(self):
        if self._env is None:
            with windows_event_span(
                    "game_environment_boot", process=self.cfg.process,
                    driver_game=self.spec.get("driver_game")):
                self._env = self._boot()
        return self._env

    def _drain_by_name(self) -> None:
        """Pre-make_env drain: resume the game by PROCESS NAME (no env yet) until
        its suspend count is fully drained — a prior run killed mid-pause() left
        it NtSuspendProcess'd, and xspeedhack injection / renderer probing hang
        against a suspended process."""
        resume_game_by_name(self.cfg.process)

    def _drain_suspension(self, env) -> None:
        """A previous eval process may have left the game NtSuspendProcess'd
        (suspend counts belong to the TARGET process and survive the
        suspender's death). Resume until drained so this process's
        pause/unpause stay balanced. Resuming a running process is a no-op."""
        pid = getattr(env, "game_pid", None)
        if pid is None:
            return
        try:
            from coding4game_gui.benchmark.windows.drivers.windows_xinput.proc_freeze import resume_process
        except Exception:  # noqa: BLE001 — non-windows / fake driver
            return
        for _ in range(16):
            try:
                resume_process(pid)
            except Exception:  # noqa: BLE001
                break

    def _boot(self):
        from coding4game_gui.benchmark.windows import save_state as ss
        from coding4game_gui.benchmark.windows.startup import prepare_launch_state, enforce_live_window
        from coding4game_gui.benchmark.windows.auto_nav import (
            start_auto_launch_session, force_foreground, wait_for_stable_game_window)

        # Mouse drivers cache HWNDs. A cold start owns a new window generation.
        _MOUSE_DRIVERS.pop(self.cfg.process.lower(), None)

        def prepare():
            with windows_event_span(
                    "launch_state_prepare", process=self.cfg.process,
                    golden_checkpoint=self.startup_spec.get("golden_checkpoint")):
                prepare_launch_state(self.spec, self.cfg)

        def clear_orphans(attempt):
            emit_windows_event("launch_attempt_cleanup", "begin",
                               process=self.cfg.process, attempt=attempt)
            ss.kill_process(self.cfg.process)
            time.sleep(2.0)
            emit_windows_event("launch_attempt_cleanup", "end",
                               process=self.cfg.process, attempt=attempt)

        def post_settle(env, hwnd):
            # Match the TUI's post-settle capture barrier before recorded input.
            if not self._force_foreground(env):
                raise RuntimeError("could not focus game before startup capture")
            time.sleep(0.5)
            import asyncio
            asyncio.run(env.render_locked())

        env, _hwnd, _title = start_auto_launch_session(
            self.cfg,
            before_launch=prepare,
            after_window=lambda hwnd: enforce_live_window(self.spec, self.cfg, hwnd),
            post_settle=post_settle,
            menu_skip_replayer=self.replay_menu_skip,
            platform_name=self.platform,
            env_fps=self.tics_per_second,
            use_speedhack=bool(getattr(self.cfg, "use_speedhack", False)),
            attempts=3,
            on_attempt_failed=clear_orphans,
            window_ok=wait_for_stable_game_window,
        )
        if not self.cfg.menu_skip:
            env.hard_pause = True
            env.pause()
        return env

    # -- watchdog --
    def alive(self) -> bool:
        pid = getattr(self._env, "game_pid", None) if self._env else None
        if pid is None:
            return False
        # exit-code check, not table presence: a ZOMBIE (terminated process
        # held in the table by a stale handle) must count as dead so the
        # watchdog must treat it as dead so the next episode cold-starts.
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFO
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == 259                  # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:  # noqa: BLE001 — non-windows / fake driver tests
            import psutil
            return psutil.pid_exists(pid)

    def shutdown(self) -> None:
        from coding4game_gui.benchmark.windows import save_state as ss
        pid = getattr(self._env, "game_pid", None) if self._env else None
        emit_windows_event(
            "game_shutdown", "begin", process=self.cfg.process, game_pid=pid,
            alive_before=self.alive(), source="ResetController.shutdown")
        if self._env is not None:
            try:
                self._env.unpause()
            except Exception:  # noqa: BLE001
                pass
            if getattr(self._env, '_owned_ime_context', None) is not None:
                from coding4game_gui.benchmark.windows.drivers.windows_xinput.ime_context import (
                    restore_owned_ime_context)
                try:
                    restore_owned_ime_context(self._env)
                except Exception as error:  # normal owned destruction remains mandatory
                    emit_windows_event('owned_ime_context', 'end', operation='restore',
                                       status='failed_before_owned_destruction', error=str(error))
            try:
                self._env.close()
            except Exception:  # noqa: BLE001
                pass
        ss.kill_process(self.cfg.process)
        _MOUSE_DRIVERS.pop(self.cfg.process.lower(), None)
        self._env = None
        self._scorer = None
        emit_windows_event(
            "game_shutdown", "end", process=self.cfg.process, game_pid=pid,
            source="ResetController.shutdown")

    def cold_restart(self):
        with windows_event_span(
                "cold_restart", process=self.cfg.process,
                source="start_check_retry"):
            self.shutdown()
            return self.ensure_env()

    def start_ok(self, frame) -> bool:
        if not self._start_check:
            return True
        kind = self._start_check.get('kind')
        if kind not in (None, 'text', 'image', 'scorer'):
            raise ValueError('unsupported startup.start_check.kind')
        if kind == 'scorer':
            checker = getattr(self._scorer, 'check_start', None)
            if not callable(checker):
                raise RuntimeError('scorer start_check requires check_start')
            if not checker(frame):
                return False
        image_checks = getattr(self, "_start_images", None)
        if image_checks is not None and not image_checks.matches(frame):
            return False
        if self._start_check.get("track_identity", False):
            scorer = self._scorer
            reading = scorer.read(frame)
            if scorer.track_identity(reading) != "ok":
                return False
        pattern = self._start_check.get("pattern")
        reject_pattern = self._start_check.get("reject_pattern")
        if pattern or reject_pattern:
            checker = self._scorer
            if checker is None or not hasattr(checker, "check_text"):
                raise RuntimeError(
                    "text start_check requires a scorer with check_text")
            rect = self._start_check.get("rect")
            join_boxes = self._start_check.get("join_text_boxes", False)
            if not isinstance(join_boxes, bool):
                raise ValueError("startup.start_check.join_text_boxes must be boolean")
            text_options = {"join_text_boxes": True} if join_boxes else {}
            if pattern and not checker.check_text(frame, pattern, rect=rect, **text_options):
                return False
            if (reject_pattern
                    and checker.check_text(frame, reject_pattern, rect=rect, **text_options)):
                return False
        luma = self._start_check.get("mean_luma")
        if luma:
            rect = luma.get("rect")
            if not isinstance(rect, (list, tuple)) or len(rect) != 4:
                raise ValueError("startup.start_check.mean_luma.rect must be [x,y,w,h]")
            calibrated = tuple(
                (self.spec.get("scorer") or {}).get("calibrated_client_size")
                or (frame.shape[1], frame.shape[0]))
            sx = frame.shape[1] / calibrated[0]
            sy = frame.shape[0] / calibrated[1]
            x, y, w, h = rect
            x1 = max(0, min(frame.shape[1], int(round(x * sx))))
            y1 = max(0, min(frame.shape[0], int(round(y * sy))))
            x2 = max(x1, min(frame.shape[1], int(round((x + w) * sx))))
            y2 = max(y1, min(frame.shape[0], int(round((y + h) * sy))))
            if x2 <= x1 or y2 <= y1:
                raise ValueError(
                    "startup.start_check.mean_luma.rect is outside the frame")
            crop = frame[y1:y2, x1:x2]
            mean = float(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).mean())
            minimum = luma.get("min")
            maximum = luma.get("max")
            if minimum is not None and mean < float(minimum):
                print(f"[wge] start_check: mean luma {mean:.2f} below "
                      f"minimum {float(minimum):.2f}")
                return False
            if maximum is not None and mean > float(maximum):
                print(f"[wge] start_check: mean luma {mean:.2f} above "
                      f"maximum {float(maximum):.2f}")
                return False
        hsv_fraction = self._start_check.get("hsv_fraction")
        if hsv_fraction:
            rect = hsv_fraction.get("rect")
            if not isinstance(rect, (list, tuple)) or len(rect) != 4:
                raise ValueError(
                    "startup.start_check.hsv_fraction.rect must be [x,y,w,h]")
            calibrated = tuple(
                (self.spec.get("scorer") or {}).get("calibrated_client_size")
                or (frame.shape[1], frame.shape[0]))
            sx = frame.shape[1] / calibrated[0]
            sy = frame.shape[0] / calibrated[1]
            x, y, w, h = rect
            x1 = max(0, min(frame.shape[1], int(round(x * sx))))
            y1 = max(0, min(frame.shape[0], int(round(y * sy))))
            x2 = max(x1, min(frame.shape[1], int(round((x + w) * sx))))
            y2 = max(y1, min(frame.shape[0], int(round((y + h) * sy))))
            if x2 <= x1 or y2 <= y1:
                raise ValueError(
                    "startup.start_check.hsv_fraction.rect is outside the frame")
            ranges = hsv_fraction.get("ranges")
            if not isinstance(ranges, list) or not ranges:
                raise ValueError(
                    "startup.start_check.hsv_fraction.ranges must be non-empty")
            crop = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
            mask = None
            for item in ranges:
                lower = item.get("lower") if isinstance(item, dict) else None
                upper = item.get("upper") if isinstance(item, dict) else None
                if (not isinstance(lower, (list, tuple)) or len(lower) != 3
                        or not isinstance(upper, (list, tuple)) or len(upper) != 3):
                    raise ValueError(
                        "each startup.start_check.hsv_fraction range requires "
                        "three-value lower and upper bounds")
                current = cv2.inRange(
                    crop,
                    tuple(int(value) for value in lower),
                    tuple(int(value) for value in upper),
                )
                mask = current if mask is None else cv2.bitwise_or(mask, current)
            fraction = float(cv2.countNonZero(mask)) / float(mask.size)
            minimum = hsv_fraction.get("min")
            maximum = hsv_fraction.get("max")
            if minimum is not None and fraction < float(minimum):
                print(f"[wge] start_check: HSV pixel fraction {fraction:.4f} below "
                      f"minimum {float(minimum):.4f}")
                return False
            if maximum is not None and fraction > float(maximum):
                print(f"[wge] start_check: HSV pixel fraction {fraction:.4f} above "
                      f"maximum {float(maximum):.4f}")
                return False
        # optional exact-value assertions on scorer regions (e.g. wave: 1) —
        # text checks alone can pass on a leftover mid-run game (Brotato
        # refills HP at every wave start, so full-HP N/N is not unique to a
        # fresh run; a wave-5 leftover slipped through on 2026-07-09)
        for name, expected in (self._start_check.get("regions") or {}).items():
            got = self._scorer.read_region(frame, name)
            if got != int(expected):
                print(f"[wge] start_check: region {name!r} reads {got!r}, "
                      f"expected {expected} — not a fresh run")
                return False
        return True

    def wait_start_ok(self, env, timeout: float = 20.0,
                      interval: float | None = None) -> bool:
        """Reach a validated, settled start frame without a live OCR window.

        The old implementation left the game running for OCR inference and
        1.5-second polling sleeps.  A real-time encounter could damage the
        player before policy tic 0.  Keep the process hard-paused while
        rendering and scoring; when a transition still needs time, advance
        exactly one scenario control tic and freeze again before inspecting.
        A configured readiness dwell counts live pulses after the first match
        and restarts if any subsequent frozen observation fails the conditions.
        ``interval`` remains an override for focused tests/diagnostics.
        """
        import time as _t
        import math
        from coding4game_gui.benchmark.windows.timing import resolve_start_settle_seconds

        pulse_s = (1.0 / self.tics_per_second
                   if interval is None else max(0.0, float(interval)))
        settle_s = resolve_start_settle_seconds(self._start_check)
        if settle_s and pulse_s <= 0:
            raise ValueError("start_check settling requires a positive live pulse")
        required_pulses = math.ceil(settle_s / pulse_s) if settle_s else 0
        matched_at = None
        emit_windows_event(
            "start_check", "begin", process=self.cfg.process,
            game_pid=getattr(env, "game_pid", None), timeout_s=timeout,
            pattern=(self._start_check or {}).get("pattern"),
            live_pulse_s=pulse_s, settle_seconds=settle_s,
            timing_scope="startup_before_policy_tic_zero")
        pulses = 0
        env.hard_pause = True
        env.pause()
        try:
            deadline = _t.monotonic() + max(0.0, float(timeout))
            while True:
                frame = _to_bgr(env.render())
                if self.start_ok(frame):
                    if matched_at is None:
                        matched_at = pulses
                else:
                    matched_at = None
                settled_pulses = 0 if matched_at is None else pulses - matched_at
                if matched_at is not None and settled_pulses >= required_pulses:
                    emit_windows_event(
                        "start_check", "end", process=self.cfg.process,
                        game_pid=getattr(env, "game_pid", None), success=True,
                        live_pulses=pulses,
                        settle_seconds=settle_s, settled_pulses=settled_pulses,
                        live_seconds=round(pulses * pulse_s, 6))
                    return True
                if _t.monotonic() >= deadline:
                    failure_frame = None
                    try:
                        from PIL import Image
                        STARTUP_FAILURE_DIR.mkdir(parents=True, exist_ok=True)
                        path = STARTUP_FAILURE_DIR / (
                            f"{getattr(env, 'game_pid', 'unknown')}_{_t.time_ns()}.png")
                        Image.fromarray(frame[:, :, ::-1]).save(path)
                        failure_frame = str(path)
                        print(f"[startup] failed frozen start frame: {path}", flush=True)
                    except Exception as exc:  # Evidence must not replace the start failure.
                        print(f"[startup] failed to preserve start frame: {exc}", flush=True)
                    emit_windows_event(
                        "start_check", "end", process=self.cfg.process,
                        game_pid=getattr(env, "game_pid", None), success=False,
                        live_pulses=pulses,
                        settle_seconds=settle_s, settled_pulses=settled_pulses,
                        live_seconds=round(pulses * pulse_s, 6),
                        failure_frame=failure_frame)
                    return False
                env.hard_pause = False
                env.unpause()
                try:
                    _t.sleep(pulse_s)
                finally:
                    env.hard_pause = True
                    env.pause()
                pulses += 1
        finally:
            env.hard_pause = True
            env.pause()

    def prepare_input_foreground(self, env) -> None:
        """Acquire game focus while the caller already owns a live window.

        Stepped callers invoke this inside the measured complete pulse after
        resume and before publishing policy input. Live callers are already
        running. This method must therefore never pause or resume the game.
        """
        import win32gui

        info = getattr(env.lifecycle, "_found_cache", None)
        hwnd = (getattr(info, "extras", None) or {}).get("hwnd") if info else None
        if not hwnd:
            raise RuntimeError("game window unavailable before input")
        if win32gui.GetForegroundWindow() == hwnd:
            return
        if not self._force_foreground(env):
            raise RuntimeError("failed to foreground game before input")

    def _force_foreground(self, env) -> bool:
        """Request focus with the configured unlock gesture and verify it."""
        hwnd = None
        try:
            import win32gui
            from coding4game_gui.benchmark.windows.auto_nav import force_foreground

            info = env.lifecycle.find()
            hwnd = (getattr(info, "extras", None) or {}).get("hwnd")
            if not hwnd:
                record_foreground_failure(hwnd, "window_unavailable")
                return False
            if win32gui.GetForegroundWindow() == hwnd:
                return True
            self._send_focus_alt()
            focused = force_foreground(hwnd)
            if not focused:
                record_foreground_failure(hwnd, "activation_not_observed")
            return focused
        except Exception as error:  # noqa: BLE001 - bounded callers handle focus failure
            record_foreground_failure(hwnd, "focus_exception", type(error).__name__)
            return False

    def _send_focus_alt(self) -> bool:
        """Emit the optional ALT focus-unlock gesture.

        Some games need this to let a background benchmark process claim the
        foreground window. Games that switch their input mode on any keyboard
        event can disable it through startup.focus_alt_key.
        """
        if not self._focus_alt_key:
            return False
        try:
            import ctypes as _ct
            _ct.windll.user32.keybd_event(0x12, 0, 0, 0)  # ALT down
            _ct.windll.user32.keybd_event(0x12, 0, 2, 0)  # ALT up
            return True
        except Exception:  # noqa: BLE001
            return False

    def _press_until_screen_changes(self, env, step, retries: int = 8) -> None:
        """Closed-loop first press: a freshly plugged virtual pad has its
        first inputs SWALLOWED until the game registers the device (observed
        live on Brotato: neutral warmup taps do NOT count as activity, and
        the swallow window is timing-unstable — open-loop waits of 4-8s all
        failed). Every recorded sequence starts with a menu action whose
        acceptance visibly changes the screen (focus highlight moves /
        screen transitions), and mid-combat frames change every frame anyway
        — so press, compare frames, and repeat until the press provably
        landed. Residual failures fall through to start_check/cold-start retry."""
        import numpy as np

        expect = getattr(step.spec, "expect_text", None)
        absent = getattr(step.spec, "expect_absent", None)
        if expect or absent:
            _press_until_text(env, step, self.nav_verifier(env), retries=retries)
            return

        def _shot():
            # crop away the bottom band: the Steam "controller connected"
            # toast animates there and would fake a screen change
            a = np.asarray(env.render().convert("L")).astype(np.int16)
            return a[: int(a.shape[0] * 0.78)]

        def _changed(a, b) -> int:
            # count CHANGED PIXELS, not mean delta: a menu focus swap only
            # touches ~5-10k px of a 720p frame (mean delta ~0.07 — far below
            # any usable mean threshold), while static frames change ~0
            # Loading saved display preferences can resize the client mid-menu.
            if a.shape != b.shape:
                return max(a.size, b.size)
            return int((np.abs(a - b) > 25).sum())

        def _settle() -> "np.ndarray":
            # wait for screen-transition animations to END before pressing:
            # presses landing mid-transition are dropped by the UI, and the
            # animation's own motion would fake the "press landed" signal.
            # Combat never settles — cap the wait and just press (combat
            # presses need no precision).
            prev = _shot()
            for _ in range(6):
                time.sleep(0.4)
                cur = _shot()
                if _changed(cur, prev) < 500:
                    return cur
                prev = cur
            return prev

        for _ in range(retries):
            self._force_foreground(env)   # refresh focus before each press: it
                                          # can be stolen mid-nav, silently
                                          # dropping the press
            before = _settle()
            env.apply_state(step.pad, buttons=step.spec.buttons,
                            left_stick=step.spec.left_stick,
                            right_stick=step.spec.right_stick,
                            left_trigger=step.spec.left_trigger,
                            right_trigger=step.spec.right_trigger)
            time.sleep(max(0.12, step.spec.duration_seconds))
            env.apply_state(
                step.pad,
                buttons=[],
                left_stick=(0.0, 0.0),
                right_stick=(0.0, 0.0),
                left_trigger=0.0,
                right_trigger=0.0,
            )                                      # release THIS step's pad
            time.sleep(0.6)
            if _changed(_shot(), before) > 2000:
                return

    # -- navigation primitive --
    def _ensure_menu_handoff_foreground(self, env) -> None:
        """Keep desktop capture on the game before its message loop freezes."""
        screen_capture = getattr(env, "screenshot_backend_name", None) in {
            "dxcam", "pyautogui", "printwindow_visible", "visible_client"}
        if not screen_capture and self.cfg.process.casefold() != "palworld-win64-shipping.exe":
            return
        for _ in range(3):
            if self._force_foreground(env):
                if getattr(self.cfg, 'ime_context_mode', 'system') == 'disabled':
                    from coding4game_gui.benchmark.windows.drivers.windows_xinput.ime_context import (
                        prepare_owned_ime_context)
                    prepare_owned_ime_context(env, self.cfg.process)
                return
            time.sleep(0.2)
        raise RuntimeError(
            f"{self.cfg.process} lost foreground before the frozen start check")

    def replay_menu_skip(self, env, steps=None) -> None:
        """Replay trusted recorded keys while preserving the gameplay input owner."""
        from coding4game_gui.benchmark.windows.startup import recorded_keyboard_input

        steps = list(self.cfg.menu_skip if steps is None else steps)
        with recorded_keyboard_input(env, steps):
            self._replay_menu_skip(env, steps)

    def _replay_menu_skip(self, env, steps=None) -> None:
        """Replay a recorded sequence with the OS-suspend freeze disabled:
        set_speed against a suspended process hangs the xspeedhack IPC (see
        coding4game_gui/benchmark/windows/driver_env.py hard_pause note), and menus generally ignore
        the timescale anyway — recorded waits carry the pacing."""
        if steps is None:
            steps = self.cfg.menu_skip
        steps = list(steps)
        if (getattr(self, "_start_check", None) and steps
                and not getattr(self, "_menu_skip_preserve_tail", True)):
            final = getattr(steps[-1], "spec", steps[-1])
            final_is_neutral = (
                getattr(final, "click", None) is None
                and not getattr(final, "click_current", False)
                and not getattr(final, "type_text", None)
                and not tuple(getattr(final, "buttons", ()) or ())
                and not any(tuple(getattr(final, "left_stick", (0.0, 0.0))
                                  or (0.0, 0.0)))
                and not any(tuple(getattr(final, "right_stick", (0.0, 0.0))
                                  or (0.0, 0.0)))
                and not float(getattr(final, "left_trigger", 0.0) or 0.0)
                and not float(getattr(final, "right_trigger", 0.0) or 0.0)
            )
            if final_is_neutral:
                deferred_s = max(
                    0.0, float(getattr(final, "duration_seconds", 0.0)))
                steps.pop()
                print(f"[wge] deferring final menu wait {deferred_s:.2f}s "
                      "to frozen start-check handoff")
                emit_windows_event(
                    "menu_skip_tail_deferred", "instant",
                    process=self.cfg.process, duration_s=deferred_s)
        env.hard_pause = False
        env.unpause()                 # resume FIRST: AttachThreadInput against
        focused = False
        for _ in range(6):            # a suspended window thread deadlocks.
            if self._force_foreground(env):  # foregrounding is flaky in a SYSTEM
                focused = True
                break                        # session; a lost focus drops every
            time.sleep(0.3)                  # menu press → retry until it sticks.
        try:
            if not focused:
                raise RuntimeError(
                    f"{self.cfg.process} lost foreground before menu replay")
            def guard_palworld_modal(_step_index: int) -> None:
                if self.cfg.process.casefold() != "palworld-win64-shipping.exe":
                    return
                from coding4game_gui.benchmark.windows.auto_nav import dismiss_palworld_eos_login_popup
                if not self._force_foreground(env):
                    raise RuntimeError("Palworld lost foreground before menu input")
                info = env.lifecycle.find()
                hwnd = (getattr(info, "extras", None) or {}).get("hwnd")
                dismiss_palworld_eos_login_popup(
                    env, self.cfg.process, hwnd)

            if self._menu_skip_replay == "open_loop":
                from coding4game_gui.benchmark.windows.auto_nav import run_pad_sequence
                replay_options = {}
                if self.cfg.process.casefold() == "palworld-win64-shipping.exe":
                    replay_options["before_step"] = guard_palworld_modal
                if any(getattr(getattr(step, "spec", step), "click", None) is not None
                       or getattr(getattr(step, "spec", step), "click_current", False)
                       or getattr(getattr(step, "spec", step), "type_text", None)
                       for step in steps):
                    replay_options["process_name"] = self.cfg.process
                run_pad_sequence(
                    env, steps, "menu_skip", max_duration=999.0,
                    **replay_options)
                self._ensure_menu_handoff_foreground(env)
                return

            # FULLY closed-loop: menus drop presses that land during screen
            # transitions (and a freshly plugged pad's first inputs) — an
            # open-loop replay of recorded timings derails on whichever press
            # gets eaten. Every effective menu press visibly changes the
            # screen (focus moves / screen switches), so press-until-changed
            # per pad-input step; pure waits sleep as recorded. A press with no
            # visible effect (e.g. a direction pinned at a list edge) retries
            # a few times then falls through — harmlessly, by definition.
            import os as _os
            _dbg = _os.environ.get("C4G_NAV_DEBUG")
            try:
                frame = env.render()
                print(f"[wge] navigating: client="
                      f"{_client_size_of(self.cfg.process)} "
                      f"frame={frame.size} "
                      f"calibrated={_calibrated_size(self.spec)}")
            except Exception:  # noqa: BLE001
                pass
            # Some menus select the mouse-hovered row even while the visible
            # controller highlight is elsewhere. Move the cursor away once,
            # before the pad sequence; doing this before every pad press would
            # repeatedly switch the game back to keyboard/mouse input mode.
            if getattr(self, "_menu_skip_park_cursor", False):
                _move_game_points(self.cfg.process, env, [(0, 0)],
                                  content_size=_calibrated_size(self.spec))
            for _i, step in enumerate(steps):
                tail_count = min(getattr(self, "_menu_skip_open_loop_tail", 0),
                                 len(steps))
                if tail_count and _i == len(steps) - tail_count:
                    from coding4game_gui.benchmark.windows.auto_nav import run_pad_sequence
                    replay_options = {}
                    if any(getattr(getattr(entry, "spec", entry), "click", None) is not None
                           or getattr(getattr(entry, "spec", entry), "click_current", False)
                           or getattr(getattr(entry, "spec", entry), "type_text", None)
                           for entry in steps[_i:]):
                        replay_options["process_name"] = self.cfg.process
                    run_pad_sequence(
                        env, steps[_i:], "menu_skip_rapid_tail",
                        max_duration=999.0,
                        before_step=lambda offset: guard_palworld_modal(_i + offset),
                        **replay_options)
                    break
                guard_palworld_modal(_i)
                if getattr(step.spec, "type_text", None):
                    _type_game_text(step.spec.type_text)
                    time.sleep(step.spec.duration_seconds)
                elif getattr(step.spec, "click_current", False):
                    _click_game_current(self.cfg.process, env)
                    time.sleep(step.spec.duration_seconds)
                elif step.spec.click is not None:
                    # Mouse steps need no press-until-changed loop: a click
                    # either lands on the target or the window is gone. This
                    # also sidesteps pad-only menu contortions — Balatro's
                    # cold path needed a five-press "wall bump" to home the
                    # focus ring, which a single click replaces.
                    expect = getattr(step.spec, "expect_text", None)
                    absent = getattr(step.spec, "expect_absent", None)
                    verifier = (self.nav_verifier(env)
                                if (expect or absent) else None)
                    if (expect or absent) and verifier is not None:
                        click_options = {}
                        if self.cfg.process.casefold() == "palworld-win64-shipping.exe":
                            click_options["before_click"] = lambda: guard_palworld_modal(_i)
                        if not _click_until_text(
                                self.cfg.process, env, step.spec.click, expect,
                                verifier,
                                settle=step.spec.duration_seconds,
                                content_size=_calibrated_size(self.spec),
                                absent=absent, **click_options):
                            raise RuntimeError(
                                f"nav click {step.spec.click} never reached "
                                f"{expect or ('not ' + str(absent))!r} "
                                f"({step.spec.description!r})")
                    elif not _click_until_changed(
                            self.cfg.process, env, step.spec.click,
                            settle=step.spec.duration_seconds,
                            content_size=_calibrated_size(self.spec)):
                        print(f"[wge] nav click {step.spec.click} had no "
                              f"visible effect after retries "
                              f"({step.spec.description!r})")
                elif (step.spec.buttons
                      or any(step.spec.left_stick)
                      or any(step.spec.right_stick)
                      or step.spec.left_trigger
                      or step.spec.right_trigger):
                    self._press_until_screen_changes(env, step)
                else:
                    expect = getattr(step.spec, "expect_text", None)
                    absent = getattr(step.spec, "expect_absent", None)
                    if expect or absent:
                        scene_options = {}
                        if getattr(step, "min_scene_luma", None) is not None:
                            scene_options["min_scene_luma"] = step.min_scene_luma
                        if not _wait_until_text(
                                env, expect, self.nav_verifier(env),
                                settle=step.spec.duration_seconds, absent=absent,
                                **scene_options):
                            raise RuntimeError(
                                f"nav wait never reached {expect or ('not ' + str(absent))!r} "
                                f"({step.spec.description!r})")
                    else:
                        time.sleep(step.spec.duration_seconds)
                if _dbg:
                    try:
                        _os.makedirs(_dbg, exist_ok=True)
                        env.render().save(
                            _os.path.join(_dbg, f"{_i:02d}_pad{step.pad}.png"))
                    except Exception:  # noqa: BLE001
                        pass
            self._ensure_menu_handoff_foreground(env)
        finally:
            env.hard_pause = True
            env.pause()

    # Compatibility for older local callers. New scenario-aware tools use the
    # public method above so their replay mode stays aligned with production.
    def _navigate(self, env, steps) -> None:
        self.replay_menu_skip(env, steps)
