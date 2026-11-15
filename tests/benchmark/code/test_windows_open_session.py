import json
from pathlib import Path
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

import coding4game_gui.benchmark.code.open_session as open_session_module
from coding4game_gui.benchmark.code.open_session import OpenSession
from coding4game_gui.benchmark.code.submissions import ProgramStore
from coding4game_gui.benchmark.evidence.timeline import DevelopmentTimeline


class Game:
    def __init__(self, root):
        self.root, self.active, self.rows = root, None, []
    def status(self):
        return dict(active_episode=self.active, episodes=self.rows,
                    tics_remaining=100, tics_used=0)
    def start(self, max_tics=None, mode='stepped', submission_id=None):
        assert self.active is None
        self.active = dict(episode_id='ep1', mode=mode, submission_id=submission_id)
        return self.active.copy()
    def observe(self, uid):
        return self.active or self.rows[-1]
    def step(self, uid, controls, tics):
        return self.stop(uid)
    def stop(self, uid, **kwargs):
        if self.active:
            self.rows.append(dict(self.active, done=True, score=1))
            self.active = None
        return self.rows[-1]
    def close(self):
        if self.active:
            self.stop(self.active['episode_id'])


@pytest.mark.parametrize("mismatch", [False, True])
def test_recovery_restores_only_proven_evaluation_without_replaying(tmp_path, mismatch):
    game=Game(tmp_path);supervisor=Supervisor();store=ProgramStore(tmp_path/'programs')
    programme=store.save({'main.py':b'pass'}, {'argv':['main.py'],'mode':'stepped','runtime':'python'},
                         execution_platform='linux')
    row={'episode_uid':'original','episode_id':'original','submission_id':programme['id'],
         'tics':7,'score':0.2,'complete':True,'terminal_detected':True,
         'outcome_kind':'valid','end_reason':'terminal:combat'}
    game.rows=[dict(row)]
    timeline=DevelopmentTimeline(tmp_path/'timeline.jsonl')
    session=OpenSession(game,supervisor,store,timeline,'python',require_evaluated_submission=True,
                        require_terminal_submission=True,execution_platform='linux')
    proof=dict(row,program_returncode=0,score=0.3 if mismatch else 0.2)
    if mismatch:
        with pytest.raises(ValueError,match='durable gameplay ledger'):
            session.restore_evaluations([proof])
        assert store.final() is None
    else:
        assert session.restore_evaluations([proof,proof])==[programme['id']]
        selected=session.dispatch('submission.finish',{'id':programme['id']},phase='development')
        assert selected['id']==programme['id'] and session.finished
    assert supervisor.launches==[] and game.rows==[row]


class Supervisor:
    def __init__(self):
        self.files = {'main.py': b'print(1)'}
        self.launches, self.stages, self.killed = [], [], []
    def read_tree(self, path):
        return self.files.copy()
    def stage(self, files, name='artifact'):
        self.stages.append((name, files.copy()))
        return str(Path('C:/box') / name)
    def launch(self, argv, cwd=None, env=None):
        self.launches.append((argv, cwd, env))
        return {'handle': 'p1'}
    def poll(self, handle):
        return {'running': False, 'exit_code': 0, 'events': []}
    def terminate(self, handle):
        self.killed.append(handle)
        return {'exit_code': -15}
    def freeze_dependencies(self, files, dependencies, deadline):
        return dependencies or {}
    def prepare_program(self, record, directory, deadline):
        return {'python': 'C:/python/python.exe', 'env': {}, 'verified': False}


def make(tmp_path, phase='development', require_evaluated_submission=False):
    game, supervisor = Game(tmp_path), Supervisor()
    store = ProgramStore(tmp_path/'submissions')
    session = OpenSession(game, supervisor, store,
        DevelopmentTimeline(tmp_path/'timeline.jsonl'), 'C:/python/python.exe', phase=phase,
        require_evaluated_submission=require_evaluated_submission)
    session.bind_bridge(SimpleNamespace(url='http://127.0.0.1:32100/call', token='scoped'))
    return session, supervisor, store


def test_terminal_only_selection_requires_valid_terminal_and_zero_exit(tmp_path):
    session, _, _ = make(tmp_path)
    session.require_terminal_submission = True
    base = dict(program_returncode=0, complete=True, outcome_kind='valid')
    assert not session._candidate_success(dict(base, terminal_detected=False))
    assert session._candidate_success(dict(base, terminal_detected=True))
    assert not session._candidate_success(dict(base, terminal_detected=True, program_returncode=1))
    assert not session._candidate_success(dict(base, terminal_detected=True, complete=False))


