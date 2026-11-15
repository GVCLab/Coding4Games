from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
CONFIGS = sorted((ROOT / "games").glob("**/*.yaml"))
ALLOWED_NAMES = {
    "neutral",
    "a", "b", "x", "y", "lb", "rb", "ls", "rs",
    "dpad_up", "dpad_down", "dpad_left", "dpad_right",
    "start", "back", "guide",
}
EXPECTED_BUTTON = {
    "a": "A", "b": "B", "x": "X", "y": "Y",
    "lb": "LB", "rb": "RB", "ls": "LS", "rs": "RS",
    "dpad_up": "DPAD_UP", "dpad_down": "DPAD_DOWN",
    "dpad_left": "DPAD_LEFT", "dpad_right": "DPAD_RIGHT",
    "start": "START", "back": "BACK", "guide": "GUIDE",
}
ANALOG_OR_MACRO_FIELDS = {
    "left_stick", "right_stick", "left_trigger", "right_trigger",
    "combine", "verb", "macro", "click", "click_text",
}


def test_every_game_uses_the_continuous_raw_action_boundary():
    assert CONFIGS
    for path in CONFIGS:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        actions = data.get("actions")
        assert isinstance(actions, dict) and actions, path
        assert "neutral" in actions, path
        assert set(actions) <= ALLOWED_NAMES, (path, set(actions) - ALLOWED_NAMES)

        neutral = actions["neutral"]
        assert isinstance(neutral, dict)
        assert not neutral.get("buttons"), (path, "neutral", neutral)
        for name, spec in actions.items():
            assert isinstance(spec, dict), (path, name)
            assert not ANALOG_OR_MACRO_FIELDS.intersection(spec), (path, name, spec)
            assert "duration_seconds" not in spec, (path, name, spec)
            assert "duration_tics" not in spec, (path, name, spec)
            if name == "neutral":
                assert set(spec) <= {"description"}, (path, name, spec)
                continue
            assert set(spec) <= {"description", "buttons"}, (
                path, name, spec
            )
            buttons = spec.get("buttons")
            assert isinstance(buttons, list) and len(buttons) == 1, (path, name, spec)
            assert buttons[0] == EXPECTED_BUTTON[name], (path, name, buttons)
