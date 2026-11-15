import pytest

from coding4game_gui.benchmark.windows.games_config import load_game_config
from coding4game_gui.benchmark.windows.games_config import parse_game_config


def test_auto_launch_yaml_without_settle_defaults_to_one_minute():
    cfg = parse_game_config({
        "name": "Example",
        "process": "example.exe",
        "auto_launch": {"launch_path": "C:/Games/Example/example.exe"},
        "actions": {"confirm": {"description": "Confirm", "buttons": ["A"]}},
    })

    assert cfg.auto_launch is not None
    assert cfg.auto_launch.settle_seconds == 60.0


def test_speedhack_is_disabled_by_default():
    cfg = parse_game_config({
        "name": "Example",
        "process": "example.exe",
        "actions": {"confirm": {"description": "Confirm", "buttons": ["A"]}},
    })

    assert cfg.use_speedhack is False


def test_speedhack_requires_a_yaml_boolean():
    for invalid in (1, 0, "true", "false", None):
        data = {
            "name": "Example",
            "process": "example.exe",
            "use_speedhack": invalid,
            "actions": {"confirm": {"description": "Confirm", "buttons": ["A"]}},
        }
        with pytest.raises(ValueError, match="use_speedhack.*boolean"):
            parse_game_config(data)


def test_balatro_opts_in_to_speedhack():
    assert load_game_config("balatro").use_speedhack is True
