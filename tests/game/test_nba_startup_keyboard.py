"""Recorded keyboard menus must preserve the gameplay pad and its lifetime."""
from types import SimpleNamespace

import pytest

from coding4game_gui.benchmark.windows import env as module
from coding4game_gui.benchmark.windows.games_config import _parse_pad_action_entry


class Pad:
    n_pads = 1

    def __init__(self):
        self.closed = False
        self.states = []

    def apply_state(self, index, **state):
        assert not any(token.startswith('KEY[') for token in state['buttons'])
        self.states.append(state)

    def reset_pad(self, index):
        pass

    def close(self):
        self.closed = True


class Keyboard:
    def __init__(self):
        self.states = []
        self.closed = False

    def set_held(self, keys):
        self.states.append(keys)

    def release_all(self):
        self.states.append([])

    def close(self):
        self.release_all()
        self.closed = True


@pytest.mark.parametrize('fail', [False, True])
def test_public_menu_replay_routes_keyboard_then_restores_original_pad(monkeypatch, fail):
    from coding4game_gui.benchmark.windows.drivers.windows_kbm import input as keyboard_module
    from coding4game_gui.benchmark.windows.drivers.windows_kbm.input import KeyboardMouseInputDriver

    pad, keyboard = Pad(), Keyboard()
    env = SimpleNamespace(input=pad)
    step = _parse_pad_action_entry(
        dict(buttons=['KEY[2]'], duration_seconds=.109), 'confirm', 'menu_skip[0]')
    controller = object.__new__(module.ResetController)
    controller.cfg = SimpleNamespace(menu_skip=[step])
    monkeypatch.setattr(keyboard_module, 'ScanCodeKeyboard', lambda: keyboard)

    def replay(adapted, steps):
        assert isinstance(adapted.input, KeyboardMouseInputDriver)
        adapted.input.apply_state(0, **step.spec.gamepad_state())
        if fail:
            raise RuntimeError('recorded navigation failed')

    controller._replay_menu_skip = replay
    if fail:
        with pytest.raises(RuntimeError, match='recorded navigation failed'):
            controller.replay_menu_skip(env)
    else:
        controller.replay_menu_skip(env)
    assert ['2'] in keyboard.states
    assert keyboard.closed and keyboard.states[-1] == []
    assert env.input is pad and not pad.closed
    pad.apply_state(0, buttons=['X'], left_stick=(0., 0.),
                    right_stick=(0., 0.), left_trigger=0., right_trigger=0.)
    assert pad.states[-1]['buttons'] == ['X']


def test_existing_keyboard_driver_keeps_its_keyboard_after_startup(monkeypatch):
    from coding4game_gui.benchmark.windows.drivers.windows_kbm.input import KeyboardMouseInputDriver

    pad, keyboard = Pad(), Keyboard()
    input_driver = KeyboardMouseInputDriver(pad, keyboard)
    env = SimpleNamespace(input=input_driver)
    controller = object.__new__(module.ResetController)
    controller.cfg = SimpleNamespace(menu_skip=[_parse_pad_action_entry(
        dict(buttons=['KEY[2]']), 'confirm', 'menu_skip[0]')])
    controller._replay_menu_skip = lambda *_args: None
    controller.replay_menu_skip(env)
    assert env.input is input_driver
    assert not keyboard.closed and not pad.closed
