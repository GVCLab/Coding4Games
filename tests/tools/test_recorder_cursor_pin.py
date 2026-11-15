"""The recorder must keep the cursor where the operator put it.

A game with controller support ENABLED drives the system cursor from pad input.
Slay the Spire warps it onto whichever UI element the pad just focused, so one
click on the recorder's pad buttons teleports the pointer into the game and the
operator has to re-aim before every single step of a menu_skip recording.

This only started happening once the virtual pads were created BEFORE the game
launched (see tests/game/test_pad_before_launch.py): until then StS never enumerated
a controller, so it never entered controller mode, so it never drove the cursor.
The fix working is what exposed this.

`_release_cursor_clip` does not help — ClipCursor(NULL) lifts a containment
boundary, and this is a SetCursorPos. So the recorder anchors the cursor on each
pad press and pulls it back for a short window afterwards.

The recording artifact is unaffected: only pad state and wall-clock timing are
recorded, and the physical cursor is the operator's aiming device rather than
input to the artifact.

Run:  pytest tests/tools/test_recorder_cursor_pin.py
"""

from __future__ import annotations

import os
import queue
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.calibration import record_pad_to_yaml as rec


class FakeCursor:
    """A movable cursor, standing in for the Win32 GetCursorPos/SetCursorPos."""

    def __init__(self, pos=(500, 400)):
        self.pos = pos
        self.sets: list[tuple[int, int]] = []

    def get(self):
        return self.pos

    def set(self, x, y):
        self.pos = (x, y)
        self.sets.append((x, y))
        return True

    def warp_to(self, x, y):
        """The GAME moving the cursor, not us."""
        self.pos = (x, y)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _Pinner:
    """CaptureWindow's cursor-pin behaviour, without building a Tk window.

    Binding the real methods to a bare object exercises the shipped code: if
    the implementation changes, these tests change with it.
    """

    def __init__(self):
        self._cursor_anchor = None
        self._cursor_anchor_until = 0.0

    CURSOR_PIN_SECONDS = rec.CaptureWindow.CURSOR_PIN_SECONDS
    CURSOR_PIN_SLACK_PX = rec.CaptureWindow.CURSOR_PIN_SLACK_PX
    _anchor_cursor_here = rec.CaptureWindow._anchor_cursor_here
    _release_cursor_anchor = rec.CaptureWindow._release_cursor_anchor
    _pin_cursor_against_warp = rec.CaptureWindow._pin_cursor_against_warp


def _setup(monkeypatch, cursor=None, clock=None):
    cursor = cursor or FakeCursor()
    clock = clock or _Clock()
    monkeypatch.setattr(rec, "_get_cursor_pos", cursor.get)
    monkeypatch.setattr(rec, "_set_cursor_pos", cursor.set)
    monkeypatch.setattr(rec.time, "monotonic", clock)
    return _Pinner(), cursor, clock


# ----------------- the core behaviour -----------------

def test_the_cursor_is_defended_for_several_ticks_not_just_one(monkeypatch):
    """The warp is the game's REACTION to the input, so it lands a frame or two
    later. A single restore on the click itself would race it and lose."""
    pin, cursor, clock = _setup(monkeypatch)
    pin._anchor_cursor_here()

    restored = 0
    for _ in range(5):
        clock.advance(0.05)
        cursor.warp_to(200, 200)       # game keeps re-warping
        pin._pin_cursor_against_warp()
        if cursor.pos == (500, 400):
            restored += 1
    assert restored == 5, "the defence stopped early and the game won"


def test_the_defence_expires_so_the_operator_regains_the_mouse(monkeypatch):
    """Pinning forever would make the recorder unusable — the operator has to be
    able to move to the NEXT button."""
    pin, cursor, clock = _setup(monkeypatch)
    pin._anchor_cursor_here()

    clock.advance(rec.CaptureWindow.CURSOR_PIN_SECONDS + 0.01)
    cursor.warp_to(700, 700)           # operator deliberately moves away
    pin._pin_cursor_against_warp()

    assert cursor.pos == (700, 700), "the cursor is still being held hostage"
    assert cursor.sets == []
    assert pin._cursor_anchor is None, "the expired anchor was not cleared"


def test_small_movements_are_left_alone(monkeypatch):
    """The operator's own hand jitter, and sub-pixel rounding, must not trigger
    a fight between them and the restore."""
    pin, cursor, _clock = _setup(monkeypatch)
    pin._anchor_cursor_here()

    cursor.warp_to(500 + rec.CaptureWindow.CURSOR_PIN_SLACK_PX, 400)
    pin._pin_cursor_against_warp()
    assert cursor.sets == [], "a within-slack move was treated as a warp"


def test_nothing_happens_without_a_press(monkeypatch):
    """No pad press means no anchor, so the cursor is entirely the operator's."""
    pin, cursor, _clock = _setup(monkeypatch)
    cursor.warp_to(10, 10)
    pin._pin_cursor_against_warp()
    assert cursor.sets == []


def test_releasing_the_anchor_stops_the_defence(monkeypatch):
    """Used when a stick DRAG starts: that is deliberate cursor movement, and
    defending an anchor through it would fight the operator."""
    pin, cursor, _clock = _setup(monkeypatch)
    pin._anchor_cursor_here()
    pin._release_cursor_anchor()

    cursor.warp_to(300, 300)
    pin._pin_cursor_against_warp()
    assert cursor.sets == []


def test_a_later_press_moves_the_defended_position(monkeypatch):
    """Each press re-anchors, so the operator can work down a row of buttons."""
    pin, cursor, _clock = _setup(monkeypatch)
    pin._anchor_cursor_here()
    cursor.warp_to(640, 480)           # operator moves to the next button
    pin._anchor_cursor_here()

    cursor.warp_to(50, 50)             # game warps again
    pin._pin_cursor_against_warp()
    assert cursor.pos == (640, 480), "the stale anchor won over the new one"


def test_an_unreadable_cursor_position_is_not_fatal(monkeypatch):
    """Off-Windows, or a failing GetCursorPos: degrade, never raise. Losing the
    pin costs the operator some re-aiming; raising loses the recording."""
    pin, cursor, _clock = _setup(monkeypatch)
    pin._anchor_cursor_here()
    monkeypatch.setattr(rec, "_get_cursor_pos", lambda: None)

    pin._pin_cursor_against_warp()     # must not raise
    assert pin._cursor_anchor is None


def test_anchoring_is_a_no_op_when_the_position_is_unknown(monkeypatch):
    pin, _cursor, _clock = _setup(monkeypatch)
    monkeypatch.setattr(rec, "_get_cursor_pos", lambda: None)
    pin._anchor_cursor_here()
    assert pin._cursor_anchor is None


# ----------------- wiring -----------------

def test_the_recorder_tick_does_not_apply_a_global_cursor_clip_workaround(monkeypatch):
    """Game-specific focus fixes must not alter every recorder at 125 Hz."""
    releases = []
    pins = []
    scheduled = []
    tick = SimpleNamespace(
        heartbeat=lambda: None,
        _guard_mouse_q=queue.Queue(),
        _pin_cursor_against_warp=lambda: pins.append(True),
        _drain_guard_mouse=lambda: None,
        root=SimpleNamespace(
            after=lambda delay, callback: scheduled.append((delay, callback))),
    )
    monkeypatch.setattr(
        rec,
        "_release_cursor_clip",
        lambda: releases.append(True) or True,
    )

    rec.CaptureWindow._drain_guard_mouse(tick)

    assert releases == []
    assert pins == [True]
    assert scheduled == [(8, tick._drain_guard_mouse)]