def test_terminal_only_status_is_available_and_finish_checks_the_candidate(tmp_path):
    session, _, store = make(tmp_path)
    session.require_terminal_submission = True
    record = store.save({'main.py': b'print(1)'},
                        {'argv': ['main.py'], 'runtime': 'python', 'mode': 'stepped'})
    assert 'submissions' in session.dispatch('bench.status', {}, phase='development')
    with pytest.raises(RuntimeError, match='registered terminal detector'):
        session.dispatch('submission.finish', {'id': record['id']}, phase='development')
    assert store.final() is None
    session._successful_submissions.add(record['id'])
    assert session.dispatch('submission.finish', {'id': record['id']},
                            phase='development')['id'] == record['id']


def save(session):
    return session.dispatch('submission.save', dict(path='candidate', entrypoint={
        'argv': ['main.py'], 'runtime': 'python', 'mode': 'stepped'}), phase='development')


def test_save_run_finish_uses_immutable_copy_and_does_not_auto_test(tmp_path):
    session, supervisor, store = make(tmp_path)
    record = save(session)
    supervisor.files['main.py'] = b'changed'
    result = session.dispatch('submission.run', {'id': record['id']}, phase='development')
    assert result['score'] == 1
    assert supervisor.stages[0][1]['main.py'] == b'print(1)'
    assert supervisor.launches[0][2]['C4G_EPISODE_ID'] == 'ep1'
    session.dispatch('submission.finish', {'id': record['id']}, phase='development')
    assert session.finished and store.final()['id'] == record['id']
    assert len(supervisor.launches) == 1
    with pytest.raises(RuntimeError):
        save(session)


def test_submission_required_mode_refuses_untested_final_candidate(tmp_path):
    session, _, store = make(tmp_path, require_evaluated_submission=True)
    record = save(session)
    with pytest.raises(RuntimeError, match="submission.run"):
        session.dispatch('submission.finish', {'id': record['id']}, phase='development')
    assert store.final() is None
    session.dispatch('submission.run', {'id': record['id']}, phase='development')
    session.dispatch('submission.finish', {'id': record['id']}, phase='development')
    assert store.final()['id'] == record['id']


def test_submission_required_mode_refuses_program_error_as_validation(tmp_path):
    session, supervisor, store = make(tmp_path, require_evaluated_submission=True)
    record = save(session)
    supervisor.poll = lambda _handle: {
        'running': False, 'exit_code': 1, 'events': [], 'output_complete': True,
    }
    result = session.dispatch('submission.run', {'id': record['id']}, phase='development')
    assert result['program_returncode'] == 1
    assert result['program_output'] == {
        'stdout': {'tail': '', 'bytes': 0, 'truncated': False},
        'stderr': {'tail': '', 'bytes': 0, 'truncated': False},
    }
    with pytest.raises(RuntimeError, match="successful submission.run"):
        session.dispatch('submission.finish', {'id': record['id']}, phase='development')
    assert store.final() is None


def test_submission_run_returns_bounded_utf8_safe_redacted_stream_tails(tmp_path):
    session, supervisor, _ = make(tmp_path)
    record = save(session)
    bridge_secret = session._bridge.token
    gateway_secret = 'gateway-secret-0123456789abcdef'
    upstream_secret = 'upstream-secret-0123456789abcdef'
    session.gateway = SimpleNamespace(token=gateway_secret)
    session.timeline.secrets = (upstream_secret,)
    stderr = 'A' * 50 + upstream_secret + 'Z' * 4080 + '雪'
    events = [
        {'stream': 'stdout', 'data': f'bridge={bridge_secret[:3]}'},
        {'stream': 'stdout', 'data': bridge_secret[3:] + ' gateway=' + gateway_secret},
        {'stream': 'stderr', 'data': stderr},
    ]
    supervisor.poll = lambda _handle: {
        'running': False, 'exit_code': 1, 'events': events,
        'output_complete': True,
    }

    result = session.run_program(record['id'])

    output = result['program_output']
    assert output['stdout']['bytes'] == len(
        f'bridge={bridge_secret} gateway={gateway_secret}'.encode('utf-8'))
    assert output['stdout']['truncated'] is False
    assert output['stdout']['tail'] == 'bridge=[REDACTED] gateway=[REDACTED]'
    assert output['stderr']['bytes'] == len(stderr.encode('utf-8'))
    assert output['stderr']['truncated'] is True
    assert len(output['stderr']['tail'].encode('utf-8')) <= 4096
    assert output['stderr']['tail'].endswith('雪')
    assert upstream_secret not in output['stderr']['tail']
    assert upstream_secret[-12:] not in output['stderr']['tail']
    assert '[REDACTED]' in output['stderr']['tail']
    host_log = (tmp_path / 'program_output/ep1.jsonl').read_text(encoding='utf-8')
    assert gateway_secret not in host_log
    assert upstream_secret not in host_log
    assert '[REDACTED]' in host_log


