"""Coordinate input, rendering, process ownership, and timing for a driver bundle."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Callable
from typing import Optional

from PIL import Image

from coding4game_gui.benchmark.windows.drivers.base import InputDriver
from coding4game_gui.benchmark.windows.drivers.base import Renderer
from coding4game_gui.benchmark.windows.drivers.base import ProcessLifecycle
from coding4game_gui.benchmark.windows.drivers.base import TimeController


# NtResumeProcess returning only means the threads are runnable again; it does
# not guarantee that SDL/LÖVE's event loop has actually run once.  A raw ACT
# pulse may give an xspeedhack-compatible game one short scheduler window at
# timescale zero before publishing the next pad state.  This is wall time, not
# charged gameplay time because the calibrated game clock is stopped.
_INPUT_WAKE_SETTLE_SECONDS = 0.02


@dataclass(frozen=True)
class StepTiming:
    """Timing split for one complete raw input pulse."""

    requested_s: float
    live_window_s: float
    step_wall_s: float
    step_overhead_s: float
    interrupted: bool = False
    timing_source: str = "host_pause_control_boundaries"
    control_transition_s: float = 0.0


class Env:
    def __init__(
        self,
        *,
        input_: InputDriver,
        renderer: Renderer,
        lifecycle: ProcessLifecycle,
        timectl: TimeController,
        env_fps: int = 10,
    ):
        self.input = input_
        self.renderer = renderer
        self.lifecycle = lifecycle
        self.timectl = timectl
        self.env_fps = max(1, int(env_fps))
        self.step_duration = 1.0 / self.env_fps
        # When True, pause() OS-suspends the game process (a complete freeze
        # beyond xspeedhack timescale). Set False to fall back to timescale-only.
        # NOTE: only safe to enable around the agent loop, NOT during menu_skip /
        # xspeedhack setup (suspending then calling set_speed can hang the IPC).
        self.hard_pause = True
        # True while the game process is OS-suspended. A suspended game produces
        # no new frames, so dxcam.get_latest_frame() would BLOCK — while suspended
        # we serve `_last_live_frame` (snapshotted just before the freeze) instead.
        self._suspended = False
        self._frozen_at: Optional[float] = None
        self._last_resume_transition_s = 0.0
        self._last_suspend_transition_s = 0.0
        self._last_suspend_failure: Optional[dict] = None
        self._last_live_frame: Optional[Image.Image] = None
        self._pre_suspend_capture_error: Optional[Exception] = None
        self.last_step_timing: Optional[StepTiming] = None

        # Cache the renderer's window geometry for startup and calibration.
        bbox = getattr(renderer, "bbox", (0, 0, 0, 0))
        _, _, self.window_width, self.window_height = bbox
        self.bbox = bbox

        # Serialize asynchronous diagnostic captures.
        self._screenshot_lock = asyncio.Lock()

    @property
    def gamepad(self) -> InputDriver:
        """Expose the virtual pads used during startup."""
        return self.input

    @property
    def use_speedhack(self) -> bool:
        """Whether the time controller can pause the game clock."""
        return bool(getattr(self.timectl, "available", False))

    @property
    def screenshot_backend_name(self) -> str:
        return getattr(self.renderer, "name", "unknown")

    @property
    def game_pid(self) -> Optional[int]:
        info = getattr(self.lifecycle, "_found_cache", None)
        return info.pid if info else None

    @property
    def game_arch(self) -> Optional[str]:
        info = getattr(self.lifecycle, "_found_cache", None)
        return info.arch if info else None

    # ---- lifecycle ----

    def reset(self) -> None:
        """Wake virtual pads so the host registers them (XInput specifically)."""
        self.input.wakeup()
        time.sleep(1.0)

    def pause(self) -> None:
        """Pause the game. With `hard_pause`, this ALSO OS-suspends the game
        process for a COMPLETE freeze (timescale=0 alone leaks on UE4). Stop the
        time controller while live, cache a frame, then suspend.
        Never call xspeedhack against a stopped process."""
        self.timectl.pause()
        self._suspend_process()

    def _suspend_process(self) -> bool:
        """OS-suspend after caching the last live frame; return whether it did."""
        if self.hard_pause and not self._suspended:
            self._last_suspend_failure = None
            fn = getattr(self.lifecycle, "suspend", None)
            if fn is not None:
                self._pre_suspend_capture_error = None
                self._last_live_frame = None
                try:
                    self._last_live_frame = self.renderer.screenshot()
                except Exception as exc:  # noqa: BLE001
                    # Freeze first, then surface the error without retrying
                    # a renderer that may require a live message loop.
                    self._pre_suspend_capture_error = exc
                try:
                    freezing_at = time.perf_counter()
                    frozen = fn()
                    self._last_suspend_transition_s = time.perf_counter() - freezing_at
                    if frozen:
                        self._frozen_at = freezing_at
                        self._suspended = True
                        return True
                    self._last_suspend_failure = getattr(self.lifecycle, "last_suspend_failure", None)
                except Exception as error:  # noqa: BLE001 — retain the existing failure guard
                    self._last_suspend_failure = {"stage": "suspend_exception",
                                                  "error_type": type(error).__name__}
        return False

    def _resume_process(self) -> bool:
        """Resume only the OS process, keeping the game timescale at zero."""
        self._last_resume_transition_s = 0.0
        self._last_suspend_transition_s = 0.0
        if not self._suspended:
            return False
        fn = getattr(self.lifecycle, "resume", None)
        if fn is None:
            raise RuntimeError("cannot resume hard-paused game: lifecycle has no resume()")
        try:
            transition_start = time.perf_counter()
            resumed = fn()
            self._last_resume_transition_s = time.perf_counter() - transition_start
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("failed to resume hard-paused game process") from exc
        if resumed is False:
            raise RuntimeError("failed to resume hard-paused game process")
        self._suspended = False
        self._frozen_at = None
        return True

    def unpause(self) -> None:
        """Resume the game. Resume the process FIRST (so the timescale write
        targets a live process), then set_speed(1)."""
        self._resume_process()
        self.timectl.unpause()

    def freeze_now(self) -> None:
        """Hard-stop an active game without waiting for capture or OCR.

        Used by the independent budget watchdog. Unlike pause(), this does not
        take a screenshot or talk to speedhack IPC before suspending the game.
        The next render serves the last captured frame, not a new observation.
        """
        if self._suspended:
            return
        fn = getattr(self.lifecycle, "suspend", None)
        freezing_at = time.perf_counter()
        if fn is None or fn() is not True:
            raise RuntimeError("cannot enforce game deadline: hard suspend failed")
        self._frozen_at = freezing_at
        self._last_suspend_transition_s = time.perf_counter() - freezing_at
        self._suspended = True

    def close(self) -> None:
        try:
            self.input.reset()
        finally:
            try:
                self.input.close()
            finally:
                self.renderer.close()

    # ---- IO ----

    def render(self) -> Image.Image:
        """Sync screenshot. Use `render_locked()` from async code."""
        if self._suspended and self._pre_suspend_capture_error is not None:
            raise RuntimeError(
                f'Pre-suspension capture failed: {self._pre_suspend_capture_error}'
            ) from self._pre_suspend_capture_error
        if self._suspended and self._last_live_frame is not None:
            return self._last_live_frame
        frame = self.renderer.screenshot()
        self._last_live_frame = frame
        return frame

    def refresh_frozen_frame(self) -> Image.Image:
        """Capture the latest desktop frame after a completed hard-paused pulse.

        ``render`` deliberately serves its cache while suspended. A renderer
        with an independent capture thread can refresh that cache after the
        game has been suspended, without advancing the game clock.
        """
        if not self._suspended:
            raise RuntimeError("fresh frozen capture requires a suspended game")
        frame = self.renderer.screenshot()
        self._last_live_frame = frame
        return frame

    def step(
        self,
        *,
        buttons: list[str],
        left_stick: tuple[float, float] = (0.0, 0.0),
        right_stick: tuple[float, float] = (0.0, 0.0),
        left_trigger: float = 0.0,
        right_trigger: float = 0.0,
        duration_seconds: Optional[float] = None,
        sample_interval: float = 0.0,
        before_input: Optional[Callable[[], None]] = None,
        live_action: Optional[Callable[[], None]] = None,
        on_frame: Optional[Callable[[Image.Image], bool]] = None,
        on_sample: Optional[Callable[[Image.Image, float], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Image.Image:
        """Resume, press and hold, release, and freeze one measured action.

        ``before_input`` is an optional trusted preparation callback. It runs
        after the game and time controller are live but before any policy
        input is published, so preparation that advances the game is included
        in the measured pulse. ``live_action`` is an optional non-pad input
        callback used by the Windows benchmark for an atomic pointer action.
        It runs after the game and time controller are live, and its elapsed
        time is part of the requested ``duration_seconds`` window rather than
        an extra settle. The pulse publishes a ``last_step_timing``
        split so callers do not confuse live game time with
        resume/suspend/render wall overhead.

        If the time controller is available and the process was hard-paused,
        pulse mode first gives the resumed event loop a short scheduler window
        at timescale zero.  Engines such as SDL/LÖVE otherwise may miss the
        next virtual-pad edge.  No such uncharged window is introduced when
        the time controller is unavailable.

        ``on_frame`` consumes samples without retaining a frame list. Returning
        True stops the hold without another press. ``should_stop`` is a
        cooperative cancellation check; native input/capture calls can block.
        ``on_sample`` passively receives the same frame and elapsed live time;
        its return value never stops or reissues the held input.
        """
        d = (self.step_duration if duration_seconds is None
             else max(0.0, float(duration_seconds)))
        self.last_step_timing = None
        # Complete raw pulse. A freshly NtResume'd SDL/LÖVE process can miss
        # a virtual-pad edge if the report arrives before its event loop gets
        # scheduled. A calibrated speed controller makes this scheduler window
        # logically free; NoopTimeController must never receive the extra wait.
        step_wall_started = time.perf_counter()
        live_started: Optional[float] = None
        live_ended: Optional[float] = None
        interrupted = True
        transitions = 0.0
        resumed_from_hard_pause = self._resume_process()
        transitions += self._last_resume_transition_s
        if not self.use_speedhack:
            live_started = time.perf_counter()
        try:
            if resumed_from_hard_pause and self.use_speedhack:
                time.sleep(_INPUT_WAKE_SETTLE_SECONDS)
            # Measure the acknowledged running interval, not RPC wall time.
            # Control-transition latency remains separately visible; this is
            # a host-boundary estimate, not an engine simulation-clock reading.
            transition_start = time.perf_counter()
            self.timectl.unpause()
            transitions += time.perf_counter() - transition_start
            if self.use_speedhack:
                live_started = time.perf_counter()
            start = live_started
            if should_stop is not None and should_stop():
                interrupted = True
            else:
                if before_input is not None:
                    before_input()
                if should_stop is not None and should_stop():
                    interrupted = True
                else:
                    self.input.apply_state(
                        0,
                        buttons=buttons, left_stick=left_stick,
                        right_stick=right_stick,
                        left_trigger=left_trigger, right_trigger=right_trigger,
                    )
                    if live_action is not None:
                        live_action()
                    interrupted = self._hold_until(
                        start + d, start=start,
                        sample_interval=sample_interval,
                        on_frame=on_frame, should_stop=should_stop,
                        on_sample=on_sample,
                    )
        finally:
            try:
                self.input.reset_pad(0)
            finally:
                pause_succeeded = False
                try:
                    transition_start = time.perf_counter()
                    if self.use_speedhack:
                        live_ended = self._frozen_at if self._suspended else transition_start
                    if not self._suspended:
                        self.timectl.pause()
                    pause_succeeded = True
                    transitions += time.perf_counter() - transition_start
                finally:
                    self._suspend_process()
                    transitions += self._last_suspend_transition_s
                    if not self.use_speedhack or not pause_succeeded:
                        live_ended = self._frozen_at
                    if live_started is not None:
                        if live_ended is None:
                            live_ended = time.perf_counter()
                        elapsed = max(0.0, live_ended - live_started)
                        wall_elapsed = max(0.0, live_ended - step_wall_started)
                        # Preserve consumption even if sampling or cleanup raises.
                        self.last_step_timing = StepTiming(
                            requested_s=d, live_window_s=elapsed,
                            step_wall_s=wall_elapsed,
                            step_overhead_s=max(0.0, wall_elapsed - elapsed),
                            interrupted=interrupted,
                            timing_source=("timescale_control_boundaries" if self.use_speedhack
                                           else "process_suspend_boundaries"),
                            control_transition_s=transitions,
                        )
                    if self.hard_pause and not self._suspended:
                        details = {key: value for key, value in (self._last_suspend_failure or {}).items()
                                   if key in {"stage", "pid", "ntstatus", "winerror", "error_type",
                                              "process_create_time", "process_status", "identity_error"}}
                        if self._pre_suspend_capture_error is not None:
                            details["capture_error_type"] = type(self._pre_suspend_capture_error).__name__
                        raise RuntimeError(
                            "failed to hard-pause game after input pulse; failure="
                            + json.dumps(details, sort_keys=True)
                        )
        frame = self.render()
        step_wall_s = time.perf_counter() - step_wall_started
        live_window_s = max(
            0.0, (live_ended if live_ended is not None else time.perf_counter())
            - (live_started if live_started is not None else step_wall_started),
        )
        self.last_step_timing = StepTiming(
            requested_s=d,
            live_window_s=live_window_s,
            step_wall_s=step_wall_s,
            step_overhead_s=max(0.0, step_wall_s - live_window_s),
            interrupted=interrupted,
            timing_source=("timescale_control_boundaries" if self.use_speedhack
                           else "process_suspend_boundaries"),
            control_transition_s=transitions,
        )
        return frame

    def _hold_until(
        self,
        end: float,
        *,
        start: float,
        sample_interval: float,
        on_frame: Optional[Callable[[Image.Image], bool]] = None,
        on_sample: Optional[Callable[[Image.Image, float], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """Busy-wait one already-live hold and optionally sample its frames."""
        next_sample = (start + sample_interval
                       if (on_frame is not None or on_sample is not None)
                       and sample_interval > 0
                       else None)
        sample_reserve = sample_interval
        while time.perf_counter() < end:
            if should_stop is not None and should_stop():
                return True
            if next_sample is not None and time.perf_counter() >= next_sample:
                sample_started = time.perf_counter()
                # Avoid starting synchronous capture near the pulse deadline.
                # The game keeps running
                # until that work returns. Keep the final frozen capture intact.
                if end - sample_started <= sample_reserve:
                    next_sample = None
                    continue
                live = self.renderer.screenshot()
                self._last_live_frame = live
                if on_sample is not None:
                    on_sample(live, max(0.0, time.perf_counter() - start))
                if on_frame is not None and on_frame(live):
                    return True
                # Do not replay missed sample slots after a slow capture/scan.
                now = time.perf_counter()
                sample_reserve = max(sample_reserve, 2 * (now - sample_started))
                next_sample += (max(1, int((now - next_sample) / sample_interval) + 1)
                                * sample_interval)
        return False

    def apply_state(
        self,
        idx: int,
        *,
        buttons: list[str],
        left_stick: tuple[float, float] = (0.0, 0.0),
        right_stick: tuple[float, float] = (0.0, 0.0),
        left_trigger: float = 0.0,
        right_trigger: float = 0.0,
    ) -> None:
        self.input.apply_state(
            idx,
            buttons=buttons, left_stick=left_stick, right_stick=right_stick,
            left_trigger=left_trigger, right_trigger=right_trigger,
        )

    async def render_locked(self) -> Image.Image:
        async with self._screenshot_lock:
            # Frozen game → dxcam would block waiting for a frame that never
            # comes; serve the frame snapshotted just before the freeze.
            if self._suspended and self._last_live_frame is not None:
                return self._last_live_frame
            frame = self.renderer.screenshot()
            self._last_live_frame = frame
            return frame
