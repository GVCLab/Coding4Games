"""Scoped Windows input-context preparation; no gameplay or layout changes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time

from coding4game_gui.benchmark.evidence.event_log import emit

_NATIVE = Path(__file__).with_name('native_ime')


def _verified_components() -> tuple[Path, Path]:
    manifest = json.loads((_NATIVE / 'manifest.json').read_text(encoding='utf-8'))
    paths = []
    for name in ('OwnedImeController.exe', 'owned_ime_context32.dll'):
        path = _NATIVE / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest['sha256'][name]:
            raise RuntimeError(f'owned IME component identity differs: {name}')
        paths.append(path)
    return tuple(paths)


def _target(hwnd: int, pid: int, process_name: str, created: float | None = None,
            *, foreground: bool) -> dict:
    import ctypes
    from ctypes import wintypes
    import psutil
    import win32gui
    import win32process

    tid, owner = win32process.GetWindowThreadProcessId(hwnd)
    process = psutil.Process(pid)
    actual_created = process.create_time()
    if (owner != pid or process.name().casefold() != process_name.casefold()
            or (created is not None and actual_created != created)
            or (foreground and win32gui.GetForegroundWindow() != hwnd)):
        raise RuntimeError('owned IME target identity or foreground differs')
    user = ctypes.WinDLL('user32')
    user.GetKeyboardLayout.argtypes = (wintypes.DWORD,)
    user.GetKeyboardLayout.restype = ctypes.c_void_p
    return dict(hwnd=hwnd, pid=pid, tid=tid, created=actual_created,
                window_class=win32gui.GetClassName(hwnd), process_name=process_name,
                game_hkl=int(user.GetKeyboardLayout(tid) or 0),
                host_hkl=int(user.GetKeyboardLayout(0) or 0))


def _request(target: dict, operation: int, *, foreground: bool) -> dict:
    helper, library = _verified_components()
    before = _target(target['hwnd'], target['pid'], target['process_name'],
                     target['created'], foreground=foreground)
    started = time.perf_counter()
    emit('owned_ime_context', 'begin', target=before,
         operation='detach' if operation == 1 else 'restore')
    with tempfile.TemporaryDirectory(prefix='c4g-owned-ime-') as temporary:
        receipt = Path(temporary) / 'request.json'
        try:
            completed = subprocess.run([
                str(helper), str(before['hwnd'] & 0xffffffff), str(before['pid']),
                before['window_class'], str(operation), str(library), str(receipt),
                f"{before['created']:.6f}", before['process_name']],
                capture_output=True, text=True, timeout=3,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            native = json.loads(receipt.read_text(encoding='utf-8')) if receipt.is_file() else {}
        except Exception as error:
            emit('owned_ime_context', 'end', status='failed', target=before,
                 operation='detach' if operation == 1 else 'restore',
                 error=f'{type(error).__name__}: {error}', result_ambiguous=True)
            raise
    after = _target(before['hwnd'], before['pid'], before['process_name'],
                    before['created'], foreground=foreground)
    record = dict(operation='detach' if operation == 1 else 'restore',
                  target=before, native=native, helper_returncode=completed.returncode,
                  seconds=time.perf_counter() - started,
                  component_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in (helper, library)})
    success = (completed.returncode == 0 and native.get('status') == 'verified'
               and native.get('state') == operation and native.get('hook_removed') is True
               and native.get('pid') == before['pid'] and native.get('tid') == before['tid']
               and native.get('hwnd') == (before['hwnd'] & 0xffffffff)
               and native.get('layout_preserved') is True
               and before['game_hkl'] == after['game_hkl']
               and before['host_hkl'] == after['host_hkl'])
    emit('owned_ime_context', 'end', status='verified' if success else 'failed', **record)
    if not success:
        raise RuntimeError(f"owned IME {record['operation']} could not be verified")
    return record


def prepare_owned_ime_context(env, process_name: str) -> dict:
    """Detach one explicitly configured x86 HWND before the existing freeze."""
    if getattr(env, 'hard_pause', True) or env.game_arch != 'x86':
        raise RuntimeError('owned IME preparation requires a live x86 game window')
    target = _target(env.renderer.hwnd, env.renderer.pid, process_name, foreground=True)
    record = _request(target, 1, foreground=True)
    env._owned_ime_context = target
    return record


def restore_owned_ime_context(env) -> dict | None:
    """Restore the saved association only while its exact owned window is live."""
    target = getattr(env, '_owned_ime_context', None)
    if target is None:
        return None
    import win32gui
    if not win32gui.IsWindow(target['hwnd']):
        env._owned_ime_context = None
        return None
    if getattr(env, '_suspended', False):
        emit('owned_ime_context', 'end', operation='restore', status='deferred_to_window_destruction',
             target=target)
        return None
    result = _request(target, 2, foreground=False)
    env._owned_ime_context = None
    return result