def test_submission_output_overflow_error_contains_bounded_redacted_diagnostics(tmp_path):
    session, supervisor, _ = make(tmp_path)
    record = save(session)
    supervisor.poll = lambda _handle: {
        'running': True, 'output_overflow': True,
        'events': [{'stream': 'stderr',
                    'data': 'failure ' + 'x' * 5000 + ' ' + session._bridge.token}],
    }

    with pytest.raises(RuntimeError, match='output overflow') as caught:
        session.run_program(record['id'])

    message = str(caught.value)
    assert session._bridge.token not in message
    assert '[REDACTED]' in message
    assert len(message.encode('utf-8')) < 9000
    assert '"truncated": true' in message


def test_submission_diagnostics_redact_overlapping_secrets_and_accept_bytes(tmp_path):
    session, supervisor, _ = make(tmp_path)
    record = save(session)
    session._bridge.token = 'shared-secret-long'
    session.timeline.secrets = ('shared-secret',)
    supervisor.poll = lambda _handle: {
        'running': False, 'exit_code': 1, 'output_complete': True,
        'events': [{'stream': 'stderr', 'data': b'prefix shared-secret-long suffix'}],
    }

    result = session.run_program(record['id'])

    stderr = result['program_output']['stderr']
    assert stderr == {'tail': 'prefix [REDACTED] suffix',
                      'bytes': len(b'prefix shared-secret-long suffix'),
                      'truncated': False}


def test_submission_diagnostics_report_redaction_expansion_truncation(tmp_path):
    session, supervisor, _ = make(tmp_path)
    record = save(session)
    session._bridge.token = 'x'
    supervisor.poll = lambda _handle: {
        'running': False, 'exit_code': 1, 'output_complete': True,
        'events': [{'stream': 'stdout', 'data': 'x' * 500}],
    }

    result = session.run_program(record['id'])

    stdout = result['program_output']['stdout']
    assert stdout['bytes'] == 500
    assert stdout['truncated'] is True
    assert len(stdout['tail'].encode('utf-8')) == 4096
    assert 'x' not in stdout['tail']


def test_finish_with_unused_tics_selects_and_stops_active_episode(tmp_path):
    session, _, store = make(tmp_path)
    record = save(session)
    session.game.start()
    result = session.dispatch('submission.finish', {'id': record['id']}, phase='development')
    assert result['id'] == record['id']
    assert session.finished and store.final()['id'] == record['id']
    assert session.game.active is None
    assert session.game.rows[-1]['done'] is True


def test_finish_rejects_unknown_candidate_before_stopping_active_episode(tmp_path):
    session, _, store = make(tmp_path)
    session.game.start()
    with pytest.raises((KeyError, ValueError)):
        session.dispatch('submission.finish', {'id': 'missing'}, phase='development')
    assert not session.finished and store.final() is None
    assert session.game.active is not None


def test_final_tools_cannot_reset_or_read_development_history(tmp_path):
    session, _, _ = make(tmp_path, 'final_test')
    for tool in ['game.start', 'submission.save', 'bench.replay', 'submission.finish']:
        with pytest.raises(ValueError):
            session.dispatch(tool, {}, phase='final_test')
    with pytest.raises(ValueError):
        session.dispatch('bench.status', {}, phase='development')
    assert 'episodes' not in session.dispatch('bench.status', {}, phase='final_test')


def test_schema_rejection_precedes_effects(tmp_path):
    session, supervisor, _ = make(tmp_path)
    with pytest.raises(ValueError):
        session.dispatch('game.start', {'mode': 'live'}, phase='development')
    with pytest.raises(ValueError):
        session.dispatch('submission.save', {'path': 'a', 'entrypoint': {}, 'extra': 1}, phase='development')
    assert session.game.active is None and not supervisor.stages


