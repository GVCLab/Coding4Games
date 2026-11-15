"""Slay the Spire commits to ONE input mode at boot and persists it.

`preferences/STSGameplaySettings` carries `Controller Enabled`. StS does not
accept pad and keyboard/mouse interchangeably — it decides from the devices
present at startup and writes that decision to disk, so a virtual pad created
after launch is ignored for that run AND inherited as false by the next one.
That is the whole reason this game needs a settings forcer plus the
before-launch pad creation in game/env.py.

Everything here runs against a temporary install tree; the real installation
is never touched.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import zipfile

import yaml

from coding4game_gui.benchmark.windows import save_state
from coding4game_gui.benchmark.windows import game_settings
from coding4game_gui.benchmark.windows.games_config import load_game_config

REPO = Path(__file__).resolve().parents[2]
SCENARIO = (REPO / "coding4game_gui" / "benchmark" / "scenarios"
            / "slay_the_spire.yaml")
GOLDEN = REPO / "snapshots" / "slay_the_spire_golden.zip"

# Verbatim shape of the files this install wrote, trimmed to the keys that
# matter. Booleans are QUOTED strings — that is the game's own spelling, and
# writing real JSON booleans makes it fall back to defaults.
GAMEPLAY = {
    "Particle Effects": "false",
    "Fast Mode": "false",
    "Controller Enabled": "false",
    "Touchscreen Enabled": "false",
    "LANGUAGE": "ZHS",
}
# width, height, refresh, then three flags whose meaning past the first is
# not established — so the forcer must leave them alone.
DISPLAYCONFIG = "1280\n960\n60\nfalse\nfalse\ntrue\n"


def _install(tmp_path: Path, *, gameplay=None, displayconfig=None,
             saves=()) -> Path:
    root = tmp_path / "SlayTheSpire"
    (root / "preferences").mkdir(parents=True)
    (root / "saves").mkdir(parents=True)
    exe = root / "SlayTheSpire.exe"
    exe.write_bytes(b"MZ")
    if gameplay is not None:
        (root / "preferences" / "STSGameplaySettings").write_text(
            json.dumps(gameplay, indent=2), encoding="utf-8")
    if displayconfig is not None:
        (root / "info.displayconfig").write_text(displayconfig,
                                                 encoding="utf-8")
    for name in saves:
        (root / "saves" / name).write_bytes(b"mid-run")
    return exe


def _gameplay(exe: Path) -> dict:
    return json.loads(
        (exe.parent / "preferences" / "STSGameplaySettings")
        .read_text(encoding="utf-8"))


def test_controller_is_enabled_so_a_virtual_pad_is_seen_at_all(tmp_path):
    """The flag behind "whatever it booted with is the only thing that works"."""
    exe = _install(tmp_path, gameplay=GAMEPLAY, displayconfig=DISPLAYCONFIG)
    game_settings.force_sts_settings((1280, 960), "Windowed", str(exe))
    assert _gameplay(exe)["Controller Enabled"] == "true"


def test_language_is_forced_to_english_for_the_ocr_scorer(tmp_path):
    """The scorer is English OCR; this install had been left on ZHS."""
    exe = _install(tmp_path, gameplay=GAMEPLAY, displayconfig=DISPLAYCONFIG)
    game_settings.force_sts_settings((1280, 960), "Windowed", str(exe))
    assert _gameplay(exe)["LANGUAGE"] == "ENG"


def test_booleans_stay_quoted_strings(tmp_path):
    """A real JSON boolean makes the game discard the file and use defaults."""
    exe = _install(tmp_path, gameplay=GAMEPLAY, displayconfig=DISPLAYCONFIG)
    game_settings.force_sts_settings((1280, 960), "Windowed", str(exe))
    raw = (exe.parent / "preferences" / "STSGameplaySettings").read_text(
        encoding="utf-8")
    assert '"Controller Enabled": "true"' in raw
    assert "true," not in raw.replace('"true",', "")


def test_unrelated_preferences_are_left_alone(tmp_path):
    exe = _install(tmp_path, gameplay=GAMEPLAY, displayconfig=DISPLAYCONFIG)
    game_settings.force_sts_settings((1280, 960), "Windowed", str(exe))
    after = _gameplay(exe)
    assert after["Particle Effects"] == "false"
    assert after["Touchscreen Enabled"] == "false"
    assert after["Fast Mode"] == "false"


def test_original_preferences_are_recoverable(tmp_path):
    """This edits a file outside the repo; the user's own settings are not
    ours to lose."""
    exe = _install(tmp_path, gameplay=GAMEPLAY, displayconfig=DISPLAYCONFIG)
    game_settings.force_sts_settings((1280, 960), "Windowed", str(exe))
    backup = exe.parent / "preferences" / "STSGameplaySettings.orig"
    assert backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8"))[
        "Controller Enabled"] == "false"


def test_resolution_is_rewritten_and_trailing_flags_preserved(tmp_path):
    exe = _install(tmp_path, gameplay=GAMEPLAY,
                   displayconfig="1920\n1080\n60\ntrue\nfalse\ntrue\n")
    game_settings.force_sts_settings((1280, 960), "Windowed", str(exe))
    lines = (exe.parent / "info.displayconfig").read_text(
        encoding="utf-8").splitlines()
    assert lines[0] == "1280"
    assert lines[1] == "960"
    assert lines[2] == "60", "refresh rate is not ours to change"
    assert lines[3] == "false", "windowed mode must clear the fullscreen flag"
    assert lines[4:] == ["false", "true"], \
        "flags whose meaning is unverified must be left exactly as found"


def test_a_short_displayconfig_is_left_alone_rather_than_fabricated(tmp_path):
    """Guessing at a file the game regenerates is worse than not writing it."""
    exe = _install(tmp_path, gameplay=GAMEPLAY, displayconfig="1280\n")
    game_settings.force_sts_settings((1280, 960), "Windowed", str(exe))
    assert (exe.parent / "info.displayconfig").read_text(
        encoding="utf-8") == "1280\n"


def test_a_saved_run_is_cleared_so_the_cold_path_is_always_the_same(tmp_path):
    """With a save present the title screen offers CONTINUE instead of a fresh
    Ironclad start, so the recorded menu_skip would replay into wrong screens
    and the episode would begin from a state no other machine shares."""
    exe = _install(tmp_path, gameplay=GAMEPLAY, displayconfig=DISPLAYCONFIG,
                   saves=("IRONCLAD.autosave", "IRONCLAD.autosave.backUp"))
    game_settings.apply("slay_the_spire", (1280, 960), "Windowed", str(exe))
    left = sorted(p.name for p in (exe.parent / "saves").glob("*.autosave*"))
    assert left == [], f"still resumable: {left}"


def test_the_backup_save_is_removed_too(tmp_path):
    """Leaving .backUp behind lets the game resurrect the run just deleted."""
    exe = _install(tmp_path, gameplay=GAMEPLAY, displayconfig=DISPLAYCONFIG,
                   saves=("IRONCLAD.autosave.backUp",))
    what = game_settings.clear_sts_saved_run(str(exe))
    assert what and "IRONCLAD.autosave.backUp" in what


def test_steam_autocloud_is_not_deleted(tmp_path):
    """Only the run state is ours to clear."""
    exe = _install(tmp_path, gameplay=GAMEPLAY, displayconfig=DISPLAYCONFIG,
                   saves=("IRONCLAD.autosave", "steam_autocloud.vdf"))
    game_settings.clear_sts_saved_run(str(exe))
    assert (exe.parent / "saves" / "steam_autocloud.vdf").exists()


def test_a_missing_install_never_raises(tmp_path):
    """A settings tweak must not be able to block a run."""
    assert game_settings.force_sts_settings((1280, 960), "Windowed",
                                            None) is None
    assert game_settings.clear_sts_saved_run(None) is None
    missing = tmp_path / "nope" / "SlayTheSpire.exe"
    assert game_settings.force_sts_settings((1280, 960), "Windowed",
                                            str(missing)) is None
    # No preferences/, no saves/ — must be a silent no-op, not an error.
    bare = tmp_path / "bare"
    bare.mkdir()
    game_settings.apply("slay_the_spire", (1280, 960), "Windowed",
                        str(bare / "SlayTheSpire.exe"))


def test_forcing_is_idempotent(tmp_path):
    exe = _install(tmp_path, gameplay=GAMEPLAY, displayconfig=DISPLAYCONFIG)
    game_settings.force_sts_settings((1280, 960), "Windowed", str(exe))
    again = game_settings.force_sts_settings((1280, 960), "Windowed", str(exe))
    assert again.startswith("already ")


def test_scenario_geometry_matches_what_the_forcer_writes():
    """The declared size is authoritative in three places — the YAML, the
    preference file the forcer writes, and the live client area
    enforce_live_window forces. A mismatch puts every OCR rect somewhere the
    text is not.
    """
    spec = yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))
    scorer = spec["scorer"]
    setup = scorer["window_setup"]
    assert setup["mode"] == "Windowed", \
        "fullscreen geometry cannot be reproduced across machines"
    assert list(setup["size"]) == list(scorer["calibrated_client_size"]), \
        "rects were measured at a size the game is not forced to"
    assert list(scorer["ocr"]["input_size"]) == list(setup["size"])
    w, h = setup["size"]
    assert w <= 1920 and h + 60 <= 1080, \
        f"{w}x{h} will not fit a 1080p screen"


def test_golden_checkpoint_replaces_the_complete_preferences_directory(
        tmp_path):
    """Restoring the preferences tree removes stale local settings."""
    cfg = load_game_config("slay_the_spire", validate_files=False)
    assert cfg.save_state is not None
    assert cfg.save_state.restore_to.name == "SlayTheSpire"

    scenario = yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))
    assert scenario["startup"]["golden_checkpoint"] == "golden"

    fixture = tmp_path / "golden.zip"
    archived = {"preferences/STSSaveSlots", "preferences/STSGameplaySettings"}
    with zipfile.ZipFile(fixture, "w") as archive:
        for name in archived:
            archive.writestr(name, "{}")

    install = tmp_path / "SlayTheSpire"
    preferences = install / "preferences"
    preferences.mkdir(parents=True)
    stale = preferences / "machine-local-file"
    stale.write_text("must be removed", encoding="utf-8")

    restore_spec = replace(
        cfg.save_state,
        restore_to=install,
        golden=fixture,
    )
    report = save_state.restore(
        restore_spec, "C4GTestProcessThatDoesNotExist.exe")

    restored = {
        path.relative_to(install).as_posix()
        for path in install.rglob("*")
        if path.is_file()
    }
    assert restored == archived
    assert not stale.exists()
    assert report["wiped"] == ["preferences"]
