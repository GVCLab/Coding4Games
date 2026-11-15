"""Best-effort, host-only observations after a failed focus request."""
from __future__ import annotations

import os

from coding4game_gui.benchmark.evidence.event_log import ENV_VAR, emit


def record_foreground_failure(hwnd, reason: str, error_type: str | None = None) -> None:
    """Record identities without input, window titles, paths, or another retry."""
    if not os.environ.get(ENV_VAR):
        return
    details = {"reason": reason, "observation_scope": "after_failed_focus_request"}
    if error_type is not None:
        details["error_type"] = error_type
    try:
        import ctypes
        import psutil
        import win32gui
        import win32process

        def window_details(handle):
            data = {"hwnd": int(handle) if handle else None}
            if not handle:
                return data
            try:
                data["is_window"] = bool(win32gui.IsWindow(handle))
                if not data["is_window"]:
                    return data
                data.update(
                    visible=bool(win32gui.IsWindowVisible(handle)),
                    minimized=bool(win32gui.IsIconic(handle)),
                    class_name=win32gui.GetClassName(handle),
                )
                thread_id, pid = win32process.GetWindowThreadProcessId(handle)
                data.update(thread_id=thread_id, pid=pid)
                try:
                    process = psutil.Process(pid)
                    data.update(process_name=process.name(),
                                process_created=process.create_time())
                except Exception as error:
                    data["process_error_type"] = type(error).__name__
            except Exception as error:
                data["observation_error_type"] = type(error).__name__
            return data

        foreground = win32gui.GetForegroundWindow()
        details["target"] = window_details(hwnd)
        details["foreground"] = window_details(foreground)
        modifiers = {"shift": 0x10, "control": 0x11, "alt": 0x12,
                     "left_windows": 0x5B, "right_windows": 0x5C}
        details["modifiers_down"] = {
            name: bool(ctypes.windll.user32.GetAsyncKeyState(key) & 0x8000)
            for name, key in modifiers.items()
        }
    except Exception as error:
        details["observation_error_type"] = type(error).__name__
    try:
        emit("foreground_acquisition", "failed", **details)
    except Exception:
        # Observation and logging failures must not replace the focus result.
        pass
