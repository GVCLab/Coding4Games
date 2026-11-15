from __future__ import annotations

import pytest

from coding4game_gui.benchmark.windows.raw_actions import WINDOWS_RAW_ACTION_PROTOCOL
from coding4game_gui.benchmark.windows.raw_actions import parse_windows_acts
from coding4game_gui.benchmark.windows.raw_actions import resolve_raw_action
from coding4game_gui.benchmark.windows.raw_actions import resolve_raw_state
from coding4game_gui.benchmark.windows.raw_actions import select_windows_gameplay
from coding4game_gui.benchmark.windows.raw_actions import windows_action_protocol
from coding4game_gui.benchmark.windows.raw_actions import windows_kbm_catalog_entries
from coding4game_gui.benchmark.windows.games_config import ActionSpec


def fixed(**items):
    return {name: (values if isinstance(values, ActionSpec)
                   else ActionSpec(name=name, description="", **values))
            for name, values in items.items()}


def test_parse_markdown_dynamic_actions_without_burst_limit():
    reply = "Here are actions:\n- ACT 1 ls[0.1, 0.1]\nACT 99 A,RT[.5]\nACT 2 NEUTRAL"
    assert parse_windows_acts(reply, None) == [
        (["LS[0.1, 0.1]"], 1),
        (["A", "RT[.5]"], 99),
        (["NEUTRAL"], 2),
    ]


def test_parse_keyboard_and_pointer_tokens_without_splitting_coordinates():
    assert parse_windows_acts(
        "ACT 4 KEY[SHIFT],KEY[A]\nACT 7 CLICK[250,750]\n"
        "ACT 5 MOVE[640,384]\nACT 6 RCLICK[1,2]\n"
        "ACT 2 RMOVE[-25,40]\n"
        "ACT 6 MCLICK[2,3]\n"
        "ACT 7 DOUBLE_CLICK[3,4]\nACT 8 DRAG[1,2,3,4]\n"
        "ACT 9 SCROLL[5,6,-2,3]", None) == [
        (["KEY[SHIFT]", "KEY[A]"], 4),
        (["CLICK[250,750]"], 7),
        (["MOVE[640,384]"], 5),
        (["RCLICK[1,2]"], 6),
        (["RMOVE[-25,40]"], 2),
        (["MCLICK[2,3]"], 6),
        (["DOUBLE_CLICK[3,4]"], 7),
        (["DRAG[1,2,3,4]"], 8),
        (["SCROLL[5,6,-2,3]"], 9),
    ]


def test_zero_tics_rejects_entire_reply():
    with pytest.raises(ValueError, match="positive integer"):
        parse_windows_acts("ACT 5 A\nACT 0 B", None)


def test_parse_rejects_entire_reply_when_act_count_exceeds_burst():
    reply = "ACT 1 A\nACT 2 B\nACT 3 X"
    with pytest.raises(
        ValueError,
        match=r"ACT burst limit exceeded: received 3 parseable ACT lines, cap is 2",
    ):
        parse_windows_acts(reply, 2)


def test_parse_rejects_prose_and_malformed_lines():
    reply = "Please ACT 3 A\nACT three A\nACT 3 A"
    assert parse_windows_acts(reply, None) == [(["A"], 3)]


def test_protocol_documents_parameterized_controls():
    assert "ACT 3 LS[0.1,0.1]" in WINDOWS_RAW_ACTION_PROTOCOL
    assert "LS[x,y]" in WINDOWS_RAW_ACTION_PROTOCOL
    assert "NEUTRAL" in WINDOWS_RAW_ACTION_PROTOCOL
    assert "one complete pulse" in WINDOWS_RAW_ACTION_PROTOCOL
    assert "all controls are released before the next ACT" in WINDOWS_RAW_ACTION_PROTOCOL
    assert "Repeated identical ACT lines" in WINDOWS_RAW_ACTION_PROTOCOL
    assert "CLICK[640,384]" not in windows_action_protocol("gamepad")
    kbm = windows_action_protocol("gamepad-kbm")
    assert "CLICK[640,384]" in kbm
    assert "1280x768" in kbm
    assert "0..1279" in kbm and "0..767" in kbm
    assert "0..1000" not in kbm
    assert "KEY[<key>]" in kbm
    assert "MOVE[640,384]" in kbm
    assert "RMOVE[dx,dy]" in kbm
    assert "CLICK_CURRENT" in kbm
    assert "[-1000,1000]" in kbm
    assert "RCLICK[x,y]" in kbm
    assert "MCLICK[x,y]" in kbm
    assert "DOUBLE_CLICK[x,y]" in kbm
    assert "DRAG[x1,y1,x2,y2]" in kbm
    assert "SCROLL[x,y,dx,dy]" in kbm
    assert "[-10,10]" in kbm
    assert "LS[x,y]" not in kbm
    assert "gamepad" not in kbm.lower()
    assert {entry["name"] for entry in windows_kbm_catalog_entries(
        "gamepad-kbm")} == {
            "KEY[<KEY>]", "MOVE[<X>,<Y>]", "RMOVE[<DX>,<DY>]",
            "CLICK[<X>,<Y>]", "CLICK_CURRENT",
            "RCLICK[<X>,<Y>]", "MCLICK[<X>,<Y>]",
            "DOUBLE_CLICK[<X>,<Y>]",
            "DRAG[<X1>,<Y1>,<X2>,<Y2>]",
            "SCROLL[<X>,<Y>,<DX>,<DY>]",
        }