def test_exhausted_game_budget_does_not_block_status_save_or_finish(tmp_path):
    session, supervisor, store = make(tmp_path)
    session.game.status = lambda: {'active_episode':None,'tics_remaining':0,'tics_used':5,
                                   'budget_stop_reason':'tic_budget_exhausted'}
    assert session.dispatch('bench.status',{},phase='development')['tics_remaining'] == 0
    record = save(session)
    session.dispatch('submission.finish',{'id':record['id']},phase='development')
    assert store.final()['id'] == record['id'] and not supervisor.launches


def test_final_launch_has_only_game_connection_and_no_gateway(tmp_path):
    session, supervisor, store = make(tmp_path, 'final_test')
    record = store.save({'main.py': b'pass'}, {'argv': ['main.py'], 'runtime': 'python', 'mode': 'stepped'})
    session.run_program(record['id'])
    env = supervisor.launches[0][2]
    assert set(env) == {'C4G_BRIDGE_URL', 'C4G_BRIDGE_TOKEN', 'C4G_EPISODE_ID', 'C4G_BRIDGE_PHASE'}
    assert env['C4G_BRIDGE_PHASE'] == 'final_test'


def test_child_can_use_bridge_while_submission_run_waits(tmp_path):
    session, supervisor, _ = make(tmp_path)
    record = save(session)
    def poll(handle):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor() as pool:
            result = pool.submit(session.dispatch, 'game.step',
                {'controls': ['NEUTRAL'], 'tics': 1}, phase='development').result(timeout=2)
        assert result['done']
        return {'running': False, 'exit_code': 0, 'events': []}
    supervisor.poll = poll
    assert session.run_program(record['id'])['done']


def test_game_terminal_terminates_then_drains_program_output(tmp_path):
    session, supervisor, _ = make(tmp_path)
    record = save(session)
    calls = []
    def poll(handle):
        calls.append(handle)
        if len(calls) == 1:
            session.game.stop('ep1')
            return {'running': True, 'events': [], 'output_complete': False}
        return {'running': False, 'exit_code': 1, 'output_complete': True,
                'events': [{'stream': 'stderr', 'data': 'final diagnostic'}]}
    supervisor.poll = poll
    result = session.run_program(record['id'])
    assert result['program_output_complete'] is True
    assert result['program_exit_policy'] == 'cooperative_after_game_end'
    assert result['program_output']['stderr']['tail'] == 'final diagnostic'
    assert supervisor.killed == []
    assert 'final diagnostic' in (tmp_path/'program_output/ep1.jsonl').read_text()


