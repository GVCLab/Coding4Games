"""Force a game's window geometry before launch, so a run is reproducible.

WHY THIS EXISTS. Fullscreen geometry is a property of the MACHINE, not of the
repo: the render size is the monitor's resolution divided by its DPI scaling,
so the same code sees a different frame on every box. Every calibrated pixel
— click coordinates, OCR rects — silently means something different. Measured
on this project: coordinates calibrated against 1920x1080 (1536x864 logical)
met a 1600x900 screen (1280x720 logical), and the cold-start clicks stopped
landing. Nothing in the logs named the resolution.

A checked-in scenario therefore has to DECLARE its geometry and make the game
match it, rather than adapt to whatever it finds. Windowed mode is what makes
that possible at all — a window can be 1280x720 on any display, while
fullscreen cannot.

Games load their preferences on startup and may rewrite them on exit. Apply
the registered settings during cold-start preparation while the game is closed.
"""
from __future__ import annotations

import os
import re
import shutil
import zlib
from pathlib import Path
from typing import Optional
from typing import Tuple

SCREENMODE_RE = re.compile(r'(\["screenmode"\]=")([^"]*)(")')
SCREEN_W_RE = re.compile(r'(\["screen_res"\]=\{\["w"\]=)(\d+)')
SCREEN_H_RE = re.compile(r'(\["h"\]=)(\d+)')


def balatro_settings_target() -> Path:
    """Return the host's logical target even before the game has run once."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("cannot prepare Balatro settings: APPDATA is unavailable")
    return Path(appdata) / "Balatro" / "settings.jkr"


def balatro_settings_path() -> Optional[Path]:
    p = balatro_settings_target()
    return p if p.exists() else None


def balatro_template() -> str:
    """Infrastructure preferences only; game defaults supply other fields."""
    return (Path(__file__).parent / "settings_templates/balatro-v1.lua").read_text(
        encoding="utf-8").strip()


def _read(path: Path) -> str:
    return zlib.decompress(path.read_bytes(), -15).decode("utf-8", "replace")


def _write(path: Path, text: str) -> None:
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    blob = comp.compress(text.encode("utf-8")) + comp.flush()
    path.write_bytes(blob)


def normalize_balatro_tutorial(text: str) -> str:
    """Edit only top-level tutorial fields in the game's serialized Lua table."""
    match = re.fullmatch(r'\s*return\s*\{(.*)\}\s*', text, re.S)
    if not match:
        raise ValueError('unsupported Balatro settings table')
    body = match.group(1)
    parts, start, depth, quote, escaped = [], 0, 0, None, False
    for index, char in enumerate(body):
        if quote:
            if escaped: escaped = False
            elif char == '\\': escaped = True
            elif char == quote: quote = None
        elif char in ('"', "'"): quote = char
        elif char == '{': depth += 1
        elif char == '}':
            depth -= 1
            if depth < 0: raise ValueError('unbalanced Balatro settings')
        elif char == ',' and depth == 0:
            parts.append(body[start:index]); start = index + 1
    if quote or depth:
        raise ValueError('unbalanced Balatro settings')
    parts.append(body[start:])
    retained, completed, progress = [], [], False
    for part in parts:
        key = re.match(r'''\s*\[\s*["'](tutorial_complete|tutorial_progress)["']\s*\]\s*=\s*(.*)''', part, re.S)
        if not key:
            retained.append(part)
        elif key[1] == 'tutorial_complete': completed.append(key[2].strip())
        else: progress = True
    if completed == ['true'] and not progress:
        return text
    retained = [part for part in retained if part.strip()]
    retained.append('["tutorial_complete"]=true')
    return 'return {' + ','.join(retained) + ',}'


def verify_balatro_tutorial_disabled() -> None:
    """Fail closed if persisted tutorial preparation is missing or changed."""
    path = balatro_settings_path()
    if path is None:
        raise RuntimeError(
            f'Balatro settings are missing at {balatro_settings_target()}; '
            'tutorial state is unverified')
    text = _read(path)
    if normalize_balatro_tutorial(text) != text:
        raise RuntimeError('Balatro tutorial state is not disabled; refusing policy handoff')


