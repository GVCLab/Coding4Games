"""WindowsProcessLifecycle: find, stop and focus for the game process.

Wraps the existing pywin32 + psutil process controls into the ProcessLifecycle
protocol. `find()` is cached on the instance so the
bundle factory and the runner share one ProcessInfo.
"""

from __future__ import annotations

import platform
from typing import Optional

import psutil

from coding4game_gui.benchmark.windows.drivers.base import ProcessInfo

# Defer win32 imports so this module is importable on non-Windows hosts
# (CI, type checkers). The driver class still asserts Windows at __init__.
try:
    import win32api
    import win32con
    import win32gui
    import win32process
    _WIN32_AVAILABLE = True
    _WIN32_IMPORT_ERROR = None
except Exception as _exc:  # noqa: BLE001
    win32api = win32con = win32gui = win32process = None  # type: ignore
    _WIN32_AVAILABLE = False
    _WIN32_IMPORT_ERROR = _exc


def set_dpi_awareness() -> None:
    """Make this process per-monitor DPI aware.

    On a scaled display (e.g. Windows scaling at 175%) a DPI-UNAWARE process
    sees virtualized/logical window coordinates: GetWindowRect / GetClientRect
    report sizes divided by the scale factor, while PrintWindow renders the
    window at PHYSICAL pixels. The capture bitmap (sized from the logical rect)
    is then too small, so only the top-left ~1/scale of the game is captured —
    exactly the "screenshot only grabs part of the screen" symptom.

    Declaring per-monitor awareness makes all window metrics PHYSICAL, so the
    rect, the PrintWindow render, and the dxcam screen region all agree.

    Must run before the first window query/capture. Idempotent: a process can
    only set awareness once; later calls fail harmlessly (already set)."""
    import ctypes

    # PER_MONITOR_AWARE_V2 (Win10 1703+) → shcore PER_MONITOR (Win8.1+) → system.
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # system-DPI-aware fallback
    except Exception:  # noqa: BLE001
        pass


def select_game_window(windows: list[dict]) -> Optional[dict]:
    """Choose a titled game window, excluding transient graphics proxies."""
    proxy_keywords = ("d3dproxywindow", "proxy", "helper", "overlay")
    return next((window for window in windows
                 if not any(word in window["title"].casefold()
                            for word in proxy_keywords)), None)


