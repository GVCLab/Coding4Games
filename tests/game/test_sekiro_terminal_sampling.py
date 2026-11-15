"""Native-frame regressions for the explicit Sekiro action sampling protocol."""
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from PIL import Image
import pytest
import yaml

from coding4game_gui.benchmark.windows.env import WindowsGameEnv
from coding4game_gui.benchmark.windows.games_config import ActionSpec
from coding4game_gui.benchmark.windows.scorers.sekiro_boss import SekiroBossScorer
from coding4game_gui.benchmark.windows.terminal import validate_terminal_detection

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests/fixtures/sekiro_gyoubu'


def native(name):
    frame = np.zeros((1080, 1920, 3), np.uint8)
    crop = cv2.imread(str(FIXTURES / name))
    frame[300:740, 700:1200] = crop
    return frame


class Driver:
    def __init__(self, frames, clock):
        self.frames, self.clock = frames, clock
        self.stopped = False
        self.calls = 0
        self.closed = False
        self.input = self.gamepad = self

    def render(self):
        return Image.new('RGB', (1920, 1080))

    def pause(self):
        pass

    def reset_pad(self, index):
        pass

    def close(self):
        self.closed = True

    def step(self, **kwargs):
        self.calls += 1
        start = self.clock[0]
        result = self.render()
        for frame in self.frames:
            self.clock[0] += .25
            result = Image.fromarray(frame[:, :, ::-1])
            if kwargs.get('on_frame') and kwargs['on_frame'](result):
                self.stopped = True
                break
        elapsed = self.clock[0] - start
        self.last_step_timing = SimpleNamespace(
            requested_s=kwargs['duration_seconds'], live_window_s=elapsed,
            interrupted=self.stopped, timing_source='test_clock')
        return result


def make_env(monkeypatch, frames):
    spec = yaml.safe_load((ROOT / 'coding4game_gui/benchmark/scenarios/sekiro_gyoubu.yaml').read_text(encoding='utf-8'))
    clock = [100.0]
    monkeypatch.setattr('coding4game_gui.benchmark.windows.env.time.perf_counter', lambda: clock[0])
    driver = Driver(frames, clock)
    scorer = SekiroBossScorer(spec['scorer'], (1920, 1080))
    env = WindowsGameEnv('sekiro_gyoubu', driver_env=driver, scorer=scorer,
                         actions={'NEUTRAL': ActionSpec(name='neutral', description='', buttons=[])}, spec=spec)
    env.reset()
    return env, driver


def test_long_action_stops_on_two_native_samples_and_preserves_accounting(monkeypatch, tmp_path):
    frames = [native('combat_current_center.png'), native('death_current_bright_center.png'),
              native('death_current_dim_center.png'), native('combat_current_center.png')]
    env, driver = make_env(monkeypatch, frames)
    assert env.act(['NEUTRAL'], 1000) is None
    assert driver.stopped and env.done and env.end_reason == 'game_over'
    assert env.tic == 8  # 0.75 s, cumulative half-up; not the requested 1000.
    assert env.last_action_timing['interrupted']
    assert env.total_reward is None
    with pytest.raises(RuntimeError, match='episode has ended'):
        env.act(['NEUTRAL'], 1)
    assert driver.calls == 1
    env.finalize_score()
    assert env.total_reward == 0
    details = env.save_score_evidence(tmp_path)
    samples = details['terminal_confirmation']['frames']
    assert len(samples) == 2
    assert all(s['source'] == 'held_action_sample' and s['tic'] is None for s in samples)
    assert samples[1]['captured_monotonic'] > samples[0]['captured_monotonic']
    for sample in samples:
        assert cv2.imread(str(tmp_path / sample['image'])).shape == (1080, 1920, 3)
    env._controller = SimpleNamespace(shutdown=driver.close)
    env.close()
    assert driver.closed


def test_single_death_sample_followed_by_combat_is_not_confirmed(monkeypatch):
    frames = [native('death_current_bright_center.png'), native('combat_current_center.png')]
    env, driver = make_env(monkeypatch, frames)
    env.act(['NEUTRAL'], 1000)
    assert not driver.stopped and not env.done
    env.close()


def test_unrecognized_protocol_and_invalid_interval_fail_before_launch():
    for lifecycle in [dict(stepped_terminal_sampling='typo'),
                      dict(stepped_terminal_sampling='during_action_v1', terminal_scan_tics=0)]:
        with pytest.raises(ValueError):
            validate_terminal_detection({'lifecycle': lifecycle})


def test_transient_victory_confirmation_survives_deferred_score_reset(monkeypatch):
    victory = np.zeros((1080, 1920, 3), np.uint8)
    crop = cv2.imread(str(FIXTURES / "victory_center.png"))
    h, w = crop.shape[:2]
    victory[200:200+h, 650:650+w] = crop
    env, driver = make_env(monkeypatch, [victory, victory.copy(), np.zeros_like(victory)])
    assert env.act(['NEUTRAL'], 1000) is None
    assert driver.stopped and env.end_reason == 'terminal:victory'
    assert env.total_reward is None
    env.finalize_score()
    assert env.total_reward == 100
    env.close()
