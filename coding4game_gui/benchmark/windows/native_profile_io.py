"""Keep host save preparation outside desktop-app AppData virtualization.

The desktop shell starts a fixed host worker when a write probe resolves to a
different directory. The worker verifies native writes before touching saves;
it never launches a game or changes the desktop application's configuration.
"""
from __future__ import annotations

import ctypes
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from coding4game_gui.paths import REPO_ROOT

_IN_WORKER = False


def _physical_path(path: Path) -> Path:
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID,
                                   w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateFileW(str(path), 0, 7, None, 3, 0, None)
    if handle == w.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        count = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not count or count >= len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        name = buffer.value
        if name.startswith("\\\\?\\UNC\\"):
            name = "\\\\" + name[8:]
        elif name.startswith("\\\\?\\"):
            name = name[4:]
        return Path(name)
    finally:
        kernel.CloseHandle(handle)


def _redirected_write(directory: Path) -> bool:
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / f".c4g-native-{uuid.uuid4().hex}.tmp"
    try:
        with probe.open("xb") as stream:
            stream.write(b"native profile probe\n")
        actual = _physical_path(probe)
        return os.path.normcase(str(actual)) != os.path.normcase(str(probe.absolute()))
    finally:
        probe.unlink(missing_ok=True)


def is_profile_path(directory: Path) -> bool:
    """Identify logical Windows profile targets without resolving an overlay."""
    if os.name != 'nt':
        return False
    path = directory.absolute()
    roots = [Path(value).absolute() for key in ("APPDATA", "LOCALAPPDATA")
             if (value := os.environ.get(key))]
    if profile := os.environ.get('USERPROFILE'):
        roots.append(Path(profile) / 'AppData' / 'LocalLow')
    return any(path.is_relative_to(root) for root in roots)


def needs_native_io(directory: Path) -> bool:
    """Inspect only Windows AppData targets; ordinary file operations stay local."""
    if _IN_WORKER or not is_profile_path(directory):
        return False
    return _redirected_write(directory.absolute())


def _desktop_shell():
    import win32com.client

    windows = win32com.client.Dispatch("Shell.Application").Windows()
    # Find the desktop even when no File Explorer folder window is open.
    desktop = windows.FindWindowSW(0, 0, 8, 0, 1)
    return desktop.Document.Application


def native_call(operation: str, payload: dict, *, timeout: float = 60.0):
    """Run one fixed preparation operation and retain its host-only receipt."""
    if operation not in {"restore_save", "kingdom_presentation", 'begin_save_session', 'restore_save_session', 'overcooked_presentation', 'sekiro_presentation_inspect'}:
        raise ValueError(f"unsupported native profile operation: {operation}")
    request_id = uuid.uuid4().hex
    directory = REPO_ROOT / "out" / "native_profile_io" / request_id
    directory.mkdir(parents=True)
    request = directory / "request.json"
    response = directory / "response.json"
    request.write_text(json.dumps({"id": request_id, "operation": operation,
                                   "payload": payload}), encoding="utf-8")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise RuntimeError("native profile preparation requires pythonw.exe")
    arguments = subprocess.list2cmdline([
        "-m", "coding4game_gui.benchmark.windows.native_profile_io", str(request)])
    import pythoncom

    # COM apartments belong to the calling request thread, not the process.
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    try:
        _desktop_shell().ShellExecute(str(pythonw), arguments, str(REPO_ROOT), "open", 0)
    finally:
        pythoncom.CoUninitialize()
    deadline = time.monotonic() + timeout
    while not response.is_file():
        if time.monotonic() >= deadline:
            pid_file = directory / "worker_pid.json"
            if pid_file.is_file():
                import psutil
                identity = json.loads(pid_file.read_text(encoding="utf-8"))
                try:
                    process = psutil.Process(identity["pid"])
                    if process.create_time() == identity["created"]:
                        process.kill()
                except psutil.NoSuchProcess:
                    pass
            raise RuntimeError(f"native profile preparation timed out; receipt: {directory}")
        time.sleep(.05)
    result = json.loads(response.read_text(encoding="utf-8"))
    if result.get("id") != request_id:
        raise RuntimeError("native profile receipt identity mismatch")
    if not result.get("ok"):
        raise RuntimeError(f"native profile preparation failed: {result.get('error')}")
    return result["result"]


def restore_native(spec, process_name: str) -> dict:
    payload = asdict(spec)
    payload["restore_to"] = str(spec.restore_to)
    payload["golden"] = str(spec.golden)
    return native_call("restore_save", {"spec": payload, "process": process_name})


