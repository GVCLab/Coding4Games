"""The scenario's geometry must be DECLARED and enforceable.

Fullscreen geometry is a property of the machine (monitor resolution / DPI
scaling), so calibrated pixels mean something different on every box — which
is how 1536x864-calibrated clicks silently stopped landing on a 1600x900
screen. A checked-in scenario has to pin its own geometry instead.
"""
from __future__ import annotations

import os
import zlib
from pathlib import Path

import yaml

from coding4game_gui.benchmark.windows import game_settings

REPO = Path(__file__).resolve().parents[2]
SCENARIO = (REPO / "coding4game_gui" / "benchmark" / "scenarios"
            / "balatro.yaml")

SETTINGS = ('return {["WINDOW"]={["vsync"]=1,["screenmode"]="Fullscreen",'
            '["DISPLAYS"]={[1]={["screen_res"]={["w"]=1536,["h"]=864},'
            '["DPI_scale"]=1}},["selected_display"]=1},'
            '["tutorial_complete"]=false,["crt"]=70,["bloom"]=1}')


def _deflate(text: str) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(text.encode("utf-8")) + c.flush()


def test_scenario_declares_a_windowed_size():
    spec = yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))
    setup = spec["scorer"]["window_setup"]
    assert setup["mode"] == "Windowed", \
        "fullscreen geometry cannot be reproduced across machines"
    w, h = setup["size"]
    # Must fit a 1080p display with room for the title bar — the floor for any
    # machine this bench runs on. It ALSO has to be large enough to read: the
    # earlier 1280x720 became a 1228x690 surface under DPI virtualisation, and
    # small labels stopped being recognised once that was upscaled to the
    # calibrated 1536x864. The scenario turns virtualisation off, so this size
    # is what the game actually renders.
    assert w <= 1920 and h + 60 <= 1080, \
        f"{w}x{h} will not fit a 1080p screen"
    assert w >= 1536, \
        f"{w}x{h} renders smaller than the calibration; text will be upscaled"


def test_forcing_rewrites_mode_and_resolution(tmp_path, monkeypatch):
    appdata = tmp_path / "AppData"
    (appdata / "Balatro").mkdir(parents=True)
    jkr = appdata / "Balatro" / "settings.jkr"
    jkr.write_bytes(_deflate(SETTINGS))
    monkeypatch.setenv("APPDATA", str(appdata))

    what = game_settings.force_balatro_window((1280, 720))
    assert what.startswith("Windowed 1280x720")
    out = zlib.decompress(jkr.read_bytes(), -15).decode()
    assert '["screenmode"]="Windowed"' in out
    assert '["w"]=1280,["h"]=720' in out
    # The developer's original preferences must be recoverable.
    assert (appdata / "Balatro" / "settings.jkr.orig").exists()


def test_forcing_is_a_noop_when_already_correct(tmp_path, monkeypatch):
    appdata = tmp_path / "AppData"
    (appdata / "Balatro").mkdir(parents=True)
    jkr = appdata / "Balatro" / "settings.jkr"
    jkr.write_bytes(_deflate(
        SETTINGS.replace("Fullscreen", "Windowed")
                .replace('["w"]=1536,["h"]=864', '["w"]=1280,["h"]=720')
                .replace('["tutorial_complete"]=false',
                         '["tutorial_complete"]=true')
                .replace('["crt"]=70', '["crt"]=0')
                .replace('["bloom"]=1', '["bloom"]=0')))
    monkeypatch.setenv("APPDATA", str(appdata))
    assert game_settings.force_balatro_window((1280, 720)).startswith(
        "already Windowed 1280x720")


def test_dpi_override_normalizes_yaml_path_before_registry(monkeypatch):
    seen = {}

    class FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeWinreg:
        HKEY_CURRENT_USER = object()
        KEY_READ = 1
        KEY_WRITE = 2
        REG_SZ = 1

        @staticmethod
        def CreateKeyEx(*_args):
            return FakeKey()

        @staticmethod
        def QueryValueEx(_key, name):
            seen["queried"] = name
            raise FileNotFoundError

        @staticmethod
        def SetValueEx(_key, name, _reserved, _kind, value):
            seen["written"] = (name, value)

    monkeypatch.setitem(__import__("sys").modules, "winreg", FakeWinreg)
    game_settings.force_dpi_unaware_off(
        "E:/SteamLibrary/steamapps/common/Balatro/Balatro.exe")

    expected = os.path.normpath(
        "E:/SteamLibrary/steamapps/common/Balatro/Balatro.exe")
    assert seen["queried"] == expected
    assert seen["written"][0] == expected
    assert "/" not in expected


def test_a_fresh_install_is_made_playable(tmp_path, monkeypatch):
    """The defaults a new player gets stop a run dead, and both were set by
    hand on the development machine — the sort of undeclared local state that
    makes a benchmark unreproducible. The tutorial locks input and forces the
    first shop's contents; the CRT filter puts scanlines over every frame the
    OCR has to read.
    """
    appdata = tmp_path / "AppData"
    (appdata / "Balatro").mkdir(parents=True)
    jkr = appdata / "Balatro" / "settings.jkr"
    jkr.write_bytes(_deflate(SETTINGS))
    monkeypatch.setenv("APPDATA", str(appdata))

    game_settings.force_balatro_window((1600, 900))
    out = zlib.decompress(jkr.read_bytes(), -15).decode()
    assert '["tutorial_complete"]=true' in out
    assert '["crt"]=0' in out
    assert '["bloom"]=0' in out


def test_a_saved_run_is_cleared_so_PLAY_always_means_new_run(tmp_path,
                                                             monkeypatch):
    """Balatro's title PLAY is context sensitive.

    With a saved run it RESUMES that run and never shows the New Run page, so
    the scripted cold start looked like a click that never landed while the
    game had gone straight into a half-finished game — carrying whatever
    jokers and money the previous run left behind. Navigation broke and the
    episode would have started from a state no other machine shares.
    """
    appdata = tmp_path / "AppData"
    for prof in ("1", "2"):
        (appdata / "Balatro" / prof).mkdir(parents=True)
        (appdata / "Balatro" / prof / "save.jkr").write_bytes(b"mid-run")
    (appdata / "Balatro" / "settings.jkr").write_bytes(_deflate(SETTINGS))
    monkeypatch.setenv("APPDATA", str(appdata))

    game_settings.apply("balatro", (1600, 900))
    for prof in ("1", "2"):
        assert not (appdata / "Balatro" / prof / "save.jkr").exists(), \
            f"profile {prof} still has a saved run"


def test_clearing_a_saved_run_is_harmless_when_there_is_none(tmp_path,
                                                             monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert game_settings.clear_balatro_saved_run() is None
    game_settings.apply("balatro", (1600, 900))     # must not raise
