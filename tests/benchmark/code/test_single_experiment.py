"""Single-run launcher checks with no game launches or paid calls."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import json
import hashlib
import pytest
from coding4game_gui.benchmark.code.run_codex import parser

spec = importlib.util.spec_from_file_location('single', Path(__file__).resolve().parents[3]/'scripts/run_single_experiment.py')
single = importlib.util.module_from_spec(spec)
spec.loader.exec_module(single)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def unexpected_prompt(*args):
        raise AssertionError('The single-run launcher must not prompt')
    monkeypatch.setattr('builtins.input', unexpected_prompt)
    profile = tmp_path/'profile.json'
    profile.write_text(json.dumps({
        'format': 'c4g-wsl2-v1', 'id': '0123456789ab',
        'root': str(tmp_path / 'runtime-0123456789ab'),
        'baseline': str(tmp_path / 'baseline.tar'), 'baseline_sha256': 'a' * 64,
        'user': 'c4gprobe', 'uid': 1000, 'python': '/usr/bin/python3',
        'agents': {'claude': '/opt/c4g-tools/claude'},
        'ports': dict(zip(('development_control', 'development_game',
                          'final_test_control', 'final_test_game', 'gateway'), range(15001, 15006))),
    }))
    monkeypatch.setattr(single, 'load_provider_route', lambda *a, **kw: SimpleNamespace(
        model='gpt-5.6-luna', api_base='https://example.invalid/v1',
        api_key='secret-fixture-key', provider_model='openai/gpt-5.6-luna',
        api_mode='responses'))
    monkeypatch.setattr(single, 'load_game_spec', lambda *a, **kw: {'runtime': {'max_tics': None}})
    calls = []
    monkeypatch.setattr(single.subprocess, 'run', lambda cmd, **kw: calls.append((cmd,kw,dict(kw['env']))) or SimpleNamespace(returncode=7))
    return ['--game','balatro','--model','gpt-5.6-luna','--profile',str(profile)], calls


def test_preview_no_launch_and_unique_outputs(setup, capsys):
    args,calls = setup
    assert single.main(args+['--preview']) == 0
    first = capsys.readouterr().out
    assert single.main(args+['--preview']) == 0
    second = capsys.readouterr().out
    a = json.JSONDecoder().raw_decode(first)[0]
    b = json.JSONDecoder().raw_decode(second)[0]
    assert a['out_dir'] != b['out_dir']
    assert 'task_instructions' not in a
    assert not Path(a['out_dir']).exists()
    assert not calls and 'secret-fixture-key' not in first


@pytest.mark.parametrize('option', ['--recovery-route-authorization', '--development-continuation-authorization', '--recovery-budget-notice'])
def test_route_authorization_is_not_a_new_development_override(setup, option):
    args, calls = setup
    with pytest.raises(SystemExit):
        single.main(args + [option, 'grant.json', '--preview'])
    assert not calls


def test_fresh_run_review_policy_is_forwarded_without_altering_original_budgets(setup, capsys):
    args, calls = setup
    assert single.main(args + ['--minimum-development-fraction', '.5', '--preview']) == 0
    report = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]
    parsed = parser().parse_args(report['command'][3:])
    assert parsed.minimum_development_fraction == .5
    assert parsed.tic_budget == 10000 and parsed.token_limit is None and parsed.wall_seconds is None
    assert not calls


def test_stop_on_http_400_is_explicitly_forwarded_for_new_development(setup, capsys):
    args, calls = setup
    assert single.main(args + ['--stop-on-http-400', '--preview']) == 0
    report = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]
    parsed = parser().parse_args(report['command'][3:])
    assert parsed.stop_on_http_400 is True
    assert not parsed.execute and not calls


def test_generated_output_uses_configured_root(setup, tmp_path, capsys):
    args, calls = setup
    root = tmp_path / 'experiments'
    assert single.main(args + ['--output-root', str(root), '--preview']) == 0
    report = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]
    assert Path(report['out_dir']).parent == root
    assert not root.exists() and not calls


def test_task_instructions_are_validated_and_forwarded_by_preview(
        setup, tmp_path, capsys):
    args, calls = setup
    instruction = tmp_path / "acceptance.md"
    raw = b"Run the local image acceptance.\n"
    instruction.write_bytes(raw)
    assert single.main(args + ['--task-instructions-file', str(instruction),
                               '--preview']) == 0
    report = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]
    assert report['task_instructions'] == {
        'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    assert raw.decode().strip() not in json.dumps(report)
    parsed = parser().parse_args(report['command'][3:])
    assert parsed.task_instructions_file == str(instruction.resolve())
    assert not calls


@pytest.mark.parametrize('payload', [b'\xff', b'x' * (64 * 1024 + 1)],
                         ids=['invalid-utf8', 'too-large'])
def test_invalid_task_instructions_block_single_launcher(
        setup, tmp_path, payload):
    args, calls = setup
    instruction = tmp_path / 'bad.md'
    instruction.write_bytes(payload)
    assert single.main(args + ['--task-instructions-file', str(instruction),
                               '--preview']) == 2
    assert not calls


def test_exactly_one_launch_and_exit_status(setup, monkeypatch, capsys):
    args,calls = setup
    assert single.main(args) == 7
    assert len(calls) == 1
    cmd,kw,env = calls[0]
    parsed = parser().parse_args(cmd[3:])
    assert parsed.scenario == 'balatro' and parsed.windows_input == 'gamepad-kbm'
    assert parsed.tic_budget == 10000 and parsed.token_limit is None and parsed.wall_seconds is None
    assert parsed.execute and parsed.confirm_paid_run
    assert parsed.upstream_api == 'responses'
    assert kw['cwd'] == single.REPO and env[single.KEY_ENV] == 'secret-fixture-key'
    assert single.KEY_ENV not in kw['env']
    assert 'secret-fixture-key' not in capsys.readouterr().out


def test_preview_does_not_enable_underlying_execution(setup, capsys):
    args,calls = setup
    assert single.main(args+['--preview']) == 0
    command = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]['command']
    assert '--execute' not in command and '--confirm-paid-run' not in command
    assert not calls


def test_batch_output_path_is_forwarded_without_preview_side_effects(setup, tmp_path, capsys):
    args, calls = setup
    output = tmp_path / 'batch' / 'job_input-gamepad-kbm'
    assert single.main(args + ['--out-dir', str(output), '--preview']) == 0
    report = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]
    assert Path(report['out_dir']) == output
    assert parser().parse_args(report['command'][3:]).out_dir == str(output)
    assert not output.exists() and not calls


@pytest.mark.parametrize('preview', [True, False])
def test_existing_batch_output_never_launches_or_overwrites(setup, tmp_path, preview):
    args, calls = setup
    output = tmp_path / 'existing_input-gamepad-kbm'
    output.mkdir()
    evidence = output / 'evidence.txt'
    evidence.write_text('preserve')
    assert single.main(args + ['--out-dir', str(output)] +
                       (['--preview'] if preview else [])) == 2
    assert not calls and evidence.read_text() == 'preserve'


@pytest.mark.parametrize('requested,expected', [(None, 1000), (20000, 1000), (300, 300)])
def test_registered_episode_cap_is_forwarded(setup, monkeypatch, capsys, requested, expected):
    args, calls = setup
    monkeypatch.setattr(single, 'load_game_spec', lambda *a, **kw: {'runtime': {'max_tics': 1000}})
    extra = [] if requested is None else ['--max-tics', str(requested)]
    assert single.main(args + extra + ['--preview']) == 0
    command = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]['command']
    assert parser().parse_args(command[3:]).max_tics == expected
    assert not calls


def test_missing_guide_blocks_execution(setup, monkeypatch):
    args,calls = setup
    def fail(*a, **kw): raise ValueError('missing keyboard guide')
    monkeypatch.setattr(single,'load_game_spec',fail)
    assert single.main(args) == 2
    assert not calls


@pytest.mark.parametrize('tokens,wall', [('unlimited','unlimited'), ('60000','300'), ('unlimited','300'), ('60000','unlimited')])
def test_explicit_budgets_match_directory_preview_and_runner(setup, monkeypatch, capsys, tokens, wall):
    args,calls = setup
    assert single.main(args+['--tic-budget','5000','--token-limit',tokens,
                            '--wall-seconds',wall]) == 7
    command = calls[0][0]
    parsed = parser().parse_args(command[3:])
    assert parsed.tic_budget == 5000
    assert parsed.token_limit == (None if tokens == 'unlimited' else int(tokens))
    assert parsed.wall_seconds == (None if wall == 'unlimited' else int(wall))
    assert f'_tics-5000_tokens-{tokens}_wall-{wall}_' in parsed.out_dir
    assert parsed.out_dir.endswith('_input-gamepad-kbm')
    preview = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]
    assert preview['out_dir'] == parsed.out_dir
    assert str(preview['limits']['token_limit']) == tokens
    assert str(preview['limits']['wall_seconds']) == wall


@pytest.mark.parametrize('flag,value', [('--tic-budget','0'),('--tic-budget','unlimited'),
    ('--token-limit','-1'),('--wall-seconds','0'),('--wall-seconds','1.5'),('--token-limit','bad')])
def test_invalid_budget_rejected_without_launch(setup, flag, value):
    args,calls = setup
    with pytest.raises(SystemExit): single.main(args+[flag,value])
    assert not calls


@pytest.mark.parametrize('mode', ['gamepad', 'gamepad-kbm'])
@pytest.mark.parametrize('preview', [True, False])
def test_input_mode_matches_spec_command_and_directory(setup, monkeypatch, capsys, mode, preview):
    args, calls = setup
    loaded = []
    def load(game, *, windows_input):
        loaded.append((game, windows_input))
        return {'runtime': {'max_tics': None}}
    monkeypatch.setattr(single, 'load_game_spec', load)
    assert single.main(args + ['--windows-input', mode] +
                       (['--preview'] if preview else [])) == (0 if preview else 7)
    report = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]
    parsed = parser().parse_args(report['command'][3:])
    assert loaded == [('balatro', mode)]
    assert parsed.windows_input == mode
    assert report['out_dir'].endswith('_input-' + mode)
    assert len(calls) == (0 if preview else 1)


def test_configured_non_openai_model_is_accepted(setup, monkeypatch):
    args, calls = setup
    loaded = []

    def load(path, selector, **kwargs):
        loaded.append((selector, kwargs))
        return SimpleNamespace(model='gemini-3.8-flash', api_base='https://example.invalid/gemini',
                               api_key='secret-fixture-key', provider_model='gemini/gemini-3.8-flash',
                               api_mode='responses')

    monkeypatch.setattr(single, 'load_provider_route', load)
    model_index = args.index('--model') + 1
    args[model_index] = 'gemini-3.8-flash'
    assert single.main(args + ['--preview']) == 0
    assert loaded == [('gemini-3.8-flash', {'allow_non_openai': True})]
    assert not calls
