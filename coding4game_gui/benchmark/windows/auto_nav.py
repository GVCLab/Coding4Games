"""Shared cold-start launch and menu-navigation primitives.

``start_auto_launch_session`` owns pads-before-launch ordering. Scenario-aware
tools inject the production menu replayer so their configured open/closed-loop
mode cannot drift from a benchmark episode.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from collections.abc import Iterable

import numpy as np
import psutil
import win32api
import win32con
import win32gui
import win32process

from coding4game_gui.benchmark.windows.drivers.registry import make_env
from coding4game_gui.benchmark.evidence.event_log import emit as emit_windows_event
from coding4game_gui.benchmark.windows.prelaunch import _close_quietly
from coding4game_gui.benchmark.windows.prelaunch import env_pad_kwargs
from coding4game_gui.benchmark.windows.prelaunch import launch_with_pads


def is_palworld_eos_login_popup(frame) -> bool:
    """Recognize both observed Palworld EOS multiplayer error layouts.

    This uses the stable geometry and dark blue-grey button of the one-button
    modal, not OCR or a network call. The button can be much dimmer than the
    calibration image, so its color must be compared with the dark backdrop.
    """
    rgb = np.asarray(frame.convert("RGB") if hasattr(frame, "convert") else frame)
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        return False
    rgb = rgb[:, :, :3]
    height, width = rgb.shape[:2]
    if height < 360 or width < 640:
        return False

    def crop(y0, y1, x0, x1):
        return rgb[int(height * y0):int(height * y1),
                   int(width * x0):int(width * x1)]

    whole_dark = float(np.mean(np.max(rgb, axis=2) < 45))
    text = crop(.28, .47, .25, .75)
    button = crop(.62, .74, .42, .58)
    separator = crop(.49, .515, .27, .73)
    text_bright = float(np.mean(np.min(text, axis=2) > 180))
    # The selected OK button is a desaturated blue-grey rectangle. Real
    # captures use about RGB(33, 40, 44); the old absolute blue >= 45 test
    # missed that button and let menu replay run behind the modal.
    button_rgb = button.astype(np.int16)
    button_blue_grey = float(np.mean(
        (button_rgb[:, :, 0] >= 20)
        & (button_rgb[:, :, 0] <= 85)
        & (button_rgb[:, :, 1] - button_rgb[:, :, 0] >= 3)
        & (button_rgb[:, :, 2] - button_rgb[:, :, 0] >= 6)
        & (button_rgb[:, :, 2] <= 110)))
    separator_grey = float(np.mean(
        (np.min(separator, axis=2) >= 35)
        & (np.max(separator, axis=2) <= 135)))
    # EOS Login Timeout uses a much wider, lower modal than the external-token
    # failure. Its OK button sits near the bottom of the frame. Checking only
    # the first layout let menu replay continue behind this second modal.
    timeout_text = crop(.25, .50, .20, .80)
    timeout_button = crop(.79, .91, .35, .65).astype(np.int16)
    timeout_separator = crop(.755, .78, .10, .90)
    timeout_bright = float(np.mean(np.min(timeout_text, axis=2) > 180))
    timeout_button_blue_grey = float(np.mean(
        (timeout_button[:, :, 0] >= 20)
        & (timeout_button[:, :, 0] <= 85)
        & (timeout_button[:, :, 1] - timeout_button[:, :, 0] >= 3)
        & (timeout_button[:, :, 2] - timeout_button[:, :, 0] >= 6)
        & (timeout_button[:, :, 2] <= 110)))
    timeout_separator_grey = float(np.mean(
        (np.min(timeout_separator, axis=2) >= 35)
        & (np.max(timeout_separator, axis=2) <= 135)))
    external_token_layout = (text_bright > .005
                             and button_blue_grey > .08
                             and separator_grey > .01)
    timeout_layout = (timeout_bright > .005
                      and timeout_button_blue_grey > .12
                      and timeout_separator_grey > .01)
    # A user screenshot may be cropped differently from the full dxcam frame.
    # Keep a layout-independent fallback: bright modal text, a blue-grey OK
    # button below it, and a long grey divider row on an otherwise dark frame.
    wide_text = crop(.15, .55, .15, .85)
    lower = crop(.50, .95, .20, .80).astype(np.int16)
    divider = crop(.45, .85, .10, .90)
    wide_text_bright = float(np.mean(np.min(wide_text, axis=2) > 180))
    lower_button_blue_grey = float(np.mean(
        (lower[:, :, 0] >= 20)
        & (lower[:, :, 0] <= 85)
        & (lower[:, :, 1] - lower[:, :, 0] >= 3)
        & (lower[:, :, 2] - lower[:, :, 0] >= 6)
        & (lower[:, :, 2] <= 110)))
    divider_grey = ((np.min(divider, axis=2) >= 35)
                    & (np.max(divider, axis=2) <= 135))
    long_divider = float(np.max(np.mean(divider_grey, axis=1))) > .30
    generic_modal = (wide_text_bright > .005
                     and lower_button_blue_grey > .004
                     and long_divider)
    return whole_dark > .90 and (external_token_layout or timeout_layout
                                 or generic_modal)


def dismiss_palworld_eos_login_popup(env, process_name: str,
                                     hwnd: int | None = None) -> bool:
    """Dismiss Palworld's multiplayer-only EOS modal before menu replay.

    A detected modal gets one keyboard Escape press and five seconds to
    return to the title screen. Normal menu replay is otherwise unchanged.
    """
    if process_name.casefold() != "palworld-win64-shipping.exe":
        return False
    frame = env.render()
    if not is_palworld_eos_login_popup(frame):
        return False
    emit_windows_event(
        "startup_modal", "detected", process=process_name,
        reason="eos_multiplayer_login_error")
    print("[auto-launch] Palworld EOS popup detected; pressing Escape", flush=True)
    if hwnd is not None and not force_foreground(hwnd):
        raise RuntimeError("could not focus Palworld before dismissing EOS popup")
    win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
    try:
        time.sleep(0.12)
    finally:
        win32api.keybd_event(win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(5.0)
    if is_palworld_eos_login_popup(env.render()):
        emit_windows_event(
            "startup_modal", "dismiss_failed", process=process_name,
            reason="eos_multiplayer_login_error")
        raise RuntimeError("Palworld EOS login popup remained after Escape")
    emit_windows_event(
        "startup_modal", "dismissed", process=process_name,
        reason="eos_multiplayer_login_error", dismissed_by="keyboard_escape")
    print("[auto-launch] EOS popup cleared; resuming recorded menu steps", flush=True)
    return True


def force_foreground(hwnd: int) -> bool:
    """Request foreground activation without sharing the game's input queue."""
    try:
        if (win32gui.GetForegroundWindow() == hwnd
                and not win32gui.IsIconic(hwnd)):
            return True
        if win32gui.IsIconic(hwnd):
            win32gui.PostMessage(hwnd, win32con.WM_SYSCOMMAND,
                                 win32con.SC_RESTORE, 0)
        # AttachThreadInput makes activation synchronous with a busy game.
        # Keep separate queues and let the caller's existing bounded polling
        # observe activation; a posted request alone is not focus evidence.
        flags = (win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                 | win32con.SWP_ASYNCWINDOWPOS)
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0, flags)
        win32gui.SetForegroundWindow(hwnd)
        return (win32gui.GetForegroundWindow() == hwnd
                and not win32gui.IsIconic(hwnd))
    except Exception as exc:  # noqa: BLE001 - best-effort Win32 focus dance
        print(f"  warn: force_foreground failed: {exc!r}", flush=True)
        return False


