"""Shared preparation for one Windows benchmark cold start.

The production reset controller and every real-game calibration tool must
prepare the same external state before launching the executable. Keeping this
in one place prevents a verifier from testing a leftover save or different
window geometry than the formal benchmark uses.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import subprocess
import time
from pathlib import Path

import psutil

from coding4game_gui.benchmark.windows import save_state as ss


_STEAM_ONLINE_MARKERS = (
    "RecvMsgClientLogOnResponse() : processing complete",
    "CCMInterface - received heartbeat response, connection still good",
)
_STEAM_OFFLINE_MARKERS = (
    "Sending SteamServersDisconnected_t",
    "CCMInterface::OnNetworkDeviceStateChange -- Lost device",
    "ConnectionDisconnected(",
)


def _steam_connection_log(steam_exe: str | os.PathLike[str]) -> Path:
    return Path(steam_exe).resolve().parent / "logs" / "connection_log.txt"


def steam_connection_state(log_path: str | os.PathLike[str]) -> str:
    """Return the newest authoritative Steam connection state from its log."""
    path = Path(log_path)
    if not path.is_file():
        return "unknown"
    # The connection log is a few MB on long-lived hosts.  Reading only its
    # tail keeps the preflight cheap while still covering many reconnects.
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - 256 * 1024), os.SEEK_SET)
        text = stream.read().decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        if any(marker in line for marker in _STEAM_ONLINE_MARKERS):
            return "online"
        if any(marker in line for marker in _STEAM_OFFLINE_MARKERS):
            return "offline"
    return "unknown"


def _steam_running(steam_exe: str | os.PathLike[str]) -> bool:
    expected = str(Path(steam_exe).resolve()).casefold()
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            if (proc.info.get("name") or "").casefold() != "steam.exe":
                continue
            actual = proc.info.get("exe")
            if actual and str(Path(actual).resolve()).casefold() == expected:
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
    return False


def _wait_steam_state(steam_exe: str | os.PathLike[str], expected: str,
                      timeout: float) -> bool:
    deadline = time.perf_counter() + max(0.0, float(timeout))
    log_path = _steam_connection_log(steam_exe)
    while time.perf_counter() <= deadline:
        if expected == "stopped":
            if not _steam_running(steam_exe):
                return True
        elif steam_connection_state(log_path) == expected:
            return True
        time.sleep(0.5)
    return False


def ensure_steam_logged_on(steam_exe: str | os.PathLike[str],
                           timeout: float = 60.0) -> dict:
    """Ensure Steam has a live CM session before launching a Steam game.

    A resident ``steam.exe`` is insufficient: Palworld asks Steam for an
    external authentication ticket, and EOS rejects a ticket produced after
    Steam's CM connection has dropped. Wait for the existing client to reconnect;
    never shut it down or restart it as part of game preparation.
    """
    steam_exe = str(Path(steam_exe).resolve())
    log_path = _steam_connection_log(steam_exe)
    initial = steam_connection_state(log_path)
    running = _steam_running(steam_exe)
    if running and initial == "online":
        print("[prelaunch] Steam CM session is logged on", flush=True)
        return {"initial": initial, "refreshed": False, "final": "online"}

    if running:
        print(f"[prelaunch] Steam session is {initial}; waiting for the existing client",
              flush=True)
        if not _wait_steam_state(steam_exe, "online", timeout=timeout):
            raise RuntimeError(
                "Steam is not logged on; reconnect in the existing client before "
                "launching this game. The client was left running.")
        return {"initial": initial, "refreshed": False, "final": "online"}

    subprocess.Popen(
        [steam_exe, "-silent"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if not _wait_steam_state(steam_exe, "online", timeout=timeout):
        final = steam_connection_state(log_path)
        raise RuntimeError(
            "Steam did not reach a logged-on CM session before game launch "
            f"(latest state: {final})")
    print("[prelaunch] Steam started and logged on", flush=True)
    return {"initial": initial, "refreshed": True, "final": "online"}


def validate_no_save_cleanup(startup: dict, save_state=None) -> str | None:
    """Validate an explicit process-only reset without certifying live readiness."""
    policy = startup.get("no_save_cleanup")
    if policy is not None and policy != "process_kill":
        raise ValueError("startup.no_save_cleanup must be 'process_kill' when declared")
    if policy and (startup.get("golden_checkpoint") or save_state):
        raise ValueError("process_kill cleanup cannot replace a configured save restore")
    return policy


def prepare_launch_state(spec: dict, cfg) -> dict:
    """Kill residual state, restore/clean the task state, and set geometry."""
    no_save_cleanup = validate_no_save_cleanup(
        spec.get("startup") or {}, getattr(cfg, "save_state", None))
    ss.kill_process(cfg.process)

    auto_launch = getattr(cfg, "auto_launch", None)
    launch_path = getattr(auto_launch, "launch_path", None)
    steam_report = None
    if (spec.get("startup", {}).get("steam_logged_on_required", False)
            and launch_path and Path(launch_path).name.casefold() == "steam.exe"):
        from coding4game_gui.benchmark.windows.game_launch import resolve_launch_path
        steam_report = ensure_steam_logged_on(resolve_launch_path(auto_launch))

    golden = (spec.get("startup") or {}).get("golden_checkpoint")
    if golden:
        restore_spec = ss.resolve_for_run(
            spec["driver_game"], cfg.save_state, golden)
        restore_report = ss.restore(restore_spec, cfg.process)
        checkpoint = getattr(restore_spec, "golden", golden)
        print(f"[startup] restored {checkpoint}", flush=True)
        result = {
            "mode": "golden_restore",
            "checkpoint": str(checkpoint),
            "restore": restore_report,
        }
    else:
        from coding4game_gui.benchmark.windows import game_settings
        auto_launch = getattr(cfg, "auto_launch", None)
        if no_save_cleanup != "process_kill":
            game_settings.clear_saved_run(
                spec["driver_game"],
                exe_path=getattr(auto_launch, "launch_path", None),
            )
        print("[startup] no golden checkpoint; using no-save cold start",
              flush=True)
        result = {"mode": "no_save_process_kill" if no_save_cleanup else "no_save_cleanup"}

    if steam_report is not None:
        result["steam_preflight"] = steam_report

    window_setup = (spec.get("scorer") or {}).get("window_setup")
    if window_setup:
        from coding4game_gui.benchmark.windows import game_settings
        auto_launch = getattr(cfg, "auto_launch", None)
        game_settings.apply(
            spec["driver_game"],
            window_setup.get("size"),
            window_setup.get("mode", "Windowed"),
            exe_path=getattr(auto_launch, "launch_path", None),
        )
    if spec['driver_game'] == 'balatro':
        from coding4game_gui.benchmark.windows.game_settings import verify_balatro_tutorial_disabled
        verify_balatro_tutorial_disabled()
    return result


def _prepare_trackmania_windowed(process: str, mode: str, hwnd: int,
                                timeout: float) -> None:
    """Use the measured native display toggle before resizing the D3D client."""
    if process.casefold() != "tmforever.exe" or mode != "Windowed":
        return
    import win32api
    import win32con
    import win32gui
    caption = win32con.WS_CAPTION
    if win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE) & caption == caption:
        return
    from coding4game_gui.benchmark.windows.auto_nav import force_foreground
    if not force_foreground(hwnd):
        raise RuntimeError("could not focus TrackMania for its native window toggle")
    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
    try:
        win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
        win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)
    finally:
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    deadline = time.perf_counter() + max(0., timeout)
    stable_since = None
    previous = None
    while time.perf_counter() <= deadline:
        if win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE) & caption == caption:
            window = win32gui.GetWindowRect(hwnd)
            client = win32gui.GetClientRect(hwnd)
            geometry = (*window, *client)
            bordered = ((window[2] - window[0]) > client[2]
                        and (window[3] - window[1]) > client[3])
            if bordered and geometry == previous:
                if stable_since is not None and time.perf_counter() - stable_since >= .3:
                    print("[startup] TrackMania native windowed mode confirmed", flush=True)
                    return
            else:
                stable_since = time.perf_counter() if bordered else None
            previous = geometry
        time.sleep(.1)
    raise RuntimeError("TrackMania did not enter native windowed mode")


def enforce_live_window(spec: dict, cfg, hwnd: int | None = None,
                        timeout: float = 5.0) -> tuple[int, int] | None:
    """Force the launched window's *client area* to the declared size.

    Some games rewrite their persisted resolution during startup. Balatro was
    observed turning a requested 1600x900 window into 2048x1280, which changes
    both aspect ratio and every scorer rectangle. Applying the size after the
    HWND exists makes the declared geometry authoritative.
    """
    setup = (spec.get("scorer") or {}).get("window_setup")
    size = (setup or {}).get("size")
    if not size:
        return None
    target = (int(size[0]), int(size[1]))
    position = setup.get("position")
    if position is not None:
        if (not isinstance(position, (list, tuple)) or len(position) != 2
                or any(type(v) is not int for v in position)):
            raise ValueError("window_setup.position must contain two integer screen coordinates")
        position = tuple(position)

    from coding4game_gui.benchmark.windows.drivers.windows_xinput.lifecycle import set_dpi_awareness
    set_dpi_awareness()
    if hwnd is None:
        from coding4game_gui.benchmark.windows.auto_nav import wait_for_game_hwnd
        hit = wait_for_game_hwnd(cfg.process, timeout=timeout)
        if hit is None:
            raise RuntimeError(
                f"could not find {cfg.process} window for geometry setup")
        hwnd = hit[0]

    import win32con
    import win32gui

    _prepare_trackmania_windowed(
        cfg.process, setup.get("mode", "Windowed"), hwnd, timeout)

    # Window-targeted renderers emit the native viewport. Desktop capture
    # reverses the measured DPI stretch, matching PrintWindow's native surface.
    if getattr(cfg, "screenshot_backend", None) in ("printwindow", "printwindow_visible", "visible_client", "dxcam"):
        from coding4game_gui.benchmark.windows.drivers.windows_xinput.renderer import printwindow_client_scale
        scale = printwindow_client_scale(hwnd)
        target = (round(target[0] * scale), round(target[1] * scale))

    _cl, _ct, cw, ch = win32gui.GetClientRect(hwnd)
    wl, wt, wr, wb = win32gui.GetWindowRect(hwnd)
    border_w = max(0, (wr - wl) - cw)
    border_h = max(0, (wb - wt) - ch)
    request_position = position or (0, 0)
    request_flags = (win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
                     | win32con.SWP_ASYNCWINDOWPOS)
    if position is None:
        request_flags |= win32con.SWP_NOMOVE
    # A synchronous cross-thread request can hang inside SetWindowPos before
    # the timeout below starts when the game's message loop is unresponsive.
    win32gui.SetWindowPos(
        hwnd, 0, *request_position,
        target[0] + border_w,
        target[1] + border_h,
        request_flags,
    )

    deadline = time.perf_counter() + max(0.0, timeout)
    last_request = time.perf_counter()
    requests = 1
    actual = None
    while time.perf_counter() <= deadline:
        _l, _t, width, height = win32gui.GetClientRect(hwnd)
        actual = (int(width), int(height))
        positioned = (position is None
                      or tuple(win32gui.GetWindowRect(hwnd)[:2]) == position)
        if actual == target and positioned:
            print(f"[startup] live client forced to {width}x{height}",
                  flush=True)
            return actual
        # DPI rounding can leave a native client one pixel short despite an
        # accepted request. Correct from the observed client/outer difference,
        # with bounded asynchronous retries in the same geometry deadline.
        if requests < 3 and time.perf_counter() - last_request >= .25:
            wl, wt, wr, wb = win32gui.GetWindowRect(hwnd)
            win32gui.SetWindowPos(
                hwnd, 0, *request_position,
                (wr - wl) + target[0] - actual[0],
                (wb - wt) + target[1] - actual[1],
                request_flags)
            requests += 1
            last_request = time.perf_counter()
        time.sleep(0.1)
    raise RuntimeError(
        f"{cfg.process} client size is {actual}, expected {target}, position={position}; "
        "scorer geometry would be invalid")


@contextmanager
def recorded_keyboard_input(env, steps):
    """Restore the original input owner and release every borrowed key on exit."""
    uses_keyboard = any(
        str(button).upper().startswith('KEY[')
        for step in steps
        for button in (getattr(getattr(step, 'spec', step), 'buttons', ()) or ()))
    original = getattr(env, 'input', None)
    if not uses_keyboard or original is None:
        yield
        return
    from coding4game_gui.benchmark.windows.drivers.windows_kbm import input as keyboard_input

    if isinstance(original, keyboard_input.KeyboardMouseInputDriver):
        yield
        return
    keyboard = keyboard_input.ScanCodeKeyboard()
    env.input = keyboard_input.KeyboardMouseInputDriver(original, keyboard)
    try:
        yield
    finally:
        env.input = original
        keyboard.close()
