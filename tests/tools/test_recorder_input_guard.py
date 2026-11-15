"""Tests for the recorder's physical-input isolation safety net.

The isolation hooks swallow every physical key and click, so a wedged recorder
takes the operator's keyboard and mouse with it (observed symptom: hard reboot).
These tests pin the two things that make that state recoverable — the watchdog
trip condition and an idempotent unhook — plus the escape hint the operator
needs to read while input is blocked.

Run:  pytest tests/tools/test_recorder_input_guard.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.calibration import record_pad_to_yaml as rec


class FakeUser32:
    """Records the win32 calls _teardown is expected to make."""

    def __init__(self) -> None:
        self.unhooked: list[object] = []

    def UnhookWindowsHookEx(self, handle):  # noqa: N802
        self.unhooked.append(handle)
        return 1


def _guard() -> rec.PhysicalInputIsolation:
    return rec.PhysicalInputIsolation(lambda *_: None, lambda *_: None)


# ----------------- watchdog trip condition -----------------

def test_watchdog_never_trips_before_first_heartbeat():
    """A slow startup (game launch, vgamepad probe) must not look like a
    deadlock — the watchdog only arms once the Tk thread has beaten once."""
    assert not rec._watchdog_should_trip(
        0.0, 10_000.0, armed=False, timeout_s=3.0,
    )


def test_watchdog_holds_while_heartbeat_is_fresh():
    assert not rec._watchdog_should_trip(
        100.0, 102.0, armed=True, timeout_s=3.0,
    )


def test_watchdog_trips_exactly_at_the_timeout_boundary():
    assert rec._watchdog_should_trip(100.0, 103.0, armed=True, timeout_s=3.0)


def test_watchdog_disabled_by_nonpositive_timeout():
    assert not rec._watchdog_should_trip(
        100.0, 999.0, armed=True, timeout_s=0.0,
    )


def test_heartbeat_arms_the_guard_and_stamps_the_clock():
    guard = _guard()
    assert not guard._armed
    before = time.monotonic()
    guard.heartbeat()
    assert guard._armed
    assert guard._last_beat >= before


# ----------------- teardown -----------------

def test_teardown_is_idempotent():
    """The pump thread's finally and the watchdog can both reach _teardown;
    double-unhooking a stale HHOOK must not happen."""
    guard = _guard()
    guard._mouse_hook = "MOUSE"
    guard._keyboard_hook = "KBD"
    fake = FakeUser32()

    guard._teardown(fake)
    guard._teardown(fake)

    assert fake.unhooked == ["MOUSE", "KBD"]


# ----------------- watchdog thread end-to-end -----------------

def test_watchdog_thread_releases_hooks_when_the_ui_goes_silent(monkeypatch):
    """The whole point: a wedged Tk thread must not leave the hooks installed."""
    monkeypatch.setattr(rec, "WATCHDOG_TIMEOUT_S", 0.05)
    monkeypatch.setattr(rec, "WATCHDOG_POLL_S", 0.01)
    guard = _guard()
    guard._mouse_hook = "MOUSE"
    guard._keyboard_hook = "KBD"
    fake = FakeUser32()

    guard.heartbeat()          # arm, then never beat again (the wedge)
    guard._start_watchdog(fake)
    deadline = time.monotonic() + 3.0
    while guard._mouse_hook and time.monotonic() < deadline:
        time.sleep(0.01)
    guard._watchdog_stop.set()

    assert guard.watchdog_tripped
    assert fake.unhooked == ["MOUSE", "KBD"]


def test_watchdog_thread_stays_quiet_while_the_ui_beats(monkeypatch):
    """No false positives: a healthy recorder must keep its hooks."""
    monkeypatch.setattr(rec, "WATCHDOG_TIMEOUT_S", 0.2)
    monkeypatch.setattr(rec, "WATCHDOG_POLL_S", 0.01)
    guard = _guard()
    guard._mouse_hook = "MOUSE"
    guard._keyboard_hook = "KBD"
    fake = FakeUser32()

    guard._start_watchdog(fake)
    end = time.monotonic() + 0.6
    while time.monotonic() < end:
        guard.heartbeat()
        time.sleep(0.02)
    guard._watchdog_stop.set()

    assert not guard.watchdog_tripped
    assert fake.unhooked == []


# ----------------- operator escape hatch -----------------

def test_escape_banner_names_every_surviving_chord():
    for chord in ("Ctrl+Alt+S", "Ctrl+Alt+Q", "Ctrl+Alt+C", "Ctrl+Alt+Del"):
        assert chord in rec.ESCAPE_BANNER_TEXT


# ----------------- one cursor, not two -----------------
#
# Swallowing WM_MOUSEMOVE froze the OS cursor while ShowCursor failed to hide
# it, forcing the recorder to paint a second one. Movement must pass through.

def test_movement_reaches_the_recorder_and_the_system_cursor():
    assert rec._should_deliver_to_recorder(rec.WM_MOUSEMOVE, flags=0)
    assert not rec._should_swallow_physical_mouse(rec.WM_MOUSEMOVE, flags=0)


def test_clicks_are_still_swallowed_and_delivered():
    for message in (
        rec.WM_LBUTTONDOWN, rec.WM_LBUTTONUP,
        rec.WM_RBUTTONDOWN, rec.WM_RBUTTONUP,
    ):
        assert rec._should_swallow_physical_mouse(message, flags=0)
        assert rec._should_deliver_to_recorder(message, flags=0)


def test_wheel_is_swallowed():
    assert rec._should_swallow_physical_mouse(rec.WM_MOUSEWHEEL, flags=0)


def test_injected_events_are_left_alone_entirely():
    """Our own vgamepad/foreground plumbing must not feed itself."""
    for flags in (rec.LLMHF_INJECTED, rec.LLMHF_LOWER_IL_INJECTED):
        for message in (rec.WM_MOUSEMOVE, rec.WM_LBUTTONDOWN):
            assert not rec._should_swallow_physical_mouse(message, flags=flags)
            assert not rec._should_deliver_to_recorder(message, flags=flags)