def prepare_startup_pointer(hwnd: int) -> None:
    """Move an external pointer into the focused client before first capture.

    A pointer left over Task View can keep its hover preview above a newly
    focused game. This preparation sends no click/key and is never used after
    policy handoff. Existing in-client pointer positions remain unchanged.
    """
    if win32gui.GetForegroundWindow() != hwnd:
        raise RuntimeError("startup pointer preparation requires game foreground")
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    x, y = win32gui.ClientToScreen(hwnd, (left, top))
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise RuntimeError("startup pointer preparation requires a visible client")

    def inside(point):
        return x <= point[0] < x + width and y <= point[1] < y + height

    before = win32api.GetCursorPos()
    if inside(before):
        return
    win32api.SetCursorPos((x + width // 2, y + height // 2))
    after = win32api.GetCursorPos()
    if not inside(after):
        raise RuntimeError("startup pointer could not enter the game client")
    emit_windows_event("startup_pointer", "prepared", hwnd=hwnd,
                       before=list(before), after=list(after),
                       reason="pointer_outside_client")


def find_game_hwnd(process_name: str):
    """Return the same non-proxy game window selected by the driver bundle."""
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
                found.append({"hwnd": hwnd, "title": title})
        return True

    win32gui.EnumWindows(cb, None)
    from coding4game_gui.benchmark.windows.drivers.windows_xinput.lifecycle import select_game_window
    selected = select_game_window(found)
    return (selected["hwnd"], selected["title"]) if selected else None


def wait_for_game_hwnd(process_name: str, timeout: float = 10.0):
    """Wait through launcher-process handoff until the real HWND is stable."""
    deadline = time.perf_counter() + max(0.0, timeout)
    hit = None
    while hit is None and time.perf_counter() < deadline:
        time.sleep(0.5)
        hit = find_game_hwnd(process_name)
    return hit


def _final_window_presentation(process_name: str, hwnd: int) -> bool:
    """Exclude observed loading-window presentations before stability/settle."""
    name = process_name.casefold()
    if name not in ("aimlab_tb.exe", "ridersrepublic.exe"):
        return True
    try:
        if name == "ridersrepublic.exe":
            # The main window retains WS_POPUP while its loading client is
            # still smaller than the calibrated windowed surface.
            if (win32gui.GetClassName(hwnd) != "ScimitarEngineWindowClass"
                    or win32gui.IsIconic(hwnd)):
                return False
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            if style & win32con.WS_POPUP:
                return False
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            return right > left and bottom > top
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    except win32gui.error:
        return False
    return style & win32con.WS_CAPTION == win32con.WS_CAPTION


def wait_for_stable_game_window(process_name: str, timeout: float,
                                *, stable_seconds: float = 10.0) -> bool:
    """Wait for the final presentation within the original launch budget."""
    deadline = time.perf_counter() + max(0.0, float(timeout))
    last_hwnd, since = None, None
    declined_dialogs = set()
    while time.perf_counter() < deadline:
        hit = find_game_hwnd(process_name)
        hwnd = hit[0] if hit else None
        if hwnd is not None and not _final_window_presentation(process_name, hwnd):
            # Aimlab and Riders can keep the same HWND through loading.
            # Count stability from the final window presentation.
            hwnd = None
        if hwnd is not None:
            from coding4game_gui.benchmark.windows.startup_dialogs import decline_palworld_driver_download
            if hwnd in declined_dialogs:
                hwnd = None
            elif decline_palworld_driver_download(process_name, hwnd):
                declined_dialogs.add(hwnd)
                hwnd = None
        now = time.perf_counter()
        if hwnd is None:
            last_hwnd, since = None, None
        else:
            if hwnd != last_hwnd:
                last_hwnd, since = hwnd, now
            if now - since >= max(0.0, stable_seconds):
                return True
        time.sleep(0.5)
    return False


def settle_with_foreground(hwnd: int, seconds: float,
                           interval: float = 2.0,
                           process_name: str | None = None,
                           handoff_timeout_seconds: float = 0.0) -> int:
    """Require the full settle on the final window within a bounded handoff."""
    settled = 0.0
    seconds = max(0.0, float(seconds))
    interval = max(0.1, float(interval))
    deadline = (time.perf_counter() + seconds
                + max(0.0, float(handoff_timeout_seconds)))
    while settled < seconds:
        step = min(interval, seconds - settled)
        if process_name:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise RuntimeError(
                    f"{process_name} did not keep a game window for the "
                    f"configured {seconds:g}s settle within the startup budget")
            step = min(step, remaining)
        time.sleep(step)
        if process_name:
            replacement = find_game_hwnd(process_name)
            if (replacement is not None
                    and not _final_window_presentation(process_name, replacement[0])):
                replacement = None
            if replacement is None:
                if hwnd is not None:
                    print("[auto-launch] game window is not ready during settle; "
                          "waiting for the final presentation", flush=True)
                hwnd = None
                settled = 0.0
                continue
            if replacement[0] != hwnd:
                hwnd = replacement[0]
                settled = 0.0
                print(f"[auto-launch] replacement hwnd={hwnd}; restarting "
                      f"the configured {seconds:g}s settle", flush=True)
                force_foreground(hwnd)
                continue
        force_foreground(hwnd)
        settled += step
    return hwnd


def run_pad_sequence(env, entries: Iterable, label: str,
                      max_duration: float = 3.0,
                      clock: Callable[[], float] | None = None,
                      before_step: Callable[[int], None] | None = None,
                      process_name: str | None = None) -> None:
    """Replay a recorded pad timeline exactly once at wall-clock 1x.

    This intentionally does not inspect screenshots or retry presses. Recorded
    waits and inputs are the artifact; changing them based on unrelated screen
    motion makes the replay a different navigation policy.
    """
    entries = list(entries)
    if not entries:
        return
    now = clock or time.perf_counter
    print(f"\n=== {label} ({len(entries)} steps) ===", flush=True)
    for i, entry in enumerate(entries):
        if before_step is not None:
            before_step(i)
        spec = getattr(entry, "spec", entry)
        pad = getattr(entry, "pad", 0)
        seconds = max(0.0, min(float(spec.duration_seconds), max_duration))
        btns = "+".join(spec.buttons) if spec.buttons else "-"
        emit_windows_event(
            "menu_skip_step", "begin", label=label, step=i + 1,
            total_steps=len(entries), pad=pad + 1, buttons=list(spec.buttons),
            duration_s=seconds, recorded_duration_s=float(spec.duration_seconds),
            description=spec.description[:200])
        if (getattr(spec, "click", None) is not None
                or getattr(spec, "click_current", False)
                or getattr(spec, "type_text", None)):
            from coding4game_gui.benchmark.windows.env import (
                _click_game_current, _click_game_points, _type_game_text,
            )
            if getattr(spec, "type_text", None):
                _type_game_text(spec.type_text)
            elif process_name is None:
                raise RuntimeError("mouse menu replay requires process_name")
            elif getattr(spec, "click_current", False):
                _click_game_current(process_name, env)
            else:
                _click_game_points(process_name, env, [spec.click])
            time.sleep(seconds)
            emit_windows_event(
                "menu_skip_step", "end", label=label, step=i + 1,
                total_steps=len(entries), pad=pad + 1,
                buttons=list(spec.buttons), duration_s=seconds, capped=False)
            continue
        env.apply_state(
            pad,
            buttons=list(spec.buttons),
            left_stick=tuple(spec.left_stick),
            right_stick=tuple(spec.right_stick),
            left_trigger=float(spec.left_trigger),
            right_trigger=float(spec.right_trigger),
        )
        started = now()
        while now() - started < seconds:
            pass
        env.gamepad.reset_pad(pad)
        # Say so when the cap reshaped a recorded wait — a silent truncation
        # derails every later step and looks like a bad recording.
        capped = ""
        if float(spec.duration_seconds) > seconds:
            capped = f" (capped from {float(spec.duration_seconds):.2f}s)"
        print(f"  [{i + 1:2d}/{len(entries)}] pad{pad + 1} "
              f"btns={btns:10s} {seconds:.2f}s{capped} | "
              f"{spec.description[:50]!r}", flush=True)
        emit_windows_event(
            "menu_skip_step", "end", label=label, step=i + 1,
            total_steps=len(entries), pad=pad + 1, buttons=list(spec.buttons),
            duration_s=seconds, capped=bool(capped))


def start_auto_launch_session(
        cfg,
        *,
        before_launch: Callable[[], None] | None = None,
        after_window: Callable[[int], None] | None = None,
        post_settle: Callable[[object, int], None] | None = None,
        menu_skip_replayer: Callable[[object, Iterable], None] | None = None,
        require_menu_skip: bool = False,
        platform_name: str = "auto",
        env_fps: int = 10,
        use_speedhack: bool = False,
        attempts: int = 1,
        on_attempt_failed=None,
        window_ok=None):
    """Cold-start one game through the recorded auto-launch path.

    This is the single implementation shared by benchmark reset, the Full
    auto-launch verifier and scorer-session recorder: preparation, pad creation, direct
    EXE launch, process/window wait, fixed settle, env construction, then one
    replay of ``menu_skip``. Scenario-aware callers pass the production
    reset-controller replayer; driver-only callers retain open-loop replay.
    The returned env remains alive for the caller.

    The pads are created BEFORE the launch and handed to the env, not built by
    it — the whole ordering lives in ``prelaunch.launch_with_pads``. This path
    kept the old launch-then-create ordering after the bench path and the
    recorder were fixed, which is why Slay the Spire's menu_skip replay and
    every scorer calibration session ran with a controller the game had never
    enumerated.
    """
    al = cfg.auto_launch
    if al is None or not al.launch_path:
        raise RuntimeError(
            f"game {cfg.name!r} has no auto_launch.launch_path")
    if before_launch is not None:
        before_launch()

    # Resolved before the launch because the pad count decides how many pads to
    # create while the game is still closed. Reading cfg has no side effects.
    n_players = 1 + max((entry.pad for entry in cfg.menu_skip), default=0)

    # One call owns pads-then-launch-then-window-wait, and closes the pads if
    # the launch fails. Interactive callers default to one attempt; benchmark
    # reset explicitly retains its bounded retry and orphan-cleanup policy.
    launch_options = dict(n_pads=n_players, attempts=attempts,
                          platform_name=platform_name)
    if on_attempt_failed is not None:
        launch_options["on_attempt_failed"] = on_attempt_failed
    launch_options["window_ok"] = window_ok or wait_for_stable_game_window
    prepared = launch_with_pads(al, cfg.process, **launch_options)
    pads = prepared.pads

    # From here to make_env nothing owns the pads yet, so every exit drops them.
    try:
        # A Steamworks bootstrap can briefly expose a window, exit, and let
        # Steam create the actual game more than ten seconds later. Keep the
        # configured window budget while following that handoff.
        window_budget = float(getattr(al, "window_wait_seconds", 180.0))
        window_deadline = time.perf_counter() + window_budget
        hit = wait_for_game_hwnd(cfg.process, timeout=window_budget)
        if hit is None:
            raise RuntimeError(f"could not locate the {cfg.process} HWND")
        hwnd, title = hit
        print(f"[auto-launch] game hwnd={hwnd} title={title!r}", flush=True)
        force_foreground(hwnd)

        print(f"[auto-launch] settling {al.settle_seconds:.0f}s ...", flush=True)
        hwnd = settle_with_foreground(
            hwnd, al.settle_seconds, process_name=cfg.process,
            handoff_timeout_seconds=max(
                0.0, window_deadline - time.perf_counter())) or hwnd
        # Splash screens can use fullscreen until account presentation loads.
        # Enforce the final geometry after settle, before renderer creation.
        if after_window is not None:
            after_window(hwnd)

        # Clear taskbar hover previews before constructing the desktop renderer.
        # Other overlays still fail the normal capture guard. Keep this outside
        # gameplay and share it with Full verification and human recording.
        if cfg.screenshot_backend == "dxcam" and force_foreground(hwnd):
            prepare_startup_pointer(hwnd)

        # Reuse the pads the game enumerated during startup.
        env = make_env(
            platform_name,
            cfg.process,
            env_fps=env_fps,
            n_players=n_players,
            screenshot_backend=(None if (cfg.screenshot_backend or "auto")
                                == "auto" else cfg.screenshot_backend),
            use_speedhack=use_speedhack,
            **env_pad_kwargs(pads),
        )
    except BaseException:
        _close_quietly(pads)
        raise

    # env owns the pads from here on: Env.close() calls input.close().
    try:
        if use_speedhack and not env.use_speedhack:
            raise RuntimeError(f"{cfg.process} requires xspeedhack, but it could not be attached")
        env.reset()
        if post_settle is not None:
            post_settle(env, hwnd)
        dismiss_palworld_eos_login_popup(env, cfg.process, hwnd)
        if not cfg.menu_skip:
            if require_menu_skip:
                raise RuntimeError(
                    f"game {cfg.name!r} has no recorded menu_skip")
            return env, hwnd, title
        force_foreground(hwnd)
        if menu_skip_replayer is None:
            replay_options = {}
            if cfg.process.casefold() == "palworld-win64-shipping.exe":
                replay_options["before_step"] = lambda _i: dismiss_palworld_eos_login_popup(
                    env, cfg.process, hwnd)
            run_pad_sequence(
                env, cfg.menu_skip, "menu_skip", max_duration=999.0,
                process_name=cfg.process,
                **replay_options)
        else:
            menu_skip_replayer(env, cfg.menu_skip)
        if (cfg.process.casefold() == "palworld-win64-shipping.exe"
                and is_palworld_eos_login_popup(env.render())):
            raise RuntimeError("Palworld EOS popup present after menu replay")
        return env, hwnd, title
    except BaseException:
        env.close()
        raise
