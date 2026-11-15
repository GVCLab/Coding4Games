import json

import pytest

from coding4game_gui.benchmark.windows import game_settings


@pytest.mark.parametrize("executable,expected", [
    ("D:/App/STEAM/steam.exe", []),
    ("G:/SteamLibrary/steamapps/common/Aim Lab/AimLab_tb.exe",
     ["G:/SteamLibrary/steamapps/common/Aim Lab/AimLab_tb.exe"]),
])
def test_presentation_preparation_does_not_modify_steam_dpi(monkeypatch, executable, expected):
    changed = []
    monkeypatch.setattr(game_settings, "force_dpi_unaware_off", changed.append)
    monkeypatch.setattr(game_settings, "clear_saved_run", lambda *_a: None)
    monkeypatch.setitem(game_settings.FORCERS, "aimlab", lambda *_a: None)

    game_settings.apply("aimlab", (2048, 1152), exe_path=executable)

    assert changed == expected


def test_aimlab_settings_preserve_controls_and_original(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    path = (tmp_path / "LocalLow/Statespace/aimlab_tb/UserDefault" /
            "WindowsPlayer_PlayerSettingsData.json")
    path.parent.mkdir(parents=True)
    original = {"graphicsSettings": {"screenWidth": 2560, "screenHeight": 1600,
                                      "displayMode": 1, "graphicsQuality": 5},
                "gameSettings": {"language": "zh"},
                "controlSettings": {"sensitivity": 0.5}}
    path.write_text(json.dumps(original))
    game_settings.apply("aimlab", (2048, 1152))
    result = json.loads(path.read_text())
    assert result["graphicsSettings"] == {"screenWidth": 2048, "screenHeight": 1152,
                                          "displayMode": 3, "graphicsQuality": 5}
    assert result["gameSettings"]["language"] == "en"
    assert result["gameSettings"]["localeID"] == "en"
    assert result["controlSettings"] == original["controlSettings"]
    assert json.loads(path.with_suffix(".json.orig").read_text()) == original
    game_settings.apply("aimlab", (2048, 1152))
    assert json.loads(path.with_suffix(".json.orig").read_text()) == original


def test_aimlab_fresh_profile_is_created_only_by_normal_onboarding(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert game_settings.force_aimlab_settings((2048, 1152)) is None
    assert not (tmp_path / "LocalLow").exists()


def test_aimlab_unknown_profile_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    path = (tmp_path / "LocalLow/Statespace/aimlab_tb/UserDefault" /
            "WindowsPlayer_PlayerSettingsData.json")
    path.parent.mkdir(parents=True)
    path.write_text('{"unknown":true}')
    with pytest.raises(ValueError, match="unsupported"):
        game_settings.force_aimlab_settings((2048, 1152))
    assert path.read_text() == '{"unknown":true}'


def test_aimlab_logged_in_presentation_and_controls_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    root = tmp_path / "LocalLow/Statespace/aimlab_tb"
    original = {"graphicsSettings": {"screenWidth": 2560, "screenHeight": 1600,
                                      "displayMode": 1, "fullscreen": True},
                "gameSettings": {"language": "zh", "localeID": "zh-CN", "theme": "custom"},
                "controlSettings": {"sensitivity": 0.75}}
    for folder in ["UserDefault", "Users/account-one"]:
        path = root / folder / "WindowsPlayer_PlayerSettingsData.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(original))
    game_settings.apply("aimlab", (2048, 1152))
    for folder in ["UserDefault", "Users/account-one"]:
        path = root / folder / "WindowsPlayer_PlayerSettingsData.json"
        data = json.loads(path.read_text())
        assert data["graphicsSettings"] == {
            "screenWidth": 2048, "screenHeight": 1152,
            "displayMode": 3, "fullscreen": True}
        assert data["gameSettings"] == {"language": "en", "localeID": "en", "theme": "custom"}
        assert data["controlSettings"] == original["controlSettings"]
        assert json.loads(path.with_suffix(".json.orig").read_text()) == original


def test_aimlab_ambiguous_accounts_do_not_change_any_preferences(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    root = tmp_path / "LocalLow/Statespace/aimlab_tb"
    paths = [root / folder / "WindowsPlayer_PlayerSettingsData.json"
             for folder in ["UserDefault", "Users/one", "Users/two"]]
    original = '{"graphicsSettings":{},"gameSettings":{"language":"zh"}}'
    for path in paths:
        path.parent.mkdir(parents=True)
        path.write_text(original)
    with pytest.raises(RuntimeError, match="multiple cached account"):
        game_settings.force_aimlab_settings((2048, 1152))
    assert all(path.read_text() == original for path in paths)


def test_aimlab_invalid_account_does_not_partially_prepare_guest(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    root = tmp_path / "LocalLow/Statespace/aimlab_tb"
    guest = root / "UserDefault/WindowsPlayer_PlayerSettingsData.json"
    account = root / "Users/one/WindowsPlayer_PlayerSettingsData.json"
    guest.parent.mkdir(parents=True)
    account.parent.mkdir(parents=True)
    original = '{"graphicsSettings":{},"gameSettings":{"language":"zh"}}'
    guest.write_text(original)
    account.write_text('{"unknown":true}')
    with pytest.raises(ValueError, match="unsupported"):
        game_settings.force_aimlab_settings((2048, 1152))
    assert guest.read_text() == original
    assert not guest.with_suffix(".json.orig").exists()
