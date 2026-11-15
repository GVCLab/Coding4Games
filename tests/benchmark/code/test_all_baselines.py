import json
from types import SimpleNamespace

import pytest

from coding4game_gui.benchmark.direct.run_direct_baseline import _build_argparser
from coding4game_gui.catalog import FORMAL_SCENARIOS, input_mode
from scripts import run_all_baselines as batch


def test_preview_never_launches(monkeypatch, capsys):
    monkeypatch.setattr(batch.subprocess, 'run', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('launched')))
    assert batch.main(['--games', 'balatro', '--models', 'test-one', 'test-two']) == 0
    assert 'Preview only' in capsys.readouterr().out


def test_sequential_batch_continues_after_failure_and_releases_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(batch, 'REPO', tmp_path)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(batch.subprocess, 'run', run)
    output_root = tmp_path / 'experiments'
    assert batch.main(['--games', 'balatro', '--models', 'test-one', 'test-two',
                       '--output-root', str(output_root), '--execute']) == 1
    assert len(calls) == 2
    assert not (output_root / '_baseline_batches/active.lock').exists()
    assert len(list((output_root / '_baseline_batches').glob('*.jsonl'))) == 1


def test_every_formal_scenario_uses_one_direct_parser(capsys, tmp_path):
    assert batch.main(['--games', *FORMAL_SCENARIOS, '--models', 'one', 'two',
                       '--tic-budget', '123', '--episodes', '2',
                       '--output-root', str(tmp_path)]) == 0
    report = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]
    assert report['protocol'] == 'direct-stepped'
    assert len(report['jobs']) == 2 * len(FORMAL_SCENARIOS)
    for job in report['jobs']:
        assert job['command'][3] == 'direct'
        parsed = _build_argparser().parse_args(job['command'][4:])
        assert parsed.scenario == job['game']
        assert parsed.player_model == job['model']
        assert parsed.max_tics == job['max_tics_per_episode'] == 123
        assert parsed.episodes == 2
        assert parsed.windows_input == input_mode(job['game'])
        assert parsed.out_root == str(tmp_path)


@pytest.mark.parametrize("arguments", [
    ['--models', 'test-model'],
    ['--games', 'balatro', '--models', 'claude=test'],
])
def test_batch_requires_explicit_scenarios_and_current_options(arguments):
    with pytest.raises(SystemExit):
        batch.main(arguments)


def test_operator_interrupt_stops_remaining_jobs(monkeypatch, tmp_path):
    monkeypatch.setattr(batch, 'REPO', tmp_path)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=3221225786)
    monkeypatch.setattr(batch.subprocess, 'run', run)
    output_root = tmp_path / 'experiments'
    assert batch.main(['--games', 'balatro', '--models', 'one', 'two',
                       '--output-root', str(output_root), '--execute']) == 3221225786
    assert len(calls) == 1
    assert not (output_root / '_baseline_batches/active.lock').exists()
