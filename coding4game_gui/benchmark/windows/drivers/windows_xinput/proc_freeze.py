"""True process freeze for Windows — suspend/resume every thread of a process.

xspeedhack's `set_speed(0.0)` only zeroes the game's *timescale*. Some titles
keep animation, physics and particle subsystems ticking on unhooked
real-time clocks, so the game is never *fully* paused. Suspending the process at
the OS level (`NtSuspendProcess`) freezes ALL its threads — a complete, engine-
independent pause, used while the agent deliberates (API wait + reasoning).

`NtSuspendProcess`/`NtResumeProcess` are undocumented but stable ntdll exports
widely used for exactly this. They suspend/resume the whole process atomically
(unlike enumerating threads + SuspendThread, which races thread creation).
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

_PROCESS_SUSPEND_RESUME = 0x0800

try:
    _ntdll = ctypes.WinDLL("ntdll")
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _AVAILABLE = True
except Exception:  # noqa: BLE001 — non-Windows / restricted env
    _AVAILABLE = False


def available() -> bool:
    return _AVAILABLE


def _with_handle(pid: int, fn, failure_details=None) -> bool:
    if not _AVAILABLE or not pid:
        if failure_details is not None:
            failure_details.update(stage="unavailable" if not _AVAILABLE else "missing_pid")
        return False
    handle = _kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, int(pid))
    if not handle:
        error = ctypes.get_last_error()
        if failure_details is not None:
            failure_details.update(stage="open_process", winerror=error)
        raise OSError(f"OpenProcess(pid={pid}) failed (err {error})")
    try:
        status = fn(handle)          # NTSTATUS: 0 == STATUS_SUCCESS
        if status != 0 and failure_details is not None:
            failure_details.update(stage="nt_call", ntstatus=f"0x{status & 0xffffffff:08X}")
        return status == 0
    finally:
        _kernel32.CloseHandle(handle)


def suspend_process(pid: int, *, failure_details=None) -> bool:
    """Suspend every thread of the process. Returns True on success."""
    return _with_handle(pid, _ntdll.NtSuspendProcess, failure_details)


def resume_process(pid: int) -> bool:
    """Resume every thread of a previously suspended process."""
    return _with_handle(pid, _ntdll.NtResumeProcess)
