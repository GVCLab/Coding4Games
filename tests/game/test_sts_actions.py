"""Slay the Spire exposes raw Xbox-labelled controls to benchmark policies."""
from __future__ import annotations

from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.games_config import load_game_config


EXPECTED_ACTIONS = {
    "neutral",
    "dpad_up", "dpad_down", "dpad_left", "dpad_right",
    "a", "b", "x", "y",
    "lb", "rb", "ls", "rs",
    "start", "back",
    "guide",
}


def test_sts_policy_action_space_uses_raw_xbox_labels():
    cfg = load_game_config("slay_the_spire")
    assert set(cfg.actions) == EXPECTED_ACTIONS

    catalog = {item["name"] for item in
               load_game_spec("slay_the_spire")["action_catalog"]}
    assert catalog == {name.upper() for name in EXPECTED_ACTIONS}
    assert {"A", "B", "X", "Y", "LB", "RB", "LS", "RS"} <= catalog
    assert not {"SOUTH", "EAST", "WEST", "NORTH"}.intersection(catalog)


def test_sts_xbox_labels_resolve_to_canonical_xinput_state():
    actions = load_game_config("slay_the_spire").actions
    assert actions["a"].buttons == ["SOUTH"]
    assert actions["b"].buttons == ["EAST"]
    assert actions["x"].buttons == ["WEST"]
    assert actions["y"].buttons == ["NORTH"]
    assert actions["lb"].buttons == ["LEFT_SHOULDER"]
    assert actions["rb"].buttons == ["RIGHT_SHOULDER"]
    assert actions["ls"].buttons == ["LEFT_THUMB"]
    assert actions["rs"].buttons == ["RIGHT_THUMB"]
    assert "lt" not in actions and "rt" not in actions
