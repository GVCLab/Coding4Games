"""Static save-state fixture: restore a golden snapshot before each run.

Each game can declare a `save_state:` block in its YAML pointing to a
golden zip of its save directory. Before the env starts, this module:

  1. Kills the game process (so the OS releases file handles on the save).
  2. Optionally wipes the destination paths the zip will overwrite.
  3. Extracts the golden zip into `restore_to`.
  4. Optionally relaunches the game and waits for the process to appear.

The agent then plays from an identical world state on every run, so
evaluation results are comparable across runs / models / dates.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Optional

import psutil

from coding4game_gui.paths import REPO_ROOT


_RESTORE_TO_REQUIRED = "save_state.restore_to is required; set the verified directory in games.local.json"


@dataclass
class SaveStateSpec:
    # Metadata-only loading can inspect a declared, locally supplied target.
    restore_to: Optional[Path]
    # `golden` can be omitted in YAML when you'll always pass --checkpoint NAME
    # (the CLI fills it in from snapshots/<game>_<NAME>.zip).
    golden: Optional[Path] = None
    wipe_before: bool = True
    launch: Optional[str] = None
    launch_timeout_seconds: float = 60.0
    post_launch_wait_seconds: float = 3.0
    extra_kill: list[str] = field(default_factory=list)
    # A command-line substring distinguishes games with a generic process name.
    process_cmdline_contains: Optional[str] = None


def _expand_path(raw: str, *, validate_files: bool = True) -> Path:
    """Expand %VAR% / ~ / glob in a path. Returns first match for globs."""
    from coding4game_gui.benchmark.windows.steam_paths import expand_steam_userdata
    raw = expand_steam_userdata(raw, required=validate_files)
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if "*" not in expanded and "?" not in expanded:
        from coding4game_gui.benchmark.windows.native_profile_io import is_profile_path
        if is_profile_path(Path(expanded)):
            # Resolve lexical components, not the desktop app's redirected
            # directory handle. Native writes are verified before restoration.
            return Path(os.path.abspath(expanded))
        return Path(expanded).resolve()
    p = Path(expanded)
    parts = p.parts
    for i, part in enumerate(parts):
        if "*" in part or "?" in part:
            base = Path(*parts[:i]) if i > 0 else Path(p.anchor or ".")
            rest = Path(*parts[i:])
            matches = sorted(base.glob(str(rest)))
            if not matches:
                if not validate_files:
                    return Path(expanded).resolve()
                raise FileNotFoundError(f"save_state: no path matched glob {raw!r}")
            return matches[0].resolve()
    return p.resolve()


def parse_save_state(data: dict[str, Any], *, validate_files: bool = True) -> SaveStateSpec:
    if not isinstance(data, dict):
        raise ValueError("save_state must be a YAML mapping")
    restore_raw = data.get("restore_to")
    if not restore_raw:
        if validate_files or restore_raw is not None or "restore_to" not in data:
            raise ValueError(_RESTORE_TO_REQUIRED)

    golden_raw = data.get("golden")
    golden: Optional[Path] = None
    if golden_raw:
        golden = (REPO_ROOT / str(golden_raw)).resolve()
        if validate_files and not golden.is_file():
            raise FileNotFoundError(f"save_state.golden not found: {golden}")

    return SaveStateSpec(
        restore_to=(_expand_path(str(restore_raw), validate_files=validate_files)
                    if restore_raw is not None else None),
        golden=golden,
        wipe_before=bool(data.get("wipe_before", True)),
        launch=data.get("launch"),
        launch_timeout_seconds=float(data.get("launch_timeout_seconds", 60.0)),
        post_launch_wait_seconds=float(data.get("post_launch_wait_seconds", 3.0)),
        extra_kill=list(data.get("extra_kill") or []),
        process_cmdline_contains=data.get("process_cmdline_contains") or None,
    )


def checkpoint_zip_path(game_name: str, checkpoint_name: str) -> Path:
    """Canonical location: <repo>/snapshots/<game>_<checkpoint>.zip."""
    return (REPO_ROOT / "snapshots" / f"{game_name}_{checkpoint_name}.zip").resolve()


def resolve_for_run(game_name: str, yaml_spec: Optional[SaveStateSpec],
                    checkpoint_name: Optional[str]) -> Optional[SaveStateSpec]:
    """Pick the SaveStateSpec a run should use.

    Precedence:
      - --checkpoint NAME passed: use snapshots/<game>_<NAME>.zip as golden.
        Other fields (restore_to, launch, ...) come from yaml_spec when set,
        otherwise restore_to is auto-resolved from the known-defaults registry.
      - yaml_spec set: use it as-is.
      - neither: None (no restore).
    """
    if yaml_spec is not None and yaml_spec.restore_to is None:
        raise ValueError(_RESTORE_TO_REQUIRED)
    if checkpoint_name:
        golden = checkpoint_zip_path(game_name, checkpoint_name)
        if not golden.is_file():
            raise FileNotFoundError(
                f"checkpoint not found: {golden}\n"
                f"Create it with: python scripts/calibration/checkpoint.py --game {game_name} {checkpoint_name}"
            )
        if yaml_spec is not None:
            return dataclasses.replace(yaml_spec, golden=golden)
        from coding4game_gui.benchmark.windows.save_paths import default_save_path
        return SaveStateSpec(restore_to=default_save_path(game_name), golden=golden)
    if yaml_spec is not None and yaml_spec.golden is None:
        raise ValueError(
            f"games/{game_name}.yaml has save_state.restore_to but no save_state.golden, "
            f"and you didn't pass --checkpoint NAME. Either pass --checkpoint <name> "
            f"(after creating it with checkpoint.py) or add save_state.golden to the YAML."
        )
    return yaml_spec


def golden_sha256(spec: SaveStateSpec) -> str:
    h = hashlib.sha256()
    with open(spec.golden, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def kill_process(name: str, timeout: float = 5.0,
                 cmdline_contains: Optional[str] = None) -> int:
    """Terminate processes matching `name` (case-insensitive).

    If `cmdline_contains` is given, only kills processes whose command
    line contains that substring (case-insensitive). Useful when `name`
    is generic like "java.exe" and you want to skip IDEs, services, etc.

    Skips processes we can't touch (AccessDenied / NoSuchProcess /
    can't-read-cmdline) with a warning rather than crashing. Returns
    the number of processes actually terminated.
    """
    name_lower = name.lower()
    needle_lower = cmdline_contains.lower() if cmdline_contains else None

    candidates = []
    skipped_inaccessible = 0
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info.get("name") or "").lower() != name_lower:
                continue
            if needle_lower is not None:
                try:
                    cmdline = " ".join(p.cmdline())
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    skipped_inaccessible += 1
                    continue
                if needle_lower not in cmdline.lower():
                    continue
            candidates.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if skipped_inaccessible:
        print(f"  (skipped {skipped_inaccessible} {name} process(es) we can't read — "
              f"likely services or higher-privilege apps unrelated to this game)")

    killed = 0
    for p in candidates:
        try:
            p.terminate()
            killed += 1
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            print(f"  WARN: can't terminate {name} (PID {p.pid}) — likely admin/elevated. "
                  f"Skipping. If this IS your game, close it manually and rerun, or run "
                  f"this script as Administrator.")
            continue

    deadline = time.time() + timeout
    while time.time() < deadline:
        still_alive = []
        for p in candidates:
            try:
                if p.is_running():
                    still_alive.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if not still_alive:
            break
        time.sleep(0.1)

    for p in candidates:
        try:
            if p.is_running():
                p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed


def _zip_top_level(zip_path: Path) -> set[str]:
    names: set[str] = set()
    with zipfile.ZipFile(zip_path, "r") as z:
        for n in z.namelist():
            n = n.replace("\\", "/").lstrip("/")
            if not n:
                continue
            top = n.split("/", 1)[0]
            if top:
                names.add(top)
    return names


def wait_for_process(process_name: str, timeout: float = 60.0,
                     poll_interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    name_lower = process_name.lower()
    while time.time() < deadline:
        for p in psutil.process_iter(["name"]):
            try:
                if (p.info.get("name") or "").lower() == name_lower:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        time.sleep(poll_interval)
    return False


def wait_for_game_window(process_name: str, timeout: float = 300.0,
                         poll_interval: float = 1.0) -> bool:
    """Wait until `process_name` has a visible window with a non-empty title.

    Launchers can start the process before creating its titled game window.
    """
    import win32gui
    import win32process

    deadline = time.time() + timeout
    name_lower = process_name.lower()
    while time.time() < deadline:
        target_pids = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if (p.info.get("name") or "").lower() == name_lower:
                    target_pids.append(p.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if target_pids:
            found = [False]

            def _cb(hwnd, _):
                try:
                    _, owner_pid = win32process.GetWindowThreadProcessId(hwnd)
                except Exception:
                    return True
                if owner_pid in target_pids:
                    title = win32gui.GetWindowText(hwnd)
                    if title and win32gui.IsWindowVisible(hwnd):
                        found[0] = True
                return True

            try:
                win32gui.EnumWindows(_cb, None)
            except Exception:
                pass
            if found[0]:
                return True

        time.sleep(poll_interval)
    return False


def restore(spec: SaveStateSpec, process_name: str) -> dict:
    """Kill game, wipe, extract golden, optionally relaunch and await process.

    Returns a report dict (logged to META.json so each run is self-describing).
    """
    restore_to = spec.restore_to
    if restore_to is None:
        raise ValueError(_RESTORE_TO_REQUIRED)
    if spec.golden is None:
        raise ValueError("restore() requires spec.golden; pass --checkpoint or set save_state.golden in YAML")
    from coding4game_gui.benchmark.windows.native_profile_io import needs_native_io, restore_native
    if needs_native_io(restore_to):
        return restore_native(spec, process_name)
    report: dict = {
        "golden": str(spec.golden),
        "restore_to": str(restore_to),
        "wipe_before": spec.wipe_before,
    }

    killed = kill_process(process_name, cmdline_contains=spec.process_cmdline_contains)
    for extra in spec.extra_kill:
        killed += kill_process(extra)
    report["killed_processes"] = killed
    if killed:
        # Windows can hold file handles briefly after process exit.
        time.sleep(1.0)

    restore_to.mkdir(parents=True, exist_ok=True)

    if spec.wipe_before:
        wiped: list[str] = []
        for name in _zip_top_level(spec.golden):
            target = restore_to / name
            if target.is_dir():
                shutil.rmtree(target)
                wiped.append(name)
            elif target.exists():
                target.unlink()
                wiped.append(name)
        report["wiped"] = wiped

    with zipfile.ZipFile(spec.golden, "r") as z:
        z.extractall(restore_to)
    report["extracted"] = True

    if spec.launch:
        subprocess.Popen(spec.launch, shell=True)
        report["launched"] = spec.launch
        if not wait_for_process(process_name, timeout=spec.launch_timeout_seconds):
            raise RuntimeError(
                f"save_state: process '{process_name}' did not appear within "
                f"{spec.launch_timeout_seconds:.0f}s after launching {spec.launch!r}"
            )
        time.sleep(spec.post_launch_wait_seconds)

    return report
