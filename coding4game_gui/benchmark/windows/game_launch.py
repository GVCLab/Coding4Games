"""Direct executable launching for Windows games.

The benchmark deliberately does not initiate launches through Steam URLs or
other URI handlers.  A game executable may still start a platform client as
part of its own bootstrap/DRM flow; that behaviour belongs to the game, not to
the harness.

`launch_args` exists for titles that REFUSE to boot from their own executable.
Forza Horizon 5 is the measured case: it ships Xbox-GDK components alongside
Steamworks (PartyXboxLive.dll, app-container CRT variants, an installscript
that registers Xbox Live protocols), and launching ForzaHorizon5.exe directly
creates a window and then exits ~4s later, because the Xbox user context the
platform client establishes before process creation is missing.  Pointing
`launch_path` at steam.exe with `launch_args: ["-applaunch", "<appid>"]` boots
it reliably and is still an absolute local .exe, so the no-URI rule holds
unchanged.  Contrast Black Myth, a plain UE5 title whose Steamworks layer is an
optional online subsystem: it boots fine from its own exe and needs no args.

CAVEAT for callers that use the return value: when the launch goes through a
platform client, the returned Popen is the CLIENT's bootstrap process, not the
game.  It typically exits within seconds while the game keeps running, so it
cannot be used to detect a game crash.  Every current caller discards the
return value and instead polls for the configured `process:` name, which is
why routing through steam.exe changes nothing for them.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Protocol
from typing import Sequence


class _LaunchSpec(Protocol):
    launch_path: str | None
    # Optional: callers may pass a spec without this attribute at all (it is
    # read with getattr below), so plain launch_path-only specs stay valid.
    launch_args: Sequence[str] | None


class DirectLaunchError(RuntimeError):
    """The configured direct executable cannot be launched safely."""


class SteamLaunchPendingError(DirectLaunchError):
    """Steam has not settled; another launch request would be unsafe."""


def steam_app_id(spec: _LaunchSpec) -> str | None:
    """Identify an explicit local Steam application launch, without starting it."""
    if Path(spec.launch_path or '').name.casefold() != 'steam.exe':
        return None
    args = resolve_launch_args(spec)
    if len(args) >= 2 and args[0].casefold() == '-applaunch' and args[1].isdigit():
        return args[1]
    return None


def _steam_app_activity(appid: str) -> tuple[bool, bool]:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            rf'Software\Valve\Steam\Apps\{appid}') as key:
            values = []
            for name in ('Running', 'Updating'):
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    value = 0
                if value not in (0, 1):
                    raise ValueError('Unexpected Steam application activity value')
                values.append(bool(value))
            return tuple(values)
    except (OSError, ValueError) as exc:
        raise SteamLaunchPendingError(
            f'Cannot verify Steam application {appid} activity; no launch was sent') from exc


def _game_process_running(process_name: str) -> bool:
    import psutil
    return any((p.info.get('name') or '').casefold() == process_name.casefold()
               for p in psutil.process_iter(['name']))


def _steam_client_tracks_app(appid: str, steam_exe: str) -> bool:
    """Read Steam's running list, which can outlive the game's main process."""
    path = Path(steam_exe).parent / 'logs' / 'gameprocess_log.txt'
    try:
        with path.open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            added = re.compile(rf'\bAppID {re.escape(appid)} adding PID \d+ as a tracked process\b')
            removed = f'Remove {appid} from running list'
            span = 512 * 1024
            while True:
                start = max(0, size - span)
                stream.seek(start, os.SEEK_SET)
                lines = stream.read(size - start).decode('utf-8', errors='replace').splitlines()
                for line in reversed(lines):
                    if removed in line:
                        return False
                    if added.search(line):
                        return True
                if start == 0:
                    return False
                span *= 2
    except OSError as exc:
        raise SteamLaunchPendingError(
            f'Cannot verify Steam running-list state for app {appid}; no launch was sent') from exc


def wait_for_steam_app_idle(appid: str, process_name: str, *, timeout: float,
                          steam_exe: str | None = None) -> None:
    """Wait for tracked helpers/update work; never relaunch an existing game."""
    deadline = time.monotonic() + max(0.0, timeout)
    reported = False
    while True:
        if _game_process_running(process_name):
            raise SteamLaunchPendingError(
                f'{process_name} is already running before launch; no duplicate request was sent')
        running, updating = _steam_app_activity(appid)
        tracked = _steam_client_tracks_app(appid, steam_exe) if steam_exe else False
        if not running and not updating and not tracked:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SteamLaunchPendingError(
                f'Steam application {appid} is still busy; no duplicate request was sent')
        if not reported:
            print(f'[prelaunch] waiting for Steam application {appid} to become idle', flush=True)
            reported = True
        time.sleep(min(.25, remaining))


def resolve_launch_path(spec: _LaunchSpec) -> Path:
    """Resolve and validate the configured local executable path.

    File existence is checked at launch time rather than YAML parse time so
    configs remain inspectable on machines where a particular game is absent.
    """
    raw = (spec.launch_path or "").strip()
    if not raw:
        raise DirectLaunchError(
            "auto_launch.launch_path is required; URI/Steam launches are disabled"
        )
    if "://" in raw:
        raise DirectLaunchError(
            f"auto_launch.launch_path must be a local executable, not a URI: {raw!r}"
        )
    expanded = os.path.expandvars(os.path.expanduser(raw))
    path = Path(expanded)
    if not path.is_absolute():
        raise DirectLaunchError(
            f"auto_launch.launch_path must be absolute, got {raw!r}"
        )
    if path.suffix.lower() != ".exe":
        raise DirectLaunchError(
            f"auto_launch.launch_path must name a Windows .exe, got {raw!r}"
        )
    if not path.is_file():
        from coding4game_gui.benchmark.windows.steam_paths import relocated_executable
        try:
            relocated = relocated_executable(path)
        except ValueError as exc:
            raise DirectLaunchError(str(exc)) from exc
        if relocated is not None:
            return relocated
        raise DirectLaunchError(
            f"auto_launch executable does not exist: {path}"
        )
    return path


def resolve_launch_args(spec: _LaunchSpec) -> list[str]:
    """Resolve the optional argument list passed to the configured executable.

    Read with getattr so a spec object that predates this field (or a test
    double carrying only launch_path) stays valid and yields no arguments.
    """
    raw = getattr(spec, "launch_args", None)
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)):
        raise DirectLaunchError(
            f"auto_launch.launch_args must be a list of strings, got {raw!r}"
        )
    args: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            raise DirectLaunchError(
                "auto_launch.launch_args entries must be strings, got "
                f"{value!r}"
            )
        args.append(os.path.expandvars(os.path.expanduser(value)))
    return args


def _launch_desktop_executable(path: Path, args: list[str]) -> None:
    """Ask the existing desktop shell to open one validated local executable."""
    if os.name != "nt":
        raise DirectLaunchError("desktop_shell launch requires Windows")
    import pythoncom
    import win32com.client

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    try:
        windows = win32com.client.Dispatch("Shell.Application").Windows()
        desktop = windows.FindWindowSW(0, 0, 8, 0, 1)
        desktop.Document.Application.ShellExecute(
            str(path), subprocess.list2cmdline(args), str(path.parent), "open", 1)
    finally:
        pythoncom.CoUninitialize()


def launch_game(spec: _LaunchSpec) -> subprocess.Popen | None:
    """Start the configured EXE directly and return its bootstrap process.

    See the module docstring before using the return value: with `launch_args`
    routing through a platform client, this is the client's process and not the
    game's. Explicit `desktop_shell` launch returns no bootstrap handle; the
    caller must use its ordinary configured process/window wait.
    """
    path = resolve_launch_path(spec)
    if str(path).casefold() != str(Path(os.path.expandvars(
            os.path.expanduser(spec.launch_path)))).casefold():
        print(f"[launch] resolved installed executable: {path}", flush=True)
    args = resolve_launch_args(spec)
    method = getattr(spec, "launch_method", "process")
    if method == "desktop_shell":
        print("[launch] opening local executable through the desktop shell", flush=True)
        _launch_desktop_executable(path, args)
        # The shell owns process creation. Callers wait for the configured
        # game process/window rather than requiring a bootstrap handle.
        return None
    if method != "process":
        raise DirectLaunchError(f"unsupported launch_method: {method!r}")
    return subprocess.Popen(
        [str(path), *args],
        cwd=str(path.parent),
        close_fds=True,
    )
