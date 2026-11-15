"""A click must never be aimed at a minimized window.

Windows reports a minimized window's rect as (-32000, -32000), so converting
a game coordinate against it aims the cursor off-screen; the click then
succeeds at the API level while doing nothing. Balatro runs fullscreen and
minimizes itself on focus loss, so this happened for real: all three recovery
clicks were aimed at (-31940, -31898), the reset "ran", start_check failed,
and the run fell through to full_restart — which relaunches and thereby
un-minimizes the game, hiding the cause.
"""
from __future__ import annotations

import ctypes
import sys
import types

import pytest


@pytest.fixture
def driver(monkeypatch):
    """A MouseDriver with win32gui faked, so this runs off Windows too."""
    state = {"iconic": True, "restored_by_focus": False, "moves": []}
    fake = types.SimpleNamespace(
        IsIconic=lambda h: state["iconic"],
        IsWindow=lambda h: True,
        IsWindowVisible=lambda h: True,
        GetWindowRect=lambda h: ((-32000, -32000, -31801, -31966)
                                 if state["iconic"] else (0, 0, 1536, 864)),
        GetForegroundWindow=lambda: 42,
        GetWindowText=lambda h: "Balatro",
        ShowWindow=lambda h, c: None,
        SetForegroundWindow=lambda h: None,
        EnumWindows=lambda cb, x: None,
    )
    monkeypatch.setitem(sys.modules, "win32gui", fake)
    monkeypatch.setitem(sys.modules, "win32con",
                        types.SimpleNamespace(SW_RESTORE=9))
    from coding4game_gui.benchmark.windows import mouse_driver as md

    monkeypatch.setattr(md.time, "sleep", lambda *_: None)
    drv = md.MouseDriver("Balatro", (1536, 864))
    drv._hwnd = 42
    monkeypatch.setattr(drv, "hwnd", lambda: 42)
    def _set_pos(x, y):
        state["moves"].append((x, y))
        state["cursor"] = (x, y)
        return 1

    state["cursor"] = (0, 0)
    monkeypatch.setattr(md._user32, "SetCursorPos", _set_pos, raising=False)
    monkeypatch.setattr(md.MouseDriver, "cursor_pos",
                        staticmethod(lambda: state["cursor"]))
    monkeypatch.setattr(md._user32, "mouse_event",
                        lambda *a: None, raising=False)
    return drv, state, fake


def test_click_is_refused_while_the_window_stays_minimized(driver):
    drv, state, _ = driver
    assert drv.click(766, 662) is False, \
        "a click against a minimized window must fail, not silently miss"
    assert not state["moves"], "the cursor must not be aimed off-screen"


def test_click_proceeds_once_the_window_is_restored(driver):
    drv, state, fake = driver

    def focus_restores():
        state["iconic"] = False
        return True

    drv.focus = focus_restores
    assert drv.click(766, 662) is True
    assert state["moves"], "a restored window should receive the click"
    x, y = state["moves"][-1]
    assert x > 0 and y > 0, f"click aimed off-screen: {(x, y)}"


def test_atomic_policy_click_has_only_short_move_and_button_delays(
        driver, monkeypatch):
    drv, state, _ = driver
    state["iconic"] = False
    from coding4game_gui.benchmark.windows import mouse_driver as md

    sleeps = []
    monkeypatch.setattr(md.time, "sleep", sleeps.append)

    assert drv.click(
        766, 662, move_settle=0.02, button_hold=0.03, settle=0.0)
    assert sleeps == [0.02, 0.03]


def test_navigation_click_retains_conservative_settle_delays(
        driver, monkeypatch):
    drv, state, _ = driver
    state["iconic"] = False
    from coding4game_gui.benchmark.windows import mouse_driver as md

    sleeps = []
    monkeypatch.setattr(md.time, "sleep", sleeps.append)

    assert drv.click(766, 662)
    assert sleeps == [0.12, 0.05, 0.45]


def test_right_click_uses_right_button_and_releases(driver, monkeypatch):
    drv, state, _ = driver
    state["iconic"] = False
    from coding4game_gui.benchmark.windows import mouse_driver as md

    events = []
    monkeypatch.setattr(md._user32, "mouse_event",
                        lambda *args: events.append(args), raising=False)

    assert drv.right_click(
        766, 662, move_settle=0, button_hold=0, settle=0)
    assert [event[0] for event in events] == [0x0008, 0x0010]