@pytest.mark.parametrize("line", [
    "ACT 3 LS[0.1]",
    "- ACT 3 LS[2,]",
    "ACT 3 A and then wait",
    "ACT 3",
])
def test_act_shaped_malformed_payload_raises(line):
    with pytest.raises(ValueError, match=r"^invalid raw ACT syntax:"):
        parse_windows_acts(line, None)


def test_resolve_exact_dynamic_state():
    state = resolve_raw_state(["LS[0.1,0.1]", "RT[0.5]"], {})
    assert state == {
        "buttons": [], "left_stick": (0.1, 0.1),
        "right_stick": (0.0, 0.0), "left_trigger": 0.0,
        "right_trigger": 0.5,
    }


def test_resolve_combines_fixed_and_dynamic_controls():
    actions = fixed(A={"buttons": ["A"]}, LS={"buttons": ["LS"]})
    state = resolve_raw_state(["a", "LS", "LS[0.2,-0.7]", "LT[1]"], actions)
    assert state["buttons"] == ["A", "LS"]
    assert state["left_stick"] == (0.2, -0.7)
    assert state["left_trigger"] == 1.0


@pytest.mark.parametrize("tokens, text", [
    (["LS[2,0]"], "vector values"),
    (["RS[nan,0]"], "finite"),
    (["LT[1.1]"], "value must be in"),
    (["LS[0,0]", "LS[0.1,0]"], "duplicate dynamic channel"),
    (["BOGUS"], "unknown fixed token"),
    (["NEUTRAL", "A"], "NEUTRAL cannot be mixed"),
])
def test_resolve_rejects_invalid_tokens(tokens, text):
    with pytest.raises(ValueError, match=text):
        resolve_raw_state(tokens, fixed(A=ActionSpec(name="A", description="")))


def test_resolve_kbm_is_exclusive_and_maps_pointer_to_frame_space():
    actions = fixed(A={"buttons": ["A"]})
    keys = resolve_raw_action(
        ["KEY[SHIFT]", "KEY[W]"], actions,
        input_mode="gamepad-kbm", content_size=(1280, 768))
    assert keys.state["buttons"] == ["KEY[SHIFT]", "KEY[W]"]
    assert keys.clicks == ()

    click = resolve_raw_action(
        ["CLICK[1279,767]"], actions,
        input_mode="gamepad-kbm", content_size=(1536, 864))
    assert click.state["buttons"] == []
    assert click.clicks == ((1535.0, 863.0),)

    # Captured Balatro regression: the model saw a 1280x768 frame and emitted
    # the visible Select button centre directly as CLICK[460,260].
    select = resolve_raw_action(
        ["CLICK[460,260]"], actions,
        input_mode="gamepad-kbm", content_size=(1536, 864))
    assert select.clicks == ((552.0, 293.0),)

    move = resolve_raw_action(
        ["MOVE[460,260]"], actions,
        input_mode="gamepad-kbm", content_size=(1536, 864))
    assert move.moves == ((552.0, 293.0),)
    assert move.clicks == ()

    relative_move = resolve_raw_action(
        ["RMOVE[-25,40]"], actions,
        input_mode="gamepad-kbm", content_size=(1536, 864))
    assert relative_move.relative_moves == ((-25, 40),)
    assert relative_move.moves == ()

    right_click = resolve_raw_action(
        ["RCLICK[460,260]"], actions,
        input_mode="gamepad-kbm", content_size=(1536, 864))
    assert right_click.right_clicks == ((552.0, 293.0),)

    middle_click = resolve_raw_action(
        ["MCLICK[460,260]"], actions,
        input_mode="gamepad-kbm", content_size=(1536, 864))
    assert middle_click.middle_clicks == ((552.0, 293.0),)

    double_click = resolve_raw_action(
        ["DOUBLE_CLICK[460,260]"], actions,
        input_mode="gamepad-kbm", content_size=(1536, 864))
    assert double_click.double_clicks == ((552.0, 293.0),)

    drag = resolve_raw_action(
        ["DRAG[0,0,1279,767]"], actions,
        input_mode="gamepad-kbm", content_size=(1536, 864))
    assert drag.drags == (((0.0, 0.0), (1535.0, 863.0)),)

    scroll = resolve_raw_action(
        ["SCROLL[460,260,-2,3]"], actions,
        input_mode="gamepad-kbm", content_size=(1536, 864))
    assert scroll.scrolls == (((552.0, 293.0), -2, 3),)

    with pytest.raises(ValueError, match="keyboard/mouse mode does not allow"):
        resolve_raw_action(
            ["A"], actions, input_mode="gamepad-kbm",
            content_size=(1280, 768))
    with pytest.raises(ValueError, match="keyboard/mouse mode does not allow"):
        resolve_raw_action(
            ["LS[0,1]"], actions, input_mode="gamepad-kbm",
            content_size=(1280, 768))


