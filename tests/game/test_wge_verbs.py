"""Token table + combine semantics: union buttons, summed+clamped sticks."""
from coding4game_gui.benchmark.windows.games_config import ActionSpec
from coding4game_gui.benchmark.windows.verbs import build_action_table
from coding4game_gui.benchmark.windows.verbs import combine_states


def _a(name, **kw):
    return ActionSpec(name=name, description="", **kw)


def test_action_table_uppercases_keys():
    table = build_action_table({"move_up": _a("move_up"), "confirm": _a("confirm")})
    assert set(table) == {"MOVE_UP", "CONFIRM"}


def test_combine_unions_buttons_in_order():
    s = combine_states([_a("x", buttons=["A", "B"]), _a("y", buttons=["B", "X"])])
    assert s["buttons"] == ["A", "B", "X"]


def test_combine_sums_and_clamps_sticks():
    s = combine_states([_a("u", left_stick=(0.8, 0.0)),
                        _a("r", left_stick=(0.8, -1.0))])
    assert s["left_stick"] == (1.0, -1.0)


def test_combine_triggers_take_max():
    s = combine_states([_a("l", left_trigger=0.3), _a("m", left_trigger=0.9)])
    assert s["left_trigger"] == 0.9
