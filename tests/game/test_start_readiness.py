"""Startup readiness must count live game time, not frozen OCR time."""
from types import SimpleNamespace

from PIL import Image
import pytest

from coding4game_gui.benchmark.windows.env import ResetController


def run_check(monkeypatch, matches, settle=1.0, timeout=100):
    clock = SimpleNamespace(wall=0.0, pulses=0, running=False)
    class Env:
        hard_pause = True
        def pause(self): clock.running = False
        def unpause(self): clock.running = True
        def render(self):
            assert self.hard_pause and not clock.running
            clock.wall += 2  # Slow OCR must not satisfy the live dwell.
            return Image.new('RGB', (8, 8))
    env = Env()
    controller = object.__new__(ResetController)
    controller.cfg = SimpleNamespace(process='test.exe')
    controller.tics_per_second = 10
    controller._start_check = {'pattern': 'READY', 'settle_seconds': settle}
    controller.start_ok = lambda frame: matches(clock.pulses)
    def sleep(seconds):
        assert clock.running and not env.hard_pause
        clock.pulses += 1
        clock.wall += seconds
    monkeypatch.setattr('time.monotonic', lambda: clock.wall)
    monkeypatch.setattr('time.sleep', sleep)
    result = controller.wait_start_ok(env, timeout=timeout)
    assert env.hard_pause and not clock.running
    return result, clock.pulses


def test_visible_start_requires_live_readiness_time(monkeypatch):
    assert run_check(monkeypatch, lambda n: True) == (True, 10)


def test_lost_start_condition_restarts_readiness_window(monkeypatch):
    assert run_check(monkeypatch, lambda n: n != 5) == (True, 16)


def test_zero_settle_preserves_first_valid_frame(monkeypatch):
    assert run_check(monkeypatch, lambda n: n >= 1, settle=0) == (True, 1)


def test_visible_but_unready_timeout_stays_paused(monkeypatch):
    assert run_check(monkeypatch, lambda n: True, timeout=0) == (False, 0)


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), True, '1'])
def test_invalid_readiness_configuration_rejected(monkeypatch, value):
    with pytest.raises(ValueError, match='settle_seconds'):
        run_check(monkeypatch, lambda n: True, settle=value)


def test_scenario_loader_rejects_invalid_dwell_before_launch(monkeypatch, tmp_path):
    import yaml
    from coding4game_gui.benchmark import spec
    data = yaml.safe_load((spec.WINDOWS_GAMES_DIR / 'balatro.yaml').read_text(encoding='utf-8'))
    data['startup']['start_check']['settle_seconds'] = -1
    (tmp_path / 'balatro.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    monkeypatch.setattr(spec, 'WINDOWS_GAMES_DIR', tmp_path)
    with pytest.raises(ValueError, match='settle_seconds'):
        spec.load_game_spec('balatro')


@pytest.mark.parametrize('ready', [False, True])
def test_scorer_start_check_uses_the_registered_game_condition(ready):
    controller = object.__new__(ResetController)
    controller._start_check = {'kind': 'scorer'}
    seen = []
    frame = object()
    controller._scorer = SimpleNamespace(
        check_start=lambda image: seen.append(image) or ready)
    assert controller.start_ok(frame) is ready
    assert seen == [frame]


def test_scorer_start_check_cannot_succeed_without_its_hook():
    controller = object.__new__(ResetController)
    controller._start_check = {'kind': 'scorer'}
    controller._scorer = SimpleNamespace()
    with pytest.raises(RuntimeError, match='requires check_start'):
        controller.start_ok(object())


def test_scorer_start_check_preserves_additional_start_constraints():
    controller = object.__new__(ResetController)
    controller._start_check = {'kind': 'scorer', 'pattern': 'READY'}
    controller._scorer = SimpleNamespace(
        check_start=lambda image: True, check_text=lambda *a, **kw: False)
    assert controller.start_ok(object()) is False


@pytest.mark.parametrize('check', [{'kind': 'unknown'}, []])
def test_scenario_loader_rejects_unsupported_start_check_before_launch(
        monkeypatch, tmp_path, check):
    import yaml
    from coding4game_gui.benchmark import spec
    data = yaml.safe_load((spec.WINDOWS_GAMES_DIR / 'balatro.yaml').read_text(encoding='utf-8'))
    data['startup']['start_check'] = check
    (tmp_path / 'balatro.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    monkeypatch.setattr(spec, 'WINDOWS_GAMES_DIR', tmp_path)
    with pytest.raises(ValueError, match='start_check'):
        spec.load_game_spec('balatro')