def _inspect_sekiro_presentation(directory: Path) -> dict:
    """Read only display fields from the native game's graphics XML."""
    import hashlib
    import xml.etree.ElementTree as ET

    path = directory / 'GraphicsConfig.xml'
    if not path.is_file():
        return {'path': str(path), 'exists': False}
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise ValueError('Sekiro graphics configuration exceeds the inspection limit')
    root = ET.fromstring(raw)
    fields = {child.tag: child.text for child in root
              if child.tag == 'ScreenMode' or child.tag.startswith('Resolution-')}
    return {'path': str(path), 'exists': True, 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'root_tag': root.tag, 'display_fields': fields}


def _run_request(request_path: Path) -> None:
    global _IN_WORKER
    import psutil

    identity = {"pid": os.getpid(), "created": psutil.Process().create_time()}
    (request_path.parent / "worker_pid.json").write_text(json.dumps(identity), encoding="utf-8")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    response = {"id": request["id"], "ok": False}
    try:
        payload = request["payload"]
        operation = request["operation"]
        if operation == "restore_save":
            directory = Path(payload["spec"]["restore_to"])
        elif operation == "kingdom_presentation":
            directory = Path(os.environ["APPDATA"]) / "kingdom_rush_alliance"
        elif operation == 'overcooked_presentation':
            directory = Path(os.environ['USERPROFILE']) / 'AppData/LocalLow/Team17/Overcooked2'
        elif operation == 'sekiro_presentation_inspect':
            directory = Path(os.environ['APPDATA']) / 'Sekiro'
        elif operation == 'begin_save_session':
            from coding4game_gui.benchmark.spec import load_game_spec
            from coding4game_gui.benchmark.windows.games_config import load_game_config
            from coding4game_gui.benchmark.windows.save_state import resolve_for_run
            spec = load_game_spec(payload['scenario'], windows_input=payload.get('windows_input'))
            cfg = load_game_config(spec['driver_game'])
            resolved = resolve_for_run(spec['driver_game'], cfg.save_state,
                                       spec['startup']['golden_checkpoint'])
            # Probe the parent so an absent original account stays absent.
            directory = resolved.restore_to.parent
        elif operation == 'restore_save_session':
            journal = Path(payload['journal'])
            state = json.loads(journal.read_text(encoding='utf-8'))
            directory = Path(state['source']).parent
        else:
            raise ValueError(f"unsupported native profile operation: {operation}")
        if _redirected_write(directory):
            raise RuntimeError(f"desktop worker still redirects writes at {directory}")
        _IN_WORKER = True
        if operation == "restore_save":
            from coding4game_gui.benchmark.windows.save_state import SaveStateSpec, restore
            data = dict(payload["spec"])
            data["restore_to"] = Path(data["restore_to"])
            data["golden"] = Path(data["golden"])
            result = restore(SaveStateSpec(**data), payload["process"])
            result["native_io"] = {"method": "desktop_shell", "verified_directory": str(directory),
                                   "receipt": str(request_path.parent)}
        elif operation == 'kingdom_presentation':
            from coding4game_gui.benchmark.windows.game_settings import force_kingdom_rush_settings
            result = force_kingdom_rush_settings(tuple(payload["size"]), payload["mode"])
        elif operation == 'overcooked_presentation':
            from coding4game_gui.benchmark.windows.game_settings import prepare_overcooked_preferences
            result = prepare_overcooked_preferences(tuple(payload['size']), payload['mode'])
        elif operation == 'sekiro_presentation_inspect':
            result = _inspect_sekiro_presentation(directory)
        elif operation == 'begin_save_session':
            from coding4game_gui.benchmark.windows.save_session import begin_save_session, _write_json
            result = begin_save_session(spec, Path(payload['evidence']))
            result['native_io'] = {'method': 'desktop_shell', 'verified_directory': str(directory),
                                   'receipt': str(request_path.parent)}
            _write_json(Path(result['journal']), result)
        elif operation == 'restore_save_session':
            from coding4game_gui.benchmark.windows.save_session import restore
            result = restore(journal)
        response.update(ok=True, result=result)
    except Exception as exc:
        response["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _IN_WORKER = False
    temporary = request_path.parent / "response.tmp"
    temporary.write_text(json.dumps(response, indent=2), encoding="utf-8")
    temporary.replace(request_path.parent / "response.json")


if __name__ == "__main__":
    from coding4game_gui.benchmark.windows.native_profile_io import _run_request as run_worker
    run_worker(Path(sys.argv[1]))