def force_balatro_window(size: Tuple[int, int],
                         mode: str = "Windowed",
                         exe_path: Optional[str] = None) -> Optional[str]:
    """Make Balatro start windowed at `size`. Returns a description, or None.

    Backs the original up once (`settings.jkr.orig`) so a developer's own
    preferences are recoverable — this edits a file outside the repo, under
    the user's profile, which is not ours to lose.
    """
    path = balatro_settings_path()
    if path is None:
        path = balatro_settings_target()
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, balatro_template())
    try:
        text = _read(path)
    except Exception as e:  # noqa: BLE001
        # The benchmark startup verifies preparation before launching.
        raise RuntimeError(f"cannot read Balatro settings: {path}") from e

    backup = path.with_suffix(".jkr.orig")
    if not backup.exists():
        try:
            shutil.copy2(path, backup)
        except OSError as e:
            raise RuntimeError(f"cannot back up Balatro settings: {path}") from e

    before = text
    text = normalize_balatro_tutorial(text)
    # A FRESH INSTALL is the case that matters for "clone it and run it".
    # Two of its defaults stop a run dead, and both were set by hand on the
    # development machine, which is exactly the kind of undeclared local state
    # that makes a benchmark unreproducible:
    #   * the tutorial locks input and forces the first shop's contents, so an
    #     agent cannot act and the run it does get is not the run anyone else
    #     gets;
    #   * the CRT filter overlays scanlines and curvature on every frame,
    #     which is noise the OCR has to see through for no benefit.
    for key, want in (("crt", "0"),
                      ("bloom", "0")):
        text = re.sub(rf'(\["{key}"\]=)[^,}}]+', rf"\g<1>{want}", text)
    text = SCREENMODE_RE.sub(rf"\g<1>{mode}\g<3>", text, count=1)
    text = SCREEN_W_RE.sub(rf"\g<1>{size[0]}", text)
    # `h` appears inside screen_res only right after `w`, so bound the
    # substitution to that neighbourhood rather than rewriting every "h".
    def _h_after_w(m):
        return f'{m.group(1)}{size[0]},["h"]={size[1]}'
    text = re.sub(r'(\["screen_res"\]=\{\["w"\]=)\d+,\["h"\]=\d+',
                  _h_after_w, text)
    if text == before:
        verify_balatro_tutorial_disabled()
        return f"already {mode} {size[0]}x{size[1]}, tutorial off, CRT off"
    try:
        _write(path, text)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"cannot prepare Balatro settings: {path}") from e
    verify_balatro_tutorial_disabled()
    return f"{mode} {size[0]}x{size[1]}, tutorial off, CRT off"


def windows_documents_dir() -> Path:
    """Resolve the current user's Documents known folder."""
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(32768)
            # CSIDL_PERSONAL (5), SHGFP_TYPE_CURRENT (0). This also resolves a
            # redirected Documents folder instead of assuming home/Documents.
            if ctypes.windll.shell32.SHGetFolderPathW(
                    None, 5, None, 0, buf) == 0 and buf.value:
                return Path(buf.value)
        except Exception:  # noqa: BLE001 - retain a conventional fallback
            pass
    return Path.home() / "Documents"


# --------------------------------------------------------------------------
# Riders Republic
#
# The game keeps presentation settings in a plain INI below Documents. The
# benchmark scorer and prompt use English UI literals, so language and window
# geometry must be prepared while the game is closed before every cold start.
# --------------------------------------------------------------------------

def riders_republic_options_path(
        documents_dir: Optional[Path] = None) -> Optional[Path]:
    root = Path(documents_dir) if documents_dir is not None \
        else windows_documents_dir()
    path = root / "My Games" / "Riders Republic" / "Default" / "options.ini"
    return path if path.is_file() else None