def _get_process_info_raw(process_name: str) -> Optional[dict]:
    """Locate a Windows process by exe name; return pid + window + arch.

    Returns None if no process matched. Picks the first non-proxy window.
    """
    results = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() != process_name.lower():
                continue
            pid = proc.info["pid"]

            try:
                handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, pid)
                is_wow64 = win32process.IsWow64Process(handle)
                win32api.CloseHandle(handle)
                arch = "x86" if is_wow64 else "x64"
            except Exception:
                arch = "unknown"

            windows: list[dict] = []

            def _enum_cb(hwnd, target_pid):
                _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
                if found_pid == target_pid:
                    title = win32gui.GetWindowText(hwnd)
                    if title and win32gui.IsWindowVisible(hwnd):
                        windows.append({"hwnd": hwnd, "title": title})
                return True

            try:
                win32gui.EnumWindows(_enum_cb, pid)
            except Exception:
                pass

            window_name = None
            window_hwnd = None
            selected = select_game_window(windows)
            if selected is not None:
                window_name = selected["title"]
                window_hwnd = selected["hwnd"]

            results.append({
                "pid": pid, "window_name": window_name,
                "window_hwnd": window_hwnd, "architecture": arch,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not results:
        return None
    # Prefer processes that actually own a visible window: a ZOMBIE (dead
    # process pinned in the table by a stale handle) or a hung windowless
    # relaunch matches by name but can never be driven — picking one poisoned
    # a whole bench run on 2026-07-09 ("no HWND found" on every boot).
    results.sort(key=lambda r: r["window_hwnd"] is None)
    if len(results) > 1:
        print(f"Warning: multiple '{process_name}' processes; using "
              f"PID {results[0]['pid']} "
              f"(window={results[0]['window_name']!r})")
    return results[0]


def _window_bbox(hwnd: int) -> tuple[int, int, int, int]:
    """Return (left, top, width, height) of the HWND's CLIENT area in screen
    coordinates — i.e. the game viewport WITHOUT the title bar or borders.

    Capturing the full window rect (GetWindowRect) drags the OS title bar and
    window chrome into every screenshot; for a windowed game that wastes the
    top strip on non-game pixels and squishes the frame. The client area is
    what the player actually sees. Falls back to the full window rect if the
    client rect is degenerate (e.g. a 0x0 minimized window)."""
    try:
        cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
        cw, ch = cr - cl, cb - ct
        if cw > 0 and ch > 0:
            sx, sy = win32gui.ClientToScreen(hwnd, (cl, ct))
            return int(sx), int(sy), int(cw), int(ch)
    except Exception:  # noqa: BLE001
        pass
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    return int(l), int(t), int(r - l), int(b - t)


class WindowsProcessLifecycle:
    def __init__(self, process_name: str):
        assert platform.system().lower() == "windows", "Windows only."
        if not _WIN32_AVAILABLE:
            raise RuntimeError(
                f"pywin32 is not importable: {_WIN32_IMPORT_ERROR!r}. "
                f"Install with `pip install pywin32`."
            )
        self.process_name = process_name
        self._found_cache: Optional[ProcessInfo] = None
        self.last_suspend_failure: Optional[dict] = None

    def find(self) -> Optional[ProcessInfo]:
        """Locate the process. Result is cached for window-bbox lookups."""
        raw = _get_process_info_raw(self.process_name)
        if raw is None:
            self._found_cache = None
            return None
        hwnd = raw.get("window_hwnd")
        bbox = _window_bbox(hwnd) if hwnd else (0, 0, 0, 0)
        info = ProcessInfo(
            pid=raw["pid"],
            window_title=raw.get("window_name"),
            arch=raw.get("architecture"),
            extras={"hwnd": hwnd, "bbox": bbox},
        )
        self._found_cache = info
        return info

    def _pid(self) -> Optional[int]:
        info = self._found_cache or self.find()
        return info.pid if info else None

    def suspend(self) -> bool:
        """Hard-freeze the game: suspend every thread of the process. Used while
        the agent deliberates so the game is COMPLETELY paused (beyond what an
        xspeedhack timescale=0 can achieve on UE4). Best-effort; returns False if
        the pid is unknown or the OS call fails."""
        from coding4game_gui.benchmark.windows.drivers.windows_xinput import proc_freeze
        self.last_suspend_failure = None
        pid = self._pid()
        if pid is None:
            self.last_suspend_failure = {"stage": "missing_pid"}
            return False
        details = {"pid": pid}
        try:
            frozen = proc_freeze.suspend_process(pid, failure_details=details)
        except OSError as error:
            details["error_type"] = type(error).__name__
            frozen = False
        if not frozen:
            # Read identity only after failure; successful pulses add no queries.
            # This observation is not proof of identity at the earlier OS call.
            try:
                process = psutil.Process(pid)
                details["process_create_time"] = process.create_time()
                details["process_status"] = process.status()
            except Exception as error:  # noqa: BLE001 — diagnostics must not replace the control result
                details["identity_error"] = type(error).__name__
            self.last_suspend_failure = details
        return frozen

    def resume(self) -> bool:
        """Resume a previously suspended game process."""
        from coding4game_gui.benchmark.windows.drivers.windows_xinput import proc_freeze
        pid = self._pid()
        if pid is None:
            return False
        try:
            return proc_freeze.resume_process(pid)
        except OSError:
            return False

    def kill(self) -> int:
        """Terminate every process matching `self.process_name`. Returns count."""
        from coding4game_gui.benchmark.windows import save_state as ss
        return ss.kill_process(self.process_name)

    def foreground(self) -> bool:
        """Foreground the same primary HWND selected for capture and input."""
        info = self.find()
        hwnd = info.extras.get("hwnd") if info is not None else None
        if not hwnd:
            return False
        from coding4game_gui.benchmark.windows.auto_nav import force_foreground
        return force_foreground(hwnd)
