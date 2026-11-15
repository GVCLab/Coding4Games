"""Discover installed Steam libraries without launching a client or reading secrets."""
from __future__ import annotations

import os
import datetime
from pathlib import Path
import re


def steam_install_roots() -> list[Path]:
    candidates = []
    try:
        import winreg
        for hive, key, names in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", ("SteamPath", "SteamExe")),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", ("InstallPath",)),
        ):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    for name in names:
                        try:
                            value = Path(winreg.QueryValueEx(handle, name)[0])
                            candidates.append(value.parent if name == "SteamExe" else value)
                        except FileNotFoundError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / "Steam")
    return _unique_existing(candidates)


def _unique_existing(paths) -> list[Path]:
    result, seen = [], set()
    for path in paths:
        path = Path(path).resolve()
        key = str(path).casefold()
        if key not in seen and path.is_dir():
            seen.add(key)
            result.append(path)
    return result


def steam_library_roots(install_roots=None) -> list[Path]:
    roots = steam_install_roots() if install_roots is None else list(install_roots)
    libraries = list(roots)
    for root in roots:
        manifest = Path(root) / "steamapps" / "libraryfolders.vdf"
        if not manifest.is_file():
            continue
        content = manifest.read_text(encoding="utf-8", errors="replace")
        for raw in re.findall(r'"path"\s+"((?:\\.|[^"\\])*)"', content):
            candidate = Path(raw.replace("\\\\", "\\"))
            if candidate.is_absolute():
                libraries.append(candidate)
    return _unique_existing(libraries)


def _registry_active_account_id() -> int | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Valve\Steam\ActiveProcess") as handle:
            account = int(winreg.QueryValueEx(handle, "ActiveUser")[0])
    except (ImportError, OSError, TypeError, ValueError):
        return None
    return account if 0 < account < 2 ** 32 else None


def _steam_client_start_times(root: Path) -> list[float]:
    """Identify live clients by their exact installed executable, without input."""
    import psutil

    expected = str((root / 'steam.exe').resolve()).casefold()
    starts = []
    for proc in psutil.process_iter(['name', 'exe', 'create_time']):
        try:
            if (proc.info.get('name') or '').casefold() != 'steam.exe':
                continue
            exe = proc.info.get('exe')
            if exe and str(Path(exe).resolve()).casefold() == expected:
                starts.append(float(proc.info['create_time']))
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, TypeError):
            continue
    return starts


def _account_from_live_connection_log(path: Path, started: float) -> int | None:
    try:
        with path.open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 256 * 1024))
            text = stream.read().decode('utf-8', errors='replace')
    except OSError:
        return None
    # A partially appended state change cannot establish a current identity.
    if not text.endswith('\n'):
        return None
    account = None
    for line in text.splitlines():
        stamp = re.match(r'^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]', line)
        if not stamp:
            continue
        try:
            observed = datetime.datetime.strptime(stamp[1], '%Y-%m-%d %H:%M:%S').timestamp()
        except ValueError:
            continue
        if observed < started:
            continue
        if any(marker in line for marker in (
                'RecvMsgClientLoggedOff(', 'AsyncDisconnect(',
                'Sending SteamServersDisconnected_t', 'ConnectionDisconnected(',
                'Lost device', '[Logging On,', '[Connecting,')):
            account = None
        identity = re.search(r'\[U:1:(\d+)\]', line)
        if ('[Logged On,' in line and identity and
                'RecvMsgClientLogOnResponse() : processing complete' in line):
            value = int(identity[1])
            account = value if 0 < value < 2 ** 32 else None
    return account


def active_steam_account_id() -> int | None:
    """Use registry identity or a verified live client's completed logon."""
    account = _registry_active_account_id()
    if account is not None:
        return account
    roots = steam_install_roots()
    if len(roots) != 1:
        return None
    starts = _steam_client_start_times(roots[0])
    if len(starts) != 1:
        return None
    return _account_from_live_connection_log(roots[0] / 'logs/connection_log.txt', starts[0])


def steam_active_userdata_root() -> Path:
    """Resolve only the logged-in account; never select an arbitrary profile."""
    account = active_steam_account_id()
    if account is None:
        raise RuntimeError("Steam active account is unavailable; log in before restoring a checkpoint")
    roots = steam_install_roots()
    if len(roots) != 1:
        raise RuntimeError("Steam install is unavailable or ambiguous; cannot resolve account saves")
    return roots[0] / "userdata" / str(account)


def expand_steam_userdata(raw: str, *, required: bool = True) -> str:
    if "%STEAM_USERDATA%" not in raw.upper():
        return raw
    try:
        root = steam_active_userdata_root()
    except RuntimeError:
        if required:
            raise
        return raw
    return re.sub("%STEAM_USERDATA%", lambda _match: str(root), raw, flags=re.IGNORECASE)


def relocated_executable(configured: Path, *, install_roots=None) -> Path | None:
    """Relocate an exact Steam-relative EXE; never guess a game or pick a match."""
    roots = steam_install_roots() if install_roots is None else list(install_roots)
    normalized = str(configured).replace("\\", "/")
    marker = "/steamapps/common/"
    offset = normalized.casefold().find(marker)
    if configured.name.casefold() == "steam.exe":
        candidates = [Path(root) / "steam.exe" for root in roots]
    elif offset >= 0:
        suffix = normalized[offset + len(marker):]
        if not suffix or any(part in {"", ".", ".."} for part in suffix.split("/")):
            return None
        candidates = [root / "steamapps" / "common" / suffix
                      for root in steam_library_roots(roots)]
    else:
        return None
    matches = {str(path.resolve()).casefold(): path.resolve()
               for path in candidates if path.is_file()}
    if len(matches) > 1:
        raise ValueError(f"multiple installed executables match {configured}; select an explicit path")
    return next(iter(matches.values()), None)
