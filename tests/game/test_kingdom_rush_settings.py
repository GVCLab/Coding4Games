import pytest

from coding4game_gui.benchmark.windows import game_settings


def test_kingdom_settings_prepare_fresh_profile_and_preserve_slots(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    root = tmp_path / "kingdom_rush_alliance"
    root.mkdir()
    slot = root / "slot_2.lua"
    slot.write_bytes(b"original checkpoint")
    game_settings.apply("kingdom_rush_alliance", (2560, 1440))
    text = (root / "settings.lua").read_text()
    assert '["fullscreen"] = false;' in text
    assert '["width"] = 2560;' in text
    assert '["height"] = 1440;' in text
    assert '["locale"] = "zh-Hans";' in text
    assert slot.read_bytes() == b"original checkpoint"


def test_kingdom_settings_preserve_preferences_and_original_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    root = tmp_path / "kingdom_rush_alliance"
    root.mkdir()
    path = root / "settings.lua"
    original = ('local obj1 = {\n\t["width"] = 1920;\n'
                '\t["fullscreen"] = true;\n\t["music_volume"] = 0.4;\n}\nreturn obj1\n')
    path.write_text(original)
    game_settings.apply("kingdom_rush_alliance_heart_of_the_forest", (2560, 1440))
    prepared = path.read_text()
    assert '["music_volume"] = 0.4;' in prepared
    assert path.with_suffix(".lua.orig").read_text() == original
    game_settings.apply("kingdom_rush_alliance", (2560, 1440))
    assert path.read_text() == prepared
    assert path.with_suffix(".lua.orig").read_text() == original


@pytest.mark.parametrize("text", ["return unknown_settings()", (
    'local obj1 = {\n["width"] = 10;\n["width"] = 20;\n}\nreturn obj1')])
def test_kingdom_settings_refuse_unknown_or_ambiguous_format(tmp_path, monkeypatch, text):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = tmp_path / "kingdom_rush_alliance" / "settings.lua"
    path.parent.mkdir()
    path.write_text(text)
    with pytest.raises(ValueError):
        game_settings.apply("kingdom_rush_alliance", (2560, 1440))
    assert path.read_text() == text