def _patch_ini_section(text: str, section: str,
                       wanted: dict[str, str]) -> tuple[str, list[str]]:
    pattern = re.compile(
        rf'(^\[{re.escape(section)}\]\s*$)(.*?)(?=^\[|\Z)',
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"missing [{section}] section")
    body = match.group(2)
    changed = []
    for key, value in wanted.items():
        key_pattern = re.compile(
            rf'^(\s*{re.escape(key)}\s*=).*$' , re.MULTILINE)
        if key_pattern.search(body) is None:
            raise ValueError(f"missing {key} in [{section}]")
        updated = key_pattern.sub(rf'\g<1>{value}', body, count=1)
        if updated != body:
            changed.append(f"{section}.{key}={value}")
            body = updated
    return text[:match.start(2)] + body + text[match.end(2):], changed


def force_riders_republic_settings(
        size: Tuple[int, int], mode: str = "Windowed",
        exe_path: Optional[str] = None,
        documents_dir: Optional[Path] = None) -> Optional[str]:
    """Force the installed Riders Republic UI to English and fixed geometry."""
    del exe_path
    path = riders_republic_options_path(documents_dir)
    if path is None:
        raise RuntimeError("Riders Republic options.ini is missing")
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
        text, language_changes = _patch_ini_section(text, "Language", {
            "text": "en-US",
            "subtitles": "en-US",
            "sound": "en-US",
            "uplay": "en-US",
        })
        text, display_changes = _patch_ini_section(text, "Display Settings", {
            "WindowMode": "0" if mode.lower().startswith("window") else "1",
            "WindowedWidth": str(int(size[0])),
            "WindowedHeight": str(int(size[1])),
        })
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(
            f"cannot prepare Riders Republic settings: {path}") from exc

    changes = language_changes + display_changes
    if not changes:
        return f"already {mode} {size[0]}x{size[1]}, EN"
    backup = path.with_suffix(path.suffix + ".orig")
    if not backup.exists():
        try:
            shutil.copy2(path, backup)
        except OSError as exc:
            raise RuntimeError(
                f"cannot back up Riders Republic settings: {path}") from exc
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"cannot write Riders Republic settings: {path}") from exc
    return f"{mode} {size[0]}x{size[1]}, EN; changed " + ", ".join(changes)


def force_dpi_unaware_off(exe_path: str) -> Optional[str]:
    """Tell Windows to stop DPI-virtualising a game.

    A DPI-UNAWARE game renders at monitor_resolution / dpi_scale and lets
    Windows stretch the result, which costs the bench twice over: the capture
    comes back at that reduced logical size, and reading calibrated rects on
    it means upscaling interpolated pixels. Measured on Balatro under 125%
    scaling: a 1280x720 window became a 1228x690 surface, and the blind
    screen's small "Select" label — perfectly legible at native size —
    stopped being recognised once blown up to 1536x864, so a text-targeted
    click could never resolve.

    Marking the executable HIGHDPIAWARE removes the whole layer: the game
    draws at true pixels, the capture is sharp, and a larger window fits the
    same screen. This is a per-USER compatibility flag (the same one the
    Properties dialog writes), so it is reversible and needs no admin rights.
    """
    try:
        import winreg
    except ImportError:      # not Windows
        return None
    # YAML paths use forward slashes, but AppCompat value names are matched
    # against the native Windows spelling. Without normalization the registry
    # entry looks valid yet is ignored by the launched process.
    exe_path = os.path.normpath(exe_path)
    key = r"Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers"
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0,
                                winreg.KEY_READ | winreg.KEY_WRITE) as h:
            try:
                cur, _ = winreg.QueryValueEx(h, exe_path)
            except FileNotFoundError:
                cur = ""
            if "HIGHDPIAWARE" in (cur or ""):
                return "DPI compatibility override already configured"
            val = ((cur + " ") if cur else "~ ") + "HIGHDPIAWARE"
            winreg.SetValueEx(h, exe_path, 0, winreg.REG_SZ, val.strip())
            return f"DPI override -> {val.strip()!r}"
    except Exception as e:  # noqa: BLE001 — never block a run over a flag
        print(f"[settings] cannot set the DPI override: {e}")
        return None