def test_game_terminal_allows_bounded_cooperative_zero_exit(tmp_path, monkeypatch):
    session, supervisor, store = make(
        tmp_path, require_evaluated_submission=True)
    record = save(session)
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(open_session_module.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(
        open_session_module.time, 'sleep',
        lambda seconds: setattr(clock, 'now', clock.now + seconds))
    polls = 0

    def poll(handle):
        nonlocal polls
        polls += 1
        if polls == 1:
            session.game.stop('ep1')
            return {'running': True, 'events': [], 'output_complete': False}
        return {'running': False, 'exit_code': 0, 'events': [],
                'output_complete': True}

    supervisor.poll = poll
    result = session.run_program(record['id'])
    assert result['program_returncode'] == 0
    assert result['program_exit_policy'] == 'cooperative_after_game_end'
    assert result['cooperative_exit_wait_seconds'] == pytest.approx(.02)
    assert supervisor.killed == []
    session.dispatch('submission.finish', {'id': record['id']}, phase='development')
    assert store.final()['id'] == record['id']


def test_game_terminal_forces_exit_after_bounded_grace(tmp_path, monkeypatch):
    session, supervisor, store = make(
        tmp_path, require_evaluated_submission=True)
    record = save(session)
    clock = SimpleNamespace(now=20.0)
    monkeypatch.setattr(open_session_module.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(
        open_session_module.time, 'sleep',
        lambda seconds: setattr(clock, 'now', clock.now + seconds))
    first = True

    def poll(handle):
        nonlocal first
        if first:
            first = False
            session.game.stop('ep1')
        if supervisor.killed:
            return {'running': False, 'exit_code': -15, 'events': [],
                    'output_complete': True}
        return {'running': True, 'events': [], 'output_complete': False}

    supervisor.poll = poll
    result = session.run_program(record['id'])
    assert result['program_returncode'] == -15
    assert result['program_exit_policy'] == 'forced_after_game_end_grace'
    assert result['cooperative_exit_wait_seconds'] == pytest.approx(2.0)
    assert supervisor.killed == ['p1']
    with pytest.raises(RuntimeError, match='successful submission.run'):
        session.dispatch('submission.finish', {'id': record['id']}, phase='development')
    assert store.final() is None


def test_deadline_before_first_poll_immediately_terminates_child(tmp_path, monkeypatch):
    session, supervisor, _ = make(tmp_path)
    record = save(session)
    session.deadline = 30.0
    monkeypatch.setattr(open_session_module.time, 'monotonic', lambda: 30.0)
    supervisor.poll = lambda _handle: pytest.fail('deadline must precede child polling')

    result = session.run_program(record['id'])

    assert supervisor.killed == ['p1']
    assert result['program_returncode'] == -15
    assert result['program_exit_policy'] == 'forced_deadline'
    assert result['cooperative_exit_wait_seconds'] == 0.0


def test_nested_python_entrypoint_gets_importable_client(tmp_path):
    session, supervisor, store = make(tmp_path)
    record = store.save({'src/main.py': b'from c4g_client import Client'},
        {'argv': ['src/main.py'], 'runtime': 'python', 'mode': 'stepped'})
    session.run_program(record['id'])
    assert 'src/c4g_client.py' in supervisor.stages[0][1]


def test_linux_program_uses_supervisor_native_paths_and_rejects_other_platform(tmp_path):
    session, supervisor, store = make(tmp_path)
    session.execution_platform = 'linux'
    supervisor.stage = lambda files, name='artifact': f'/home/c4g/run/{name}'
    supervisor.native_path = lambda directory, *parts: str(PurePosixPath(directory).joinpath(*parts))
    record = store.save({'main.py': b'pass'},
        {'argv': ['main.py'], 'runtime': 'python', 'mode': 'stepped'},
        execution_platform='linux')
    session.run_program(record['id'])
    assert supervisor.launches[0][0][0] == 'C:/python/python.exe'
    assert supervisor.launches[0][0][1].startswith('/home/c4g/run/runs/')
    legacy = store.save({'old.py': b'pass'},
        {'argv': ['old.py'], 'runtime': 'python', 'mode': 'stepped'})
    with pytest.raises(ValueError, match='platform differs'):
        session.run_program(legacy['id'])


def test_linux_save_freezes_dependencies_and_rewrites_bundled_metadata(tmp_path):
    session, supervisor, store = make(tmp_path)
    session.execution_platform = 'linux'
    declared = {'environment': {'requirements': ['pyfiglet==1.0.2']}}
    supervisor.files['c4g-program.json'] = json.dumps(
        {'dependencies': declared, 'other': 'preserved'}).encode()
    locked = {**declared, 'environment_lock': {'id': 'lock-1', 'artifacts': []}}
    seen = {}
    def freeze(files, dependencies, deadline):
        seen.update(files=files, dependencies=dependencies, deadline=deadline)
        return locked
    supervisor.freeze_dependencies = freeze

    record = session.dispatch('submission.save', {'path': 'candidate', 'entrypoint': {
        'argv': ['main.py'], 'runtime': 'python', 'mode': 'stepped'}},
        phase='development')

    assert seen['dependencies'] == declared
    assert record['dependencies'] == locked
    _, files = store.get(record['id'])
    metadata = json.loads(files['c4g-program.json'])
    assert metadata == {'dependencies': locked, 'other': 'preserved'}


def test_linux_prepares_before_game_and_uses_prepared_python_and_environment(tmp_path):
    session, supervisor, store = make(tmp_path)
    session.execution_platform = 'linux'
    supervisor.stage = lambda files, name='artifact': f'/home/c4g/run/{name}'
    supervisor.native_path = lambda directory, *parts: str(
        PurePosixPath(directory).joinpath(*parts))
    record = store.save({'main.py': b'pass'},
        {'argv': ['main.py'], 'runtime': 'python', 'mode': 'stepped'},
        dependencies={'environment_lock': {'id': 'lock-1'}},
        execution_platform='linux')
    def prepare(candidate, directory, deadline):
        assert session.game.status()['active_episode'] is None
        assert candidate['id'] == record['id']
        return {'python': '/opt/c4g-envs/lock-1/bin/python',
                'env': {'C4G_ASSET_DIR': '/opt/c4g-assets/lock-1'},
                'verified': True, 'lock_id': 'lock-1'}
    supervisor.prepare_program = prepare

    session.run_program(record['id'])

    argv, _, env = supervisor.launches[0]
    assert argv[0] == '/opt/c4g-envs/lock-1/bin/python'
    assert env['C4G_ASSET_DIR'] == '/opt/c4g-assets/lock-1'
    assert env['C4G_BRIDGE_TOKEN'] == 'scoped'


def test_linux_preparation_failure_starts_no_game_and_clears_run_reservation(tmp_path):
    session, supervisor, store = make(tmp_path)
    session.execution_platform = 'linux'
    record = store.save({'main.py': b'pass'},
        {'argv': ['main.py'], 'runtime': 'python', 'mode': 'stepped'},
        execution_platform='linux')
    supervisor.prepare_program = lambda *args: (_ for _ in ()).throw(
        RuntimeError('dependency preparation failed'))

    with pytest.raises(RuntimeError, match='dependency preparation failed'):
        session.run_program(record['id'])

    with pytest.raises(RuntimeError, match='dependency preparation failed'):
        session.run_program(record['id'])

    assert session.game.status()['episodes'] == []
    assert session.game.status()['active_episode'] is None
    assert supervisor.launches == []


def test_linux_session_rejects_native_submission_before_reading_workspace(tmp_path):
    session, supervisor, _ = make(tmp_path)
    session.execution_platform = 'linux'
    with pytest.raises(ValueError, match='Python submitted programs only'):
        session.dispatch('submission.save', {'path': 'candidate', 'entrypoint': {
            'argv': ['main.exe'], 'runtime': 'native', 'mode': 'stepped'}},
            phase='development')
    assert supervisor.stages == []


def test_startup_exhaustion_revokes_model_access(tmp_path):
    from coding4game_gui.benchmark.windows.session import StartupRetriesExhausted
    from unittest.mock import Mock
    session, supervisor, store = make(tmp_path)
    session.gateway = Mock()
    session.game.start = Mock(side_effect=StartupRetriesExhausted('startup_retries_exhausted'))
    with pytest.raises(StartupRetriesExhausted):
        session.dispatch('game.start', {'mode': 'stepped'}, phase='development')
    session.gateway.revoke.assert_called_once_with('game_startup_failed')


def test_submission_startup_exhaustion_revokes_model_access(tmp_path):
    from coding4game_gui.benchmark.windows.session import StartupRetriesExhausted
    from unittest.mock import Mock
    session, supervisor, store = make(tmp_path)
    candidate = save(session)
    session.gateway = Mock()
    session.game.start = Mock(side_effect=StartupRetriesExhausted('startup_retries_exhausted'))
    with pytest.raises(StartupRetriesExhausted):
        session.dispatch('submission.run', {'id': candidate['id']}, phase='development')
    session.gateway.revoke.assert_called_once_with('game_startup_failed')
    assert supervisor.launches == []


@pytest.mark.parametrize("tool", ["game.start", "submission.run"])
def test_nonretryable_native_startup_failure_revokes_model_access(tmp_path, tool):
    from unittest.mock import Mock
    from coding4game_gui.benchmark.windows.session import GameSession, StartupRetriesExhausted

    class NativeStartupError(Exception):
        pass

    calls = []

    def factory():
        calls.append("startup")
        raise NativeStartupError("CO_E_NOTINITIALIZED")

    session, supervisor, _ = make(tmp_path)
    candidate = save(session)
    session.gateway = Mock()
    with GameSession(factory, tmp_path / "game", tic_budget=100) as game:
        session.game = game
        arguments = {"mode": "stepped"} if tool == "game.start" else {"id": candidate["id"]}
        with pytest.raises(StartupRetriesExhausted, match="CO_E_NOTINITIALIZED") as failed:
            session.dispatch(tool, arguments, phase="development")
        assert isinstance(failed.value.__cause__, NativeStartupError)
        assert calls == ["startup"]
        assert game.status()["tics_remaining"] == 100
        rows = game.status()["episodes"]
        assert len(rows) == 1 and rows[0]["score"] is None
        assert rows[0]["end_reason"] == "startup_error"
        assert rows[0]["outcome_kind"] == "infrastructure_error"
        with pytest.raises(RuntimeError, match="session_closed"):
            game.start()
    session.gateway.revoke.assert_called_once_with("game_startup_failed")
    assert supervisor.launches == []
