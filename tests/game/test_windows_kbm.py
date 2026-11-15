from __future__ import annotations

import pytest

from coding4game_gui.benchmark.windows.drivers.registry import list_platforms
from coding4game_gui.benchmark.windows.drivers.registry import resolve_platform
from coding4game_gui.benchmark.windows.drivers.windows_kbm.input import KeyboardMouseInputDriver
from coding4game_gui.benchmark.windows.prelaunch import make_pads


class FakeGamepad:
    def __init__(self):
        self.n_pads = 1
        self.states = []
        self.reset_indices = []
        self.wakeups = []
        self.closed = False

    def apply_state(self, idx, **state):
        self.states.append((idx, state))

    def reset_pad(self, idx):
        self.reset_indices.append(idx)

    def reset(self):
        self.reset_indices.append("all")

    def wakeup(self, duration=0.1):
        self.wakeups.append(duration)

    def close(self):
        self.closed = True


class FakeKeyboard:
    def __init__(self):
        self.states = []
        self.releases = 0
        self.closed = False

    def set_held(self, keys):
        self.states.append(list(keys))

    def release_all(self):
        self.releases += 1

    def close(self):
        self.closed = True


def test_kbm_platform_is_explicit_and_auto_default_is_unchanged(monkeypatch):
    assert "windows-kbm" in list_platforms()
    assert resolve_platform("windows-kbm") == "windows-kbm"
    monkeypatch.setattr("coding4game_gui.benchmark.windows.prelaunch.time.sleep", lambda _s: None)
    made = []
    pads = make_pads(
        1, platform_name="windows-kbm",
        pad_factory=lambda n: made.append(FakeGamepad()) or made[-1])
    assert pads is made[0]
    assert pads.wakeups == [0.1]


def test_keyboard_adapter_preserves_gamepad_state_and_routes_raw_keys():
    gamepad, keyboard = FakeGamepad(), FakeKeyboard()
    driver = KeyboardMouseInputDriver(gamepad, keyboard)
    driver.apply_state(
        0, buttons=["A", "KEY[SHIFT]", "KEY[W]"],
        left_stick=(0.25, -0.5), right_stick=(0.0, 0.0),
        left_trigger=0.0, right_trigger=0.75)
    idx, state = gamepad.states[-1]
    assert idx == 0
    assert state["buttons"] == ["A"]
    assert state["left_stick"] == (0.25, -0.5)
    assert state["right_trigger"] == 0.75
    assert keyboard.states[-1] == ["SHIFT", "W"]

    driver.reset_pad(0)
    assert keyboard.releases == 1
    assert gamepad.reset_indices == [0]


def test_keyboard_adapter_close_releases_keys_and_closes_both_adapters():
    gamepad, keyboard = FakeGamepad(), FakeKeyboard()
    driver = KeyboardMouseInputDriver(gamepad, keyboard)
    driver.close()
    assert keyboard.closed and gamepad.closed


def test_escape_is_delivered_and_released_by_normal_pulse_adapter():
    gamepad, keyboard = FakeGamepad(), FakeKeyboard()
    driver = KeyboardMouseInputDriver(gamepad, keyboard)
    driver.apply_state(0, buttons=["KEY[ESC]"])
    assert keyboard.states == [["ESC"]]
    assert gamepad.states[-1][1]["buttons"] == []
    driver.reset_pad(0)
    assert keyboard.releases == 1


def test_startup_query_packets_preserve_ascii_without_layout_conversion():
    from coding4game_gui.benchmark.windows.drivers.windows_kbm.input import type_startup_text

    received = []

    def send(count, events, size):
        assert count == 2
        assert size > 0
        received.append([(e.type, e.ki.wVk, e.ki.wScan, e.ki.dwFlags)
                         for e in events])
        return count

    query = "Gridshot 3x3"
    type_startup_text(query, interval=0, send_input=send)
    assert received == [[(1, 0, ord(c), 4), (1, 0, ord(c), 6)] for c in query]


def test_startup_query_partial_delivery_releases_then_fails_without_retry():
    from coding4game_gui.benchmark.windows.drivers.windows_kbm.input import type_startup_text

    counts = []

    def partial(count, events, size):
        counts.append(count)
        return 1

    with pytest.raises(OSError, match="1/2 events at index 0"):
        type_startup_text("Gridshot", interval=0, send_input=partial)
    assert counts == [2, 1]


def test_startup_query_rejects_unsupported_text_before_input():
    from coding4game_gui.benchmark.windows.drivers.windows_kbm.input import type_startup_text

    with pytest.raises(ValueError, match="ASCII"):
        type_startup_text("测试", send_input=lambda *_: pytest.fail("unexpected input"))


def test_startup_query_requires_interactive_session(monkeypatch):
    from coding4game_gui.benchmark.windows.drivers.windows_kbm import input as module

    monkeypatch.setattr(module, "session_ok", lambda: False)
    with pytest.raises(RuntimeError, match="interactive session"):
        module.type_startup_text("Gridshot 3x3")