def clear_balatro_saved_run() -> Optional[str]:
    """Remove any in-progress run so the cold path is always the same.

    Balatro's title-screen PLAY is context sensitive: with a saved run it
    RESUMES that run directly, skipping the New Run page entirely. The
    scripted cold start expects that page, so it looked like a click that
    never landed — while the game had in fact gone straight into a half
    finished game, complete with whatever jokers and money the previous run
    had accumulated. Two things were wrong at once: navigation could not
    proceed, and an episode would have started from a state no other machine
    shares.

    Deleting the save makes PLAY mean "new run" every time.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    removed = []
    for sub in ("1", "2", "3"):          # Balatro numbers its profiles
        p = Path(appdata) / "Balatro" / sub / "save.jkr"
        if p.exists():
            try:
                p.unlink()
                removed.append(f"{sub}/save.jkr")
            except OSError as e:
                print(f"[settings] cannot remove {p}: {e}")
    return ("cleared " + ", ".join(removed)) if removed else None


# --------------------------------------------------------------------------
# Brotato
#
# Brotato keeps permanent progression (save_v3_0*) and the resumable current
# run (run_v3_0*) beside each other under a per-SteamID directory.  A benchmark
# must remove only the latter: deleting save_v3_0* would erase unlocks, while
# leaving any run backup can make Resume reappear after an interrupted episode.
# --------------------------------------------------------------------------

_STEAM_ID64_ACCOUNT_BASE = 76561197960265728


def _active_steam_account_id() -> Optional[int]:
    """Return Steam's active 32-bit account id without guessing a save dir."""
    from coding4game_gui.benchmark.windows.steam_paths import active_steam_account_id
    return active_steam_account_id()


def _brotato_active_save_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError(
            "cannot clear Brotato Resume: APPDATA is unavailable")
    account_id = _active_steam_account_id()
    if account_id is None:
        raise RuntimeError(
            "cannot clear Brotato Resume: active Steam account is unknown")
    steam_id = str(_STEAM_ID64_ACCOUNT_BASE + account_id)
    return Path(appdata) / "Brotato" / steam_id


def clear_brotato_saved_run(_exe_path: Optional[str] = None) -> Optional[str]:
    """Delete only the active account's resumable run, preserving progression."""
    root = _brotato_active_save_dir()
    if not root.is_dir():
        return None

    marker = root / "steam_autocloud.vdf"
    if marker.is_file():
        try:
            text = marker.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RuntimeError(
                f"cannot validate Brotato Steam account marker {marker}: {exc}"
            ) from exc
        match = re.search(r'"accountid"\s+"(\d+)"', text)
        active = _active_steam_account_id()
        if match and active is not None and int(match.group(1)) != active:
            raise RuntimeError(
                "refusing to clear Brotato Resume: save directory account "
                f"{match.group(1)} does not match active Steam account {active}")

    candidates = [root / "run_v3_0.json", root / "run_v3_0.json.bak"]
    candidates.extend(sorted(root.glob("run_v3_0_*.json.bak")))
    removed = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            path.unlink()
            removed.append(path.name)
        except OSError as exc:
            raise RuntimeError(
                f"cannot clear Brotato Resume file {path}: {exc}") from exc
    return ("cleared " + ", ".join(removed)) if removed else None


