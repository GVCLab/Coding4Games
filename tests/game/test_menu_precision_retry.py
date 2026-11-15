"""A missed door must not replay an entire long approach on a precision retry."""
from types import SimpleNamespace

from PIL import Image
import pytest

from coding4game_gui.benchmark.windows.games_config import _parse_pad_action_entry


def entry(**changes):
    raw = dict(buttons=['KEY[RIGHT]'], duration_seconds=2.3, expect='LCTL',
               retry_duration_seconds=.025)
    raw.update(changes)
    return _parse_pad_action_entry(raw, 'door', 'menu_skip[19]')


@pytest.mark.parametrize('retry,expected', [
    (.025, [2.3, .6, .025, .6]),
    (None, [2.3, .6, 2.3, .6]),
])
def test_approach_keeps_recorded_hold_and_uses_only_calibrated_retry(monkeypatch, retry, expected):
    from coding4game_gui.benchmark.windows import env as module

    step = entry(retry_duration_seconds=retry)
    controller = object.__new__(module.ResetController)
    sent, slept = [], []
    controller.nav_verifier = lambda _env: SimpleNamespace(
        check_text=lambda *_args: sum(bool(s['buttons']) for s in sent) >= 2)
    env = SimpleNamespace(render=lambda: Image.new('RGB', (32, 18)),
                          apply_state=lambda *_a, **kw: sent.append(kw))
    monkeypatch.setattr(module.time, 'sleep', slept.append)
    controller._press_until_screen_changes(env, step, retries=3)
    assert slept == expected
    assert [s['buttons'] for s in sent] == [['KEY[RIGHT]'], [], ['KEY[RIGHT]'], []]


@pytest.mark.parametrize('duration', [0, -.01, True, float('inf'), float('nan'), 2.4])
def test_invalid_precision_retry_is_rejected_before_input(duration):
    with pytest.raises(ValueError, match='retry_duration_seconds'):
        entry(retry_duration_seconds=duration)


@pytest.mark.parametrize('changes', [dict(expect=None), dict(buttons=[]), dict(click=[1, 1])])
def test_precision_retry_is_only_available_to_guarded_pad_or_key_navigation(changes):
    with pytest.raises(ValueError, match='retry_duration_seconds'):
        entry(**changes)


def test_precision_retry_is_navigation_metadata_not_a_gameplay_hold():
    step = entry()
    assert step.retry_duration_seconds == .025
    assert not hasattr(step.spec, 'retry_duration_seconds')
    assert step.spec.duration_seconds == 2.3


def test_legacy_keyboard_field_is_rejected_instead_of_becoming_a_wait():
    with pytest.raises(ValueError, match='unsupported keys field'):
        _parse_pad_action_entry(
            {'keys': ['SPACE'], 'duration_seconds': .1}, 'confirm', 'menu_skip[1]')


def test_recorded_keyboard_button_keeps_its_duration_and_pad():
    step = _parse_pad_action_entry(
        {'buttons': ['KEY[SPACE]'], 'duration_seconds': .015586900000926107,
         'pad': 1}, 'confirm', 'menu_skip[1]')
    assert step.spec.buttons == ['KEY[SPACE]']
    assert step.spec.duration_seconds == .015586900000926107
    assert step.pad == 0