@pytest.mark.parametrize("tokens, text", [
    (["CLICK[1280,0]"], r"X must be in \[0, 1279\]"),
    (["CLICK[0,768]"], r"Y must be in \[0, 767\]"),
    (["CLICK[01,0]"], "canonical integers"),
    (["CLICK[1.0,0]"], "must be integers"),
    (["KEY[ALT]"], "is not allowed"),
    (["KEY[DELETE]"], "is not allowed"),
    (["KEY[CTRL]", "KEY[ESC]"], "ESC must be the only control"),
    (["KEY[W]", "KEY[W]"], "duplicate keyboard key"),
    (["CLICK[1,1]", "KEY[W]"], "CLICK must be the only control"),
    (["CLICK[1,1]", "A"], "keyboard/mouse mode does not allow"),
    (["MOVE[1,1]", "CLICK[1,1]"], "pointer action must be the only control"),
    (["RMOVE[0,0]"], "cannot both be zero"),
    (["RMOVE[1001,0]"], r"must be in \[-1000, 1000\]"),
    (["RMOVE[01,0]"], "canonical integer"),
    (["RMOVE[1.0,0]"], "must be an integer"),
    (["RMOVE[1,0]", "KEY[W]"], "RMOVE must be the only control"),
    (["DRAG[0,0,1280,0]"], r"X must be in \[0, 1279\]"),
    (["SCROLL[1,1,0,0]"], "cannot both be zero"),
    (["SCROLL[1,1,11,0]"], r"must be in \[-10, 10\]"),
    (["DRAG[1,1,2,2]", "KEY[W]"], "DRAG must be the only control"),
])
def test_resolve_kbm_rejects_ambiguous_or_unsafe_controls(tokens, text):
    with pytest.raises(ValueError, match=text):
        resolve_raw_action(
            tokens, fixed(A={"buttons": ["A"]}),
            input_mode="gamepad-kbm", content_size=(1280, 768))


@pytest.mark.parametrize("name", ["ESC", "escape"])
def test_escape_alias_resolves_to_physical_scan_code(name):
    from coding4game_gui.benchmark.windows.drivers.windows_kbm.input import SCANCODES
    resolved = resolve_raw_action(
        [f"KEY[{name}]"], {}, input_mode="gamepad-kbm",
        content_size=(1280, 768))
    assert resolved.state["buttons"] == ["KEY[ESC]"]
    assert SCANCODES["ESC"] == 0x01


@pytest.mark.parametrize("tokens", [["CLICK[1,1]"], ["KEY[W]"]])
def test_default_gamepad_mode_rejects_keyboard_mouse_controls(tokens):
    with pytest.raises(ValueError, match="requires --windows-input"):
        resolve_raw_action(
            tokens, {}, input_mode="gamepad", content_size=(1280, 768))


def test_gameplay_guide_selection_is_exclusive_and_strips_markers():
    gameplay = """Shared facts.
<!-- C4G_WINDOWS_INPUT:gamepad:begin -->
Controller guide.
<!-- C4G_WINDOWS_INPUT:gamepad:end -->
<!-- C4G_WINDOWS_INPUT:gamepad-kbm:begin -->
Keyboard/mouse guide.
<!-- C4G_WINDOWS_INPUT:gamepad-kbm:end -->"""
    assert select_windows_gameplay(gameplay, "gamepad") == (
        "Shared facts.\nController guide.")
    assert select_windows_gameplay(gameplay, "gamepad-kbm") == (
        "Shared facts.\nKeyboard/mouse guide.")


def test_keyboard_mouse_gameplay_guide_fails_closed_when_unverified():
    with pytest.raises(ValueError, match="no verified gameplay guide"):
        select_windows_gameplay("Shared facts only.", "gamepad-kbm",
                                scenario="legacy")