def force_brotato_settings(size: Tuple[int, int],
                           mode: str = "Windowed",
                           exe_path: Optional[str] = None) -> Optional[str]:
    """Make Brotato windowed, English, and gamepad-enabled for OCR/control.

    Brotato stores these infrastructure preferences next to the active Steam
    account's progression.  Preserve the original file once and change only
    presentation/input fields; permanent progression and gameplay choices are
    outside this function.  Godot's process arguments own the requested pixel
    size, while ``fullscreen=false`` is required for them to affect the render
    surface captured by PrintWindow.
    """
    import json

    root = _brotato_active_save_dir()
    path = root / "settings.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"cannot read Brotato settings: {path}") from exc
    prefs = data.get("settings")
    if not isinstance(prefs, dict):
        raise RuntimeError(f"unsupported Brotato settings format: {path}")

    wanted = {
        "fullscreen": not mode.lower().startswith("window"),
        "language": "en",
        "movement_with_gamepad": True,
        "mouse_only": False,
    }
    changed = []
    for key, value in wanted.items():
        if prefs.get(key) != value:
            prefs[key] = value
            changed.append(key)
    if not changed:
        return f"already {mode} {size[0]}x{size[1]}, gamepad on, EN"

    backup = path.with_suffix(path.suffix + ".orig")
    if not backup.exists():
        try:
            shutil.copy2(path, backup)
        except OSError as exc:
            raise RuntimeError(
                f"cannot back up Brotato settings: {path}") from exc
    try:
        path.write_text(json.dumps(data, separators=(",", ":")),
                        encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot write Brotato settings: {path}") from exc
    return (f"{mode} {size[0]}x{size[1]}, gamepad on, EN; changed "
            + ", ".join(changed))


# --------------------------------------------------------------------------
# Slay the Spire
#
# Unlike Balatro, every file here is plain text next to the executable, so
# there is no container format to decode. What makes StS need a forcer at all
# is `Controller Enabled` in STSGameplaySettings: the game does not accept pad
# and keyboard/mouse interchangeably, it commits to ONE input mode and
# PERSISTS that choice. Boot it once with no controller present and the flag
# goes false, after which a virtual pad is ignored no matter when it appears.
# Two other values matter for reproducibility: LANGUAGE must be ENG because
# the scorer is English OCR, and info.displayconfig carries the resolution the
# OCR rects were measured at.
# --------------------------------------------------------------------------

def sts_install_dir(exe_path: Optional[str]) -> Optional[Path]:
    """StS keeps preferences/ and saves/ beside the executable, not in APPDATA."""
    if not exe_path:
        return None
    d = Path(exe_path).parent
    return d if d.is_dir() else None


def _patch_json_prefs(path: Path, wanted: dict) -> list[str]:
    """Set keys in one of StS's flat JSON preference files.

    Values are quoted strings even for booleans ("true"), which is the game's
    own spelling — writing a real JSON boolean makes it fall back to defaults.
    Returns the list of keys actually changed.
    """
    import json
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001 — never block a run over a pref file
        print(f"[settings] cannot read {path}: {e}")
        return []
    if not isinstance(data, dict):
        return []
    changed = []
    for key, want in wanted.items():
        if data.get(key) != want:
            data[key] = want
            changed.append(f"{key}={want}")
    if not changed:
        return []
    backup = path.with_suffix(path.suffix + ".orig")
    if not backup.exists():
        try:
            shutil.copy2(path, backup)
        except Exception:  # noqa: BLE001
            pass
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[settings] cannot write {path}: {e}")
        return []
    return changed


def force_sts_settings(size: Tuple[int, int],
                       mode: str = "Windowed",
                       exe_path: Optional[str] = None) -> Optional[str]:
    """Make StS start windowed at `size`, pad-enabled, in English.

    `info.displayconfig` is a bare line-per-value file — width, height, refresh
    rate, then three flags. Only the first two and the fullscreen flag are
    rewritten; the rest is left exactly as the game wrote it, since guessing at
    a flag whose meaning has not been verified is how you silently change
    something else. Missing or short files are left alone rather than
    fabricated: the game regenerates them, and a hand-built one that is wrong
    is worse than none.
    """
    root = sts_install_dir(exe_path)
    if root is None:
        return None
    notes = []

    prefs = root / "preferences"
    gameplay = prefs / "STSGameplaySettings"
    if gameplay.exists():
        # Controller Enabled is the flag behind "the input mode it booted with
        # is the only one that works"; LANGUAGE feeds the English OCR scorer.
        changed = _patch_json_prefs(gameplay, {
            "Controller Enabled": "true",
            "LANGUAGE": "ENG",
        })
        notes.extend(changed)

    dc = root / "info.displayconfig"
    if dc.exists():
        try:
            lines = dc.read_text(encoding="utf-8",
                                 errors="replace").splitlines()
        except Exception as e:  # noqa: BLE001
            print(f"[settings] cannot read {dc}: {e}")
            lines = []
        if len(lines) >= 4:
            before = list(lines)
            lines[0] = str(int(size[0]))
            lines[1] = str(int(size[1]))
            if mode.lower().startswith("window"):
                lines[3] = "false"          # fullscreen off
            if lines != before:
                backup = dc.with_suffix(".displayconfig.orig")
                if not backup.exists():
                    try:
                        shutil.copy2(dc, backup)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    dc.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    notes.append(f"{mode} {size[0]}x{size[1]}")
                except Exception as e:  # noqa: BLE001
                    print(f"[settings] cannot write {dc}: {e}")

    if not notes:
        return f"already {mode} {size[0]}x{size[1]}, pad on, ENG"
    return ", ".join(notes)


def clear_sts_saved_run(exe_path: Optional[str] = None) -> Optional[str]:
    """Remove any in-progress run so the cold path is always the same.

    With a save present the title screen offers to CONTINUE, which is a
    different menu than a fresh Ironclad start — the recorded menu_skip would
    replay into the wrong screens, and the episode would begin from a state no
    other machine shares (the save found on this box was already 3 floors in).
    Deleting it makes the recorded path mean "new run" every time.
    """
    root = sts_install_dir(exe_path)
    if root is None:
        return None
    saves = root / "saves"
    if not saves.is_dir():
        return None
    removed = []
    # *.autosave plus the game's own .backUp copies — leaving the backup
    # behind lets the game resurrect the run we just deleted.
    for p in sorted(saves.glob("*.autosave*")):
        try:
            p.unlink()
            removed.append(p.name)
        except OSError as e:
            print(f"[settings] cannot remove {p}: {e}")
    return ("cleared " + ", ".join(removed)) if removed else None


def force_aimlab_settings(size: Tuple[int, int], mode: str = "Windowed",
                          exe_path: Optional[str] = None) -> Optional[str]:
    """Prepare presentation for the guest and sole cached account profile."""
    import json

    if mode != "Windowed":
        raise ValueError("Aimlab benchmark requires Windowed mode")
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("cannot prepare Aimlab settings: LOCALAPPDATA is unavailable")
    root = Path(local).parent / "LocalLow" / "Statespace" / "aimlab_tb"
    account_paths = sorted((root / "Users").glob(
        "*/WindowsPlayer_PlayerSettingsData.json"))
    if len(account_paths) > 1:
        raise RuntimeError(
            "cannot prepare Aimlab settings: multiple cached account profiles; "
            "active profile selection is unverified")
    paths = [path for path in [
        root / "UserDefault" / "WindowsPlayer_PlayerSettingsData.json",
        *account_paths,
    ] if path.is_file()]
    if not paths:
        # The normal guest onboarding creates the profile. Never manufacture
        # an account/profile or mark onboarding complete in a synthetic save.
        return None
    wanted = {"screenWidth": int(size[0]), "screenHeight": int(size[1]),
              "displayMode": 3}
    updates = []
    # Validate every selected document before modifying either profile.
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        graphics = data.get("graphicsSettings") if isinstance(data, dict) else None
        game = data.get("gameSettings") if isinstance(data, dict) else None
        if not isinstance(graphics, dict) or not isinstance(game, dict):
            raise ValueError(f"unsupported Aimlab presentation settings: {path}")
        if any(graphics.get(key) != value for key, value in wanted.items()) \
                or game.get("language") != "en" or game.get("localeID") != "en":
            graphics.update(wanted)
            game["language"] = "en"
            game["localeID"] = "en"
            updates.append((path, data))
    for path, data in updates:
        backup = path.with_suffix(path.suffix + ".orig")
        if not backup.exists():
            shutil.copy2(path, backup)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return f"Windowed {size[0]}x{size[1]}, EN"


def force_kingdom_rush_settings(size: Tuple[int, int], mode: str = "Windowed",
                                exe_path: Optional[str] = None) -> str:
    """Prepare only the observed presentation fields, preserving save slots."""
    if mode != "Windowed":
        raise ValueError("Kingdom Rush benchmark requires Windowed mode")
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("cannot prepare Kingdom Rush settings: APPDATA is unavailable")
    path = Path(appdata) / "kingdom_rush_alliance" / "settings.lua"
    from coding4game_gui.benchmark.windows.native_profile_io import needs_native_io, native_call
    if needs_native_io(path.parent):
        return native_call("kingdom_presentation", {"size": list(size), "mode": mode})
    text = path.read_text(encoding="utf-8") if path.exists() else (
        "local obj1 = {\n}\nreturn obj1\n")
    # The game writes this named table, not an executable settings script.
    # Refuse a different format instead of replacing an unknown local file.
    if not re.fullmatch(r"\s*local obj1 = \{.*\}\s*return obj1\s*", text, re.S):
        raise ValueError(f"unsupported Kingdom Rush settings table: {path}")
    wanted = {
        "fullscreen": "false", "width": str(int(size[0])),
        "height": str(int(size[1])), "locale": '"zh-Hans"',
        "highdpi": "false", "player_customized": "true",
    }
    before = text
    for key, value in wanted.items():
        pattern = rf'(?m)^(\s*\["{key}"\]\s*=\s*)[^;\n]+;'
        matches = list(re.finditer(pattern, text))
        if len(matches) > 1:
            raise ValueError(f"duplicate Kingdom Rush settings key: {key}")
        if matches:
            text = re.sub(pattern, lambda m: m[1] + value + ";", text)
        else:
            text = text.replace("local obj1 = {", (
                f'local obj1 = {{\n\t["{key}"] = {value};'), 1)
    if text != before or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = path.with_suffix(".lua.orig")
        if path.exists() and not backup.exists():
            shutil.copy2(path, backup)
        path.write_text(text, encoding="utf-8")
    return f"Windowed {size[0]}x{size[1]}, zh-Hans"


def force_palworld_settings(size: Tuple[int, int], mode: str = "Windowed",
                            exe_path: Optional[str] = None) -> str:
    """Set observed UE/Pal presentation fields without rewriting other settings."""
    if mode != "Windowed":
        raise ValueError("Palworld benchmark requires Windowed mode")
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("cannot prepare Palworld settings: LOCALAPPDATA is unavailable")
    path = Path(local) / "Pal/Saved/Config/Windows/GameUserSettings.ini"
    before = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    text = before
    sections = {
        "/Script/Engine.GameUserSettings": {
            "ResolutionSizeX": int(size[0]), "ResolutionSizeY": int(size[1]),
            "LastUserConfirmedResolutionSizeX": int(size[0]),
            "LastUserConfirmedResolutionSizeY": int(size[1]),
            "FullscreenMode": 2, "LastConfirmedFullscreenMode": 2,
            "PreferredFullscreenMode": 2,
        },
        "/Script/Pal.PalGameLocalSettings": {"SelectedWindowMode": 2},
    }
    for section, wanted in sections.items():
        section_pattern = rf"(?ms)^\[{re.escape(section)}\][^\n]*\n(.*?)(?=^\[|\Z)"
        matches = list(re.finditer(section_pattern, text))
        if len(matches) > 1:
            raise ValueError(f"duplicate Palworld presentation section: {section}")
        body = matches[0][1] if matches else ""
        for key, value in wanted.items():
            pattern = rf"(?m)^({re.escape(key)}\s*=\s*)[^\r\n]*"
            if len(re.findall(pattern, body)) > 1:
                raise ValueError(f"duplicate Palworld presentation key: {key}")
            if re.search(pattern, body):
                body = re.sub(pattern, lambda match: match[1] + str(value), body)
            else:
                body = body.rstrip("\n") + f"\n{key}={value}\n"
        if matches:
            start, end = matches[0].span(1)
            text = text[:start] + body + text[end:]
        else:
            text = text.rstrip("\n") + f"\n[{section}]\n{body}"
    if text != before:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = path.with_suffix(".ini.orig")
        if path.exists() and not backup.exists():
            shutil.copy2(path, backup)
        path.write_text(text, encoding="utf-8")
    return f"Windowed {size[0]}x{size[1]}"


def prepare_overcooked_preferences(size: Tuple[int, int], mode: str = 'Windowed') -> dict:
    """Set only the installed Unity Player's three observed display values."""
    if mode != 'Windowed' or len(size) != 2 or any(type(v) is not int or v <= 0 for v in size):
        raise ValueError('Overcooked requires a positive Windowed client size')
    import psutil
    if any((p.info.get('name') or '').casefold() == 'overcooked2.exe'
           for p in psutil.process_iter(['name'])):
        raise RuntimeError('close Overcooked before preparing presentation')
    import winreg
    key_path = r'Software\Team17\Overcooked2'
    wanted = {
        'Screenmanager Resolution Width_h182942802': size[0],
        'Screenmanager Resolution Height_h2627697771': size[1],
        'Screenmanager Is Fullscreen mode_h3981298716': 0,
    }
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                        winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
        before = {}
        for name in wanted:
            value, kind = winreg.QueryValueEx(key, name)
            if kind != winreg.REG_DWORD:
                raise ValueError('unexpected Overcooked display preference type')
            before[name] = value
        try:
            for name, value in wanted.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
            after = {name: winreg.QueryValueEx(key, name)[0] for name in wanted}
            if after != wanted:
                raise RuntimeError('Overcooked display preference verification failed')
        except BaseException:
            for name, value in before.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
            raise
    return dict(registry_key=key_path, before=before, after=after,
                description=f'Windowed {size[0]}x{size[1]}')


def force_overcooked_settings(size: Tuple[int, int], mode: str = 'Windowed',
                              exe_path: Optional[str] = None) -> str:
    """Prepare measured display preferences without Steam launch arguments."""
    from coding4game_gui.benchmark.windows.native_profile_io import needs_native_io, native_call
    profile = os.environ.get('USERPROFILE')
    if not profile:
        raise RuntimeError('Overcooked presentation requires USERPROFILE')
    directory = Path(profile) / 'AppData/LocalLow/Team17/Overcooked2'
    if needs_native_io(directory):
        result = native_call('overcooked_presentation', {'size': list(size), 'mode': mode})
    else:
        result = prepare_overcooked_preferences(size, mode)
    return result['description']


FORCERS = {
    'overcooked_2': force_overcooked_settings,
    "aimlab": force_aimlab_settings,
    "balatro": force_balatro_window,
    "brotato": force_brotato_settings,
    "slay_the_spire": force_sts_settings,
    "riders_republic": force_riders_republic_settings,
    "kingdom_rush_alliance": force_kingdom_rush_settings,
    "kingdom_rush_alliance_heart_of_the_forest": force_kingdom_rush_settings,
    "palworld_capture": force_palworld_settings,
}

# driver_game -> callable(exe_path) clearing any resumable run, so the cold
# start always begins at the same screen.
SAVE_CLEARERS = {
    "balatro": lambda _exe: clear_balatro_saved_run(),
    "brotato": clear_brotato_saved_run,
    "slay_the_spire": clear_sts_saved_run,
}


def clear_saved_run(driver_game: str,
                    exe_path: Optional[str] = None) -> Optional[str]:
    """Run a registered deterministic current-run cleanup, if any."""
    key = (driver_game or "").lower()
    clearer = SAVE_CLEARERS.get(key)
    if clearer is None:
        return None
    what = clearer(exe_path)
    if what:
        print(f"[settings] {driver_game}: {what}")
    return what


def apply(driver_game: str, size, mode: str = "Windowed",
          exe_path: Optional[str] = None) -> None:
    """Apply the declared geometry for `driver_game`, if we know how."""
    key = (driver_game or "").lower()
    # A platform launch targets its client, whose DPI settings are unrelated
    # to the game window and must remain under the operator's control.
    if exe_path and Path(exe_path).name.lower() != "steam.exe":
        what = force_dpi_unaware_off(exe_path)
        if what:
            print(f"[settings] {driver_game}: {what}")
    clear_saved_run(driver_game, exe_path)
    fn = FORCERS.get(key)
    if fn is None or not size:
        return
    what = fn((int(size[0]), int(size[1])), mode, exe_path)
    if what:
        print(f"[settings] {driver_game}: forced {what}")