def test_middle_click_uses_middle_button_and_releases(driver, monkeypatch):
    drv, state, _ = driver
    state["iconic"] = False
    from coding4game_gui.benchmark.windows import mouse_driver as md

    events = []
    monkeypatch.setattr(md._user32, "mouse_event",
                        lambda *args: events.append(args), raising=False)

    assert drv.middle_click(
        766, 662, move_settle=0, button_hold=0, settle=0)
    assert [event[0] for event in events] == [0x0020, 0x0040]


def test_double_click_is_two_complete_left_clicks(driver, monkeypatch):
    drv, state, _ = driver
    state["iconic"] = False
    from coding4game_gui.benchmark.windows import mouse_driver as md

    events = []
    sleeps = []
    monkeypatch.setattr(md._user32, "mouse_event",
                        lambda *args: events.append(args), raising=False)
    monkeypatch.setattr(md.time, "sleep", sleeps.append)

    assert drv.double_click(
        766, 662, move_settle=0.02, button_hold=0.03,
        inter_click=0.05, settle=0)
    assert [event[0] for event in events] == [
        0x0002, 0x0004, 0x0002, 0x0004]
    assert sleeps == [0.02, 0.03, 0.05, 0.03]


def test_drag_moves_to_end_and_always_releases_left(driver, monkeypatch):
    drv, state, _ = driver
    state["iconic"] = False
    from coding4game_gui.benchmark.windows import mouse_driver as md

    events = []
    monkeypatch.setattr(md._user32, "mouse_event",
                        lambda *args: events.append(args), raising=False)

    assert drv.drag((100, 100), (900, 700), duration=0)
    assert [event[0] for event in events] == [0x0002, 0x0004]
    assert state["moves"][-1] == drv.to_screen(900, 700)

    events.clear()
    calls = iter([True, False])
    monkeypatch.setattr(drv, "move", lambda *_args: next(calls))
    assert not drv.drag((100, 100), (900, 700), duration=0)
    assert [event[0] for event in events] == [0x0002, 0x0004]


def test_scroll_targets_point_and_emits_both_wheels(driver, monkeypatch):
    drv, state, _ = driver
    state["iconic"] = False
    from coding4game_gui.benchmark.windows import mouse_driver as md

    events = []
    monkeypatch.setattr(md._user32, "mouse_event",
                        lambda *args: events.append(args), raising=False)

    assert drv.scroll(766, 662, dx=2, dy=-3)
    assert [event[0] for event in events] == [0x1000, 0x0800]
    assert events[0][3] == 2 * 120
    assert events[1][3] == ctypes.c_ulong(-3 * 120).value


def test_policy_move_leaves_cursor_at_hover_target(driver, monkeypatch):
    drv, state, _ = driver
    state["iconic"] = False
    from coding4game_gui.benchmark.windows import env as env_mod

    monkeypatch.setitem(env_mod._MOUSE_DRIVERS, "balatro.exe", drv)
    env_mod._move_game_points(
        "Balatro.exe", object(), [(766, 662)], (1536, 864))

    assert len(state["moves"]) == 1
    assert state["moves"][0][0] > 0 and state["moves"][0][1] > 0


def test_a_refused_SetCursorPos_is_reported_not_swallowed(driver, monkeypatch):
    """move() used to return True without checking SetCursorPos.

    That is how a dead input channel hid for an entire debugging session:
    SetCursorPos returned 0, the cursor never left (800, 450), every click
    reported success, menu_skip "ran", start_check failed and the run fell
    through to full_restart repeatedly — with nothing in the log naming the
    mouse.
    """
    drv, state, _ = driver
    state["iconic"] = False
    from coding4game_gui.benchmark.windows import mouse_driver as md

    monkeypatch.setattr(md._user32, "SetCursorPos", lambda x, y: 0,
                        raising=False)
    assert drv.move(766, 662) is False
    assert drv.click(766, 662) is False


def test_a_pinned_cursor_is_reported(driver, monkeypatch):
    """SetCursorPos can succeed while something else owns the pointer."""
    drv, state, _ = driver
    state["iconic"] = False
    from coding4game_gui.benchmark.windows import mouse_driver as md

    monkeypatch.setattr(md._user32, "SetCursorPos", lambda x, y: 1,
                        raising=False)
    monkeypatch.setattr(md.MouseDriver, "cursor_pos",
                        staticmethod(lambda: (800, 450)))
    assert drv.move(766, 662) is False, \
        "a cursor that never arrives must not count as a successful aim"
