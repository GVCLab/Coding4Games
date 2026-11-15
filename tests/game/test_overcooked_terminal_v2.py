"""V2 evidence and stopping regressions; synthetic input is not live acceptance."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from PIL import Image
import pytest

from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.env import WindowsGameEnv
from coding4game_gui.benchmark.windows.games_config import ActionSpec
from coding4game_gui.benchmark.windows.scorers.overcooked_2 import Overcooked2Scorer
from coding4game_gui.benchmark.windows.terminal_evidence import NativeEvidence, EvidenceFailure, Stability, TerminalMonitor

ROOT = Path(__file__).resolve().parents[2]


def readings(net=219):
    return json.loads((ROOT/f'tests/fixtures/overcooked_2/result_{net}.ocr.json').read_text(encoding='utf-8'))


def spec():
    return load_game_spec('overcooked_2_2_1', windows_input='gamepad-kbm')


def frame(tag=0):
    return np.full((1200, 1920, 3), tag, np.uint8)


def scorer(items, crop=None):
    return Overcooked2Scorer(spec()['scorer'], (1920, 1200),
        ocr=lambda f: deepcopy(items if f.shape[0] == 1200 else crop or []))


@pytest.mark.parametrize('net', [219, 246])
def test_human_recorded_positive_readings(net):
    s = scorer(readings(net))
    a = s.inspect_results(frame())
    assert a['complete'] and a['values']['net_score'] == net
    assert s.snapshot(frame())['success'] == (net >= 220)


def test_missing_zero_uses_only_measured_crop_and_preserves_mapping():
    items = readings()
    for item in items:
        if item['box'][0] > 1130:
            item['text'] = '0'
    items = [it for it in items if not (it['box'][0] > 1130 and it['box'][1] > 600)]
    a = scorer(items, [dict(text='0', conf=.99, box=[104, 12, 53, 65])]).inspect_results(frame())
    assert a['complete'] and a['values']['net_score'] == 0
    assert a['targeted_ocr']['mapped_items'][0]['box'] == [1234, 612, 53, 65]
    assert a['conditions']['net_score']['full_frame']['items'] == []
    assert not scorer(items).inspect_results(frame())['complete']


@pytest.mark.parametrize('case', ['conflict', 'missing', 'blank', 'map', 'negative'])
def test_unreadable_conflict_blank_and_map_not_scorable(case):
    items = readings()
    if case == 'conflict':
        items.append(dict(text='200', box=[1180, 610, 50, 40], conf=.99))
    elif case == 'missing':
        items = [it for it in items if it['text'] != '19']
    elif case == 'blank':
        items = [it for it in items if it['text'] in ('2-1', '继续')]
    elif case == 'map':
        items = [dict(text='2-1', box=[1500, 100, 100, 80]),
                 dict(text='460', box=[1550, 500, 100, 80])]
    else:
        for it in items:
            if it['text'] == '200': it['text'] = '0'
            if it['text'] == '19': it['text'] = '0'
            if it['text'] == '0': it['text'] = '-30' if it['box'][1] == 536 else '0'
            if it['text'] == '219': it['text'] = '-30'
    a = scorer(items).inspect_results(frame())
    assert a['complete'] == (case == 'negative')
    if case == 'negative': assert scorer(items).snapshot(frame())['score'] < 0
    if case == 'blank': assert a['candidate']
    if case == 'map': assert not a['candidate']


def test_independent_time_and_reset_then_latch():
    s = Stability()
    good = dict(complete=True, values={'net': 219})
    bad = dict(complete=False, values={})
    assert not s.update(good, 0, 0)['confirmed']
    assert not s.update(good, 0, .5)['independent']
    assert not s.update(good, 1, .6)['confirmed']  # two independent frames
    assert s.update(bad, 2, .7)['stable_count'] == 0
    assert not s.update(good, 3, 1)['confirmed']
    assert not s.update(dict(complete=True, values={'net': 246}), 4, 1.6)['confirmed']
    assert not s.update(good, 5, 1.7)['confirmed']
    assert not s.update(good, 6, 1.8)['confirmed']
    assert s.update(good, 7, 2.2)['confirmed']
    assert s.update(bad, 8, 2.3)['confirmed']


def test_registered_protocol_is_explicit_and_other_scenarios_keep_default():
    from coding4game_gui.benchmark.windows.terminal import validate_terminal_detection
    current = spec()
    assert current['lifecycle']['stepped_terminal_sampling'] == 'during_action_v2'
    assert current['scorer']['recognition_version'] == 'overcooked_results_v2'
    assert validate_terminal_detection(current)
    assert validate_terminal_detection({'lifecycle': {'terminal_detection': True}})


def test_candidate_changes_scan_cadence_and_incomplete_resets(tmp_path):
    class Reader:
        count = 0
        def inspect_results(self, f):
            self.count += 1
            return dict(candidate=self.count >= 2, complete=False, values={},
                        diagnostic='candidate_incomplete' if self.count >= 2 else 'no_candidate_observed')
    m = TerminalMonitor(Reader(), tmp_path, 'test')
    for i in range(9): m.capture(frame(), timestamp=i*.1, source='held_action_sample')
    m.close()
    checks = [json.loads(x) for x in (tmp_path/'checks.jsonl').read_text().splitlines()]
    assert [round(x['captured_monotonic'], 1) for x in checks] == [0, .3, .4, .5, .6, .7, .8]
    assert not any(x['decision']['confirmed'] for x in checks)


def test_ring_and_real_png_manifest(tmp_path):
    m = TerminalMonitor(scorer([]), tmp_path, 'test')
    for i in range(30):
        m.capture(frame(i), timestamp=i*.1, source='held_action_sample', action_id=1, batch_id='b')
    summary = m.close()
    rows = [json.loads(x) for x in (tmp_path/'frames.jsonl').read_text().splitlines()]
    assert 20 <= len(rows) <= 24
    assert summary['diagnostic'] == 'no_candidate_observed'
    assert all(r['tic'] is None and r['batch_id'] == 'b' for r in rows)
    assert cv2.imread(str(tmp_path/rows[-1]['path'])).shape == (1200, 1920, 3)


def test_writer_failure_and_overflow_are_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(cv2, 'imwrite', lambda *a, **k: False)
    writer = NativeEvidence(tmp_path/'fail', 'test')
    writer.add(dict(frame=frame(), frame_id=1), 'test')
    with pytest.raises(EvidenceFailure): writer.close()
    import threading
    gate = threading.Event()
    original = NativeEvidence._write
    monkeypatch.setattr(NativeEvidence, '_write', lambda self: (gate.wait(), original(self)))
    writer = NativeEvidence(tmp_path/'overflow', 'test', capacity=1)
    writer.add(dict(frame=frame(), frame_id=1), 'test')
    with pytest.raises(EvidenceFailure, match='overflow'):
        writer.add(dict(frame=frame(), frame_id=2), 'test')
    gate.set()
    with pytest.raises(EvidenceFailure): writer.close()


class Driver:
    def __init__(self, clock):
        self.clock = clock
        self.calls = self.releases = 0
        self.last_step_timing = None
    def render(self): return Image.new('RGB', (1920, 1200))
    def step(self, **kw):
        self.calls += 1
        start = self.clock[0]
        try:
            for i in range(15):
                self.clock[0] += .25
                if kw['on_frame'](self.render()): break
        finally:
            self.releases += 1
            self.last_step_timing = SimpleNamespace(requested_s=kw['duration_seconds'],
                live_window_s=self.clock[0]-start, interrupted=True, timing_source='fake')
        return self.render()


def make_env(tmp_path, monkeypatch):
    clock = [100.]
    monkeypatch.setattr('coding4game_gui.benchmark.windows.env.time.perf_counter', lambda: clock[0])
    driver = Driver(clock)
    env = WindowsGameEnv('overcooked_2_2_1', driver_env=driver, scorer=scorer(readings()),
        spec=spec(), actions={'NEUTRAL': ActionSpec(name='neutral', description='', buttons=[])})
    env.reset()
    env._terminal_directory = tmp_path
    return env, driver


def test_held_action_stop_release_actual_tics_and_scoring_frame(tmp_path, monkeypatch):
    env, driver = make_env(tmp_path, monkeypatch)
    env.set_action_context(batch_id='batch')
    env.act(['NEUTRAL'], 1000)
    assert env.done and driver.releases == 1 and env.tic == 8
    with pytest.raises(RuntimeError, match='ended'): env.act(['KEY[SPACE]'], 1)
    assert driver.calls == 1 and env.total_reward is None
    env._last_frame = frame(9)  # a later map observation is never a scoring source
    assert env.finalize_score() == pytest.approx(20 + 80*219/220)
    assert env._deferred_score.frame_count == 1
    details = env.save_score_evidence(tmp_path/'settlement')
    assert details['terminal_v2']['diagnostic'] == 'confirmed'
    assert details['reading']['net_score'] == 219
    assert details['score_breakdown']['combined_score'] == pytest.approx(20 + 80*219/220)
    assert details['score_breakdown']['game_score'] == pytest.approx(219/220*100)
    assert details['score_breakdown']['success'] is False
    assert details['milestone_evidence'][0]['sha256']
    env.close()


def test_cross_drive_settlement_preserves_portable_native_evidence(tmp_path, monkeypatch):
    import hashlib
    import os

    env, driver = make_env(tmp_path/'native', monkeypatch)
    try:
        env.act(['NEUTRAL'], 1000)
        score = env.finalize_score()
        source = env._terminal_monitor.writer.directory
        destination = tmp_path/'settlement'
        source_bytes = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
        original_relpath = os.path.relpath

        def different_drive(path, start=None):
            if Path(path) == source and Path(start) == destination:
                raise ValueError("path is on mount 'C:', start on mount 'E:'")
            return original_relpath(path, start)

        with monkeypatch.context() as patch:
            patch.setattr(os.path, 'relpath', different_drive)
            details = env.save_score_evidence(destination)
            repeated = env.save_score_evidence(destination)
        relative = Path(details['terminal_v2']['evidence_directory'])
        assert not relative.is_absolute() and '..' not in relative.parts
        copied = destination/relative
        assert {p.name: p.read_bytes() for p in copied.iterdir() if p.is_file()} == source_bytes
        for row in (copied/'frames.jsonl').read_text().splitlines():
            record = json.loads(row)
            assert hashlib.sha256((copied/record['path']).read_bytes()).hexdigest() == record['sha256']
        assert repeated == details
        assert json.loads((destination/'settlement.json').read_text()) == details
        assert env.total_reward == score and env.tic == 8 and driver.calls == 1
        assert {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()} == source_bytes
    finally:
        env.close()


def test_cross_drive_copy_failure_cannot_publish_settlement(tmp_path, monkeypatch):
    import os
    import shutil

    env, _ = make_env(tmp_path/'native', monkeypatch)
    try:
        env.act(['NEUTRAL'], 1000)
        env.finalize_score()
        source = env._terminal_monitor.writer.directory
        destination = tmp_path/'settlement'
        original_relpath = os.path.relpath

        def different_drive(path, start=None):
            if Path(path) == source and Path(start) == destination:
                raise ValueError("path is on mount 'C:', start on mount 'E:'")
            return original_relpath(path, start)

        def failed_copy(*args, **kwargs):
            raise PermissionError('test evidence copy denied')

        with monkeypatch.context() as patch:
            patch.setattr(os.path, 'relpath', different_drive)
            patch.setattr(shutil, 'copytree', failed_copy)
            with pytest.raises(PermissionError, match='evidence copy denied'):
                env.save_score_evidence(destination)
        assert not (destination/'settlement.json').exists()
    finally:
        env.close()


def test_evidence_failure_stops_and_charges_failed_hold(tmp_path, monkeypatch):
    env, driver = make_env(tmp_path, monkeypatch)
    m = env._get_terminal_monitor()
    monkeypatch.setattr(m.writer, 'add', lambda *a: (_ for _ in ()).throw(EvidenceFailure('test disk full')))
    with pytest.raises(EvidenceFailure): env.act(['NEUTRAL'], 1000)
    assert env.done and env.tic == 3 and driver.releases == 1
    with pytest.raises(RuntimeError): env.act(['KEY[SPACE]'], 1)
    with pytest.raises(EvidenceFailure): env.finalize_score()
    assert (tmp_path/'summary.json').exists()
    assert not m.writer.thread.is_alive()


def test_code_batch_cancels_space_and_rejects_future_input(tmp_path, monkeypatch):
    from coding4game_gui.benchmark.windows.session import GameSession
    env, driver = make_env(tmp_path/'native', monkeypatch)
    with GameSession(lambda: env, tmp_path/'code', tic_budget=1860, max_tics=1860) as game:
        uid = game.start()['episode_id']
        env._terminal_directory = tmp_path/'native'
        row = game.step_batch(uid, [dict(type='wait', tics=1000),
            dict(type='keypress', keys=['SPACE'], tics=10), dict(type='wait', tics=100)])
        # The real startup monitor already sampled the initial frozen frame.
        assert row['done'] and row['tics'] == 5 and driver.calls == 1
        native = game.root / 'evidence' / uid / 'terminal'
        checks = (native / 'checks.jsonl').read_text(encoding='utf-8').splitlines()
        assert json.loads(checks[0])['source'] == 'initial_frozen_frame'
        assert row['batch']['executed_actions'] == 1 and row['batch']['remaining_actions'] == 2
        assert row['score'] == pytest.approx(20 + 80*219/220)
        assert row['score_version'] == 'overcooked_2_2_1_composite_v1'
        assert row['net_score'] == 219
        assert row['success'] is False
        with pytest.raises(ValueError, match='episode is not active'):
            game.step_batch(uid, [dict(type='keypress', keys=['SPACE'], tics=1)])
        assert driver.calls == 1


def test_direct_batch_cancels_space_without_another_model_call(tmp_path, monkeypatch):
    from coding4game_gui.benchmark.direct.player import play_one
    env, driver = make_env(tmp_path/'native', monkeypatch)
    original_reset = env.reset
    def reset(*a, **k):
        original_reset(*a, **k)
        env._terminal_directory = tmp_path/'native'
    env.reset = reset
    calls = []
    def reply(*args):
        calls.append(1)
        return 'ACT 1000 NEUTRAL\nACT 10 KEY[SPACE]\nACT 100 NEUTRAL', 0, 0
    row = play_one(env, reply, 'test-only', '',
                      max_burst=None, max_calls_per_ep=1, max_tics=1860, episode_id=0)
    assert row.tics == 8 and driver.calls == 1 and len(calls) == 1
    assert row.score == pytest.approx(20 + 80*219/220)
    assert row.score_version == 'overcooked_2_2_1_composite_v1'
    assert row.process_score == 20
    assert row.game_score == pytest.approx(219/220*100)
    assert row.success is False


def test_code_evidence_export_and_callback_errors_do_not_skip_cleanup(tmp_path, monkeypatch):
    from coding4game_gui.benchmark.windows.session import GameSession
    env, driver = make_env(tmp_path/'native', monkeypatch)
    game = GameSession(lambda: env, tmp_path/'code', tic_budget=1860)
    uid = game.start()['episode_id']
    env._terminal_directory = tmp_path/'native'
    m = env._get_terminal_monitor()
    def fail(*args): raise EvidenceFailure('disk full')
    monkeypatch.setattr(m.writer, 'add', fail)
    monkeypatch.setattr(env, 'save_score_evidence', fail)
    game.terminal_evidence_failure_callback = fail
    with pytest.raises(EvidenceFailure): game.step_batch(uid, [dict(type='wait', tics=1000)])
    assert game._closed and game._active is None and driver.releases == 1
    assert not m.writer.thread.is_alive()
    row = game._results[uid]
    assert row['tics'] == 3 and not row['complete'] and row['score'] is None
    with pytest.raises(RuntimeError, match='session_closed'): game.start()
    game.close()


def test_direct_evidence_failure_keeps_usage_and_cancels_later_episodes(tmp_path, monkeypatch):
    from coding4game_gui.benchmark.direct.player import play_episodes
    env, driver = make_env(tmp_path/'native', monkeypatch)
    reset_original = env.reset
    def reset(*a, **k):
        reset_original(*a, **k)
        env._terminal_directory = tmp_path/'native'
    env.reset = reset
    def fail(*args): raise EvidenceFailure('disk full')
    monkeypatch.setattr(NativeEvidence, 'add', fail)
    rows = play_episodes(lambda: env, lambda *a: ('ACT 1000 NEUTRAL\nACT 1 KEY[SPACE]', 0, 0),
                         'mock', '', episodes=3, max_burst=None, max_calls_per_ep=1,
                         max_tics=1860)
    assert len(rows) == 1 and rows[0].tics == 3 and rows[0].calls == 1
    assert rows[0].outcome_kind == 'infrastructure_error' and rows[0].score is None
    assert driver.calls == 1 and driver.releases == 1
