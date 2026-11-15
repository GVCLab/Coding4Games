"""Presentation preparation preserves repeated benchmark and gameplay fields."""

import pytest

from coding4game_gui.benchmark.windows import game_settings


def test_palworld_presentation_preserves_other_fields_and_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = tmp_path / "Pal/Saved/Config/Windows/GameUserSettings.ini"
    path.parent.mkdir(parents=True)
    original = (
        "[/Script/Engine.GameUserSettings]\nResolutionSizeX=1920\nResolutionSizeY=1080\n"
        "FullscreenMode=1\nLastGPUBenchmarkSteps=10\nLastGPUBenchmarkSteps=20\n"
        "[/Script/Pal.PalGameLocalSettings]\nSelectedWindowMode=1\n"
        "AudioSettings=(Master=0.5)\nOtherGameplaySetting=preserved\n")
    path.write_text(original)
    game_settings.apply("palworld_capture", (2560, 1440))
    changed = path.read_text()
    assert "ResolutionSizeX=2560" in changed and "ResolutionSizeY=1440" in changed
    assert "FullscreenMode=2" in changed and "SelectedWindowMode=2" in changed
    for line in ["LastGPUBenchmarkSteps=10", "LastGPUBenchmarkSteps=20",
                 "AudioSettings=(Master=0.5)", "OtherGameplaySetting=preserved"]:
        assert line in changed
    assert path.with_suffix(".ini.orig").read_text() == original
    game_settings.apply("palworld_capture", (2560, 1440))
    assert path.read_text() == changed
    assert path.with_suffix(".ini.orig").read_text() == original


@pytest.mark.parametrize("duplicate", ["ResolutionSizeX=10\nResolutionSizeX=20", "FullscreenMode=1\nFullscreenMode=2"])
def test_ambiguous_presentation_is_not_written(tmp_path, monkeypatch, duplicate):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = tmp_path / "Pal/Saved/Config/Windows/GameUserSettings.ini"
    path.parent.mkdir(parents=True)
    before = f"[/Script/Engine.GameUserSettings]\n{duplicate}\n"
    path.write_text(before)
    with pytest.raises(ValueError, match="duplicate Palworld"):
        game_settings.apply("palworld_capture", (2560, 1440))
    assert path.read_text() == before
