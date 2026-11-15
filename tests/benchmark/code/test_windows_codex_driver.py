import json
from pathlib import Path
import pytest

from coding4game_gui.benchmark.code.codex_driver import prepare_codex_run
from coding4game_gui.benchmark.code.codex_driver import run_codex


class Timeline:
    def __init__(self, path=None):
        self.rows = []
        self.path = path

    def append(self, kind, **kwargs):
        self.rows.append({"kind": kind, **kwargs})


class FakeSupervisor:
    def __init__(self, polls):
        self.workspace = Path(r"C:\boxed\workspace")
        self.polls = iter(polls)
        self.staged = []
        self.launches = []
        self.terminated = []

    def stage(self, files, name="artifact"):
        self.staged.append((name, files))
        return str(self.workspace / name)

    def launch_codex(self, argv, **kwargs):
        self.launches.append((argv, kwargs))
        return {"handle": "child", "running": True}

    def poll(self, handle):
        assert handle == "child"
        return next(self.polls)

    def terminate(self, handle):
        self.terminated.append(handle)
        return {"terminated": True, "exit_code": -1}


class Profile:
    python = Path(r"C:\runtime\python.exe")
    codex = Path(r"C:\tools\codex.exe")


class Gateway:
    base_url = "http://127.0.0.1:51005/v1"
    token = "gateway-secret-" + "x" * 32
    port = 51005
    model = "fixed-codex-model"

    def __init__(self):
        self.revoked = False

    def revoke(self, reason="session_closed"):
        self.revoked = True

    def status(self):
        return {}


class Bridge:
    url = "http://127.0.0.1:51002/call"
    token = "bridge-secret-" + "y" * 32


@pytest.mark.parametrize("changed", [False, True])
def test_restored_codex_launch_resumes_exact_thread_or_fails_closed(changed):
    session="11111111-1111-4111-8111-111111111111"
    observed="22222222-2222-4222-8222-222222222222" if changed else session
    supervisor=FakeSupervisor([{"running":False,"exit_code":0,"events":[
        {"stream":"stdout","data":json.dumps({"type":"thread.started","thread_id":observed})+"\n"}]}])
    gateway=Gateway();timeline=Timeline()
    if changed:
        with pytest.raises(RuntimeError,match="identity changed"):
            run_codex(supervisor,Profile(),gateway,Bridge(),"Continue preserved work",timeline,
                      deadline=100,clock=lambda:1,poll_interval=0,resume_session_id=session)
        assert supervisor.terminated==["child"] and gateway.revoked
    else:
        outcome=run_codex(supervisor,Profile(),gateway,Bridge(),"Continue preserved work",timeline,
                          deadline=100,clock=lambda:1,poll_interval=0,resume_session_id=session)
        assert outcome["thread_id"]==session and not outcome["explicit_finished"]
        assert outcome["recovery_identity_observed"] is True
    assert len(supervisor.launches)==1
    argv,options=supervisor.launches[0]
    assert argv[-5:]==["resume","--json","--skip-git-repo-check",session,"-"]
    assert options["stdin_text"]=="Continue preserved work"


def test_prepare_stages_isolated_user_config_and_secret_free_argv():
    supervisor = FakeSupervisor([])
    prepared = prepare_codex_run(supervisor, Profile(), Gateway(), Bridge(), "do work")
    assert [name for name, _files in supervisor.staged] == ["mcp", ".codex"]
    client = supervisor.staged[0][1]
    assert set(client) == {"client.py", "c4g_client.py"}
    assert b"def mcp_main" in client["client.py"]
    assert client["c4g_client.py"] == client["client.py"]
    config = supervisor.staged[1][1]["config.toml"].decode()
    assert 'model = "fixed-codex-model"' in config
    assert 'model_provider = "c4g_gateway"' in config
    assert 'web_search = "live"' in config
    assert 'tool_output_token_limit = 8000' in config
    assert 'model_auto_compact_token_limit' not in config
    assert 'model_auto_compact_token_limit_scope' not in config
    assert 'base_url = "http://127.0.0.1:51005/v1"' in config
    assert 'env_key = "C4G_GATEWAY_TOKEN"' in config
    assert 'wire_api = "responses"' in config
    assert 'stream_max_retries = 5' in config
    assert 'request_max_retries = 5' in config
    assert '[mcp_servers.c4g_game]' in config
    assert '"-I", "-B", "-X", "utf8"' in config
    assert 'command = "C:\\\\runtime\\\\python.exe"' in config
    assert 'env_vars = ["C4G_BRIDGE_URL", "C4G_BRIDGE_TOKEN", "C4G_BRIDGE_PHASE"]' in config
    assert "gateway-secret" not in config and "bridge-secret" not in config
    assert prepared["argv"] == [str(Profile.codex), "exec", "--json",
                                "--skip-git-repo-check", "--sandbox",
                                "danger-full-access", "-"]
    assert prepared["stdin_text"] == "do work"
    assert all("secret" not in arg for arg in prepared["argv"])
    assert prepared["connection_env"] == {
        "C4G_BRIDGE_URL": Bridge.url,
        "C4G_BRIDGE_TOKEN": Bridge.token,
        "C4G_BRIDGE_PHASE": "development",
    }


def test_long_prompt_is_preserved_outside_command_line():
    prompt = ('Explore, test, and improve.\n' * 5000) + '\u2603'
    prepared = prepare_codex_run(FakeSupervisor([]), Profile(), Gateway(), Bridge(), prompt)
    assert prepared['stdin_text'] == prompt
    assert prepared['argv'][-1] == '-'
    assert sum(map(len, prepared['argv'])) < 1024


def test_compaction_threshold_is_left_to_pinned_codex_model_catalog():
    class StrictGateway(Gateway):
        def status(self):
            return {"strict_total_enforcement": True}

    supervisor = FakeSupervisor([])
    prepare_codex_run(supervisor, Profile(), StrictGateway(), Bridge(), "do work")
    config = supervisor.staged[1][1]["config.toml"].decode()
    assert "model_auto_compact_token_limit" not in config
    assert "model_auto_compact_token_limit_scope" not in config


def test_custom_gateway_model_gets_explicit_context_and_compaction_metadata():
    class CustomGateway(Gateway):
        model = "qwen3.8-flash"

    supervisor = FakeSupervisor([])
    prepare_codex_run(supervisor, Profile(), CustomGateway(), Bridge(), "do work")
    config = supervisor.staged[1][1]["config.toml"].decode()
    assert "model_context_window = 1000000" in config
    assert "model_auto_compact_token_limit = 800000" in config


def test_hy4_uses_provider_documented_context_capacity():
    class Hy4Gateway(Gateway):
        model = "hy4-preview"

    supervisor = FakeSupervisor([])
    prepare_codex_run(supervisor, Profile(), Hy4Gateway(), Bridge(), "do work")
    config = supervisor.staged[1][1]["config.toml"].decode()
    assert "model_context_window = 1000000" in config
    assert "model_auto_compact_token_limit = 800000" in config


def test_deepseek_uses_provider_documented_context_capacity():
    class DeepSeekGateway(Gateway):
        model = "deepseek-v4.1-flash"

    supervisor = FakeSupervisor([])
    prepare_codex_run(supervisor, Profile(), DeepSeekGateway(), Bridge(), "do work")
    config = supervisor.staged[1][1]["config.toml"].decode()
    assert "model_context_window = 1000000" in config
    assert "model_auto_compact_token_limit = 800000" in config


def test_run_reassembles_fragmented_jsonl_and_records_stderr():
    supervisor = FakeSupervisor([
        {"running": True, "exit_code": None, "events": [
            {"stream": "stdout", "data": '{"type":"turn.', "partial": True},
            {"stream": "stderr", "data": "warning\n", "partial": False},
        ]},
        {"running": False, "exit_code": 0, "events": [
            {"stream": "stdout", "data": 'completed","value":1}\n', "partial": False},
        ]},
    ])
    timeline = Timeline()
    outcome = run_codex(supervisor, Profile(), Gateway(), Bridge(), "work", timeline,
                        deadline=100, clock=lambda: 1, poll_interval=0)
    assert outcome["reason"] == "codex_exited"
    assert outcome["exit_code"] == 0 and outcome["explicit_finished"] is False
    assert outcome["event_count"] == 1 and "events" not in outcome
    codex = [row for row in timeline.rows if row["kind"] == "codex_event"]
    assert codex[0]["details"] == {
        "origin": "codex", "type": "turn.completed", "value": 1}
    stderr = [row for row in timeline.rows if row["kind"] == "codex_stderr"]
    assert stderr[0]["details"]["data"] == "warning\n"


def test_explicit_finish_revokes_gateway_and_terminates_without_claiming_exit_success():
    supervisor = FakeSupervisor([{"running": True, "exit_code": None, "events": []}])
    gateway, timeline = Gateway(), Timeline()
    outcome = run_codex(supervisor, Profile(), gateway, Bridge(), "work", timeline,
                        deadline=100, clock=lambda: 1, poll_interval=0,
                        finished=lambda: True)
    assert outcome["reason"] == "explicit_finished"
    assert outcome["explicit_finished"] is True
    assert outcome["exit_code"] is None
    assert gateway.revoked and supervisor.terminated == ["child"]


def test_explicit_finish_from_limit_crossing_response_wins_over_token_stop():
    class ExhaustedGateway(Gateway):
        def status(self):
            return {"revoked": True, "stop_reason": "token_budget_exhausted",
                    "budget_message": "tokens 101 / 100"}

    supervisor = FakeSupervisor([{"running": False, "exit_code": 1, "events": []}])
    gateway = ExhaustedGateway()
    checks = iter([False, True])
    outcome = run_codex(supervisor, Profile(), gateway, Bridge(), "work", Timeline(),
                        deadline=100, clock=lambda: 1, poll_interval=0,
                        finished=lambda: next(checks))
    assert outcome["reason"] == "explicit_finished"
    assert outcome["explicit_finished"] is True
    assert outcome["success"] is None



def test_deadline_and_output_overflow_terminate_as_failures():
    for poll, expected in [
        ({"running": True, "exit_code": None, "events": []}, "deadline"),
        ({"running": True, "exit_code": None, "events": [],
          "output_overflow": True, "fatal_error": "output_overflow"}, "output_overflow"),
    ]:
        supervisor = FakeSupervisor([poll])
        clock_values = iter([0, 2]) if expected == "deadline" else iter([0, 0])
        outcome = run_codex(supervisor, Profile(), Gateway(), Bridge(), "work", Timeline(),
                            deadline=1, clock=lambda: next(clock_values), poll_interval=0)
        assert outcome["reason"] == expected
        assert outcome["success"] is False
        assert supervisor.terminated == ["child"]


def test_invalid_stdout_json_is_preserved_as_diagnostic_failure():
    supervisor = FakeSupervisor([{"running": False, "exit_code": 1, "events": [
        {"stream": "stdout", "data": "not-json\n", "partial": False},
    ]}])
    timeline = Timeline()
    outcome = run_codex(supervisor, Profile(), Gateway(), Bridge(), "work", timeline,
                        deadline=10, clock=lambda: 0, poll_interval=0)
    assert outcome["reason"] == "invalid_jsonl"
    diagnostic = [row for row in timeline.rows if row["kind"] == "codex_diagnostic"]
    assert diagnostic[0]["details"]["data"] == "not-json"


def test_full_stdout_events_and_redacted_stderr_are_persisted(tmp_path):
    secret = Gateway.token
    supervisor = FakeSupervisor([{"running": False, "exit_code": 0, "events": [
        {"stream": "stdout", "data": json.dumps({"type": "message", "text": "x" * 5000}) + "\n",
         "partial": False},
        {"stream": "stderr", "data": f"header {secret}\n", "partial": False},
    ]}])
    timeline = Timeline(tmp_path / "timeline.jsonl")
    run_codex(supervisor, Profile(), Gateway(), Bridge(), "work", timeline,
              deadline=10, clock=lambda: 0, poll_interval=0)
    event = json.loads((tmp_path / "codex_events.jsonl").read_text())
    assert len(event["text"]) == 5000
    stderr = (tmp_path / "codex_stderr.log").read_text()
    assert "[REDACTED]" in stderr and secret not in stderr
    assert next(row for row in timeline.rows if row['kind'] == 'codex_event')["source"] == "sandbox"
    assert next(row for row in timeline.rows if row['kind'] == 'codex_event')["details"]["origin"] == "codex"


def test_exit_retains_budget_stop_reason_and_last_response(tmp_path):
    class BudgetGateway(Gateway):
        def status(self):
            return {'stop_reason':'token_budget_exhausted', 'budget_message':'tokens 25 / 20'}
    supervisor = FakeSupervisor([{'running':False,'exit_code':1,'events':[
        {'stream':'stdout','data':'{"type":"item.completed","text":"last answer"}\n'}]}])
    result = run_codex(supervisor,Profile(),BudgetGateway(),Bridge(),'work',Timeline(tmp_path/'timeline.jsonl'),
                       deadline=100,clock=lambda:1,poll_interval=0)
    assert result['reason'] == 'token_budget_exhausted' and result['success'] is False
    assert result['budget_message'] == 'tokens 25 / 20'
    assert 'last answer' in (tmp_path/'codex_events.jsonl').read_text()


def test_root_exit_waits_for_output_pumps_to_finish():
    supervisor = FakeSupervisor([
        {"running": False, "exit_code": 0, "output_complete": False,
         "events": [{"stream": "stdout", "data": '{"type":"late', "partial": True}]},
        {"running": False, "exit_code": 0, "output_complete": True,
         "events": [{"stream": "stdout", "data": '.event"}\n', "partial": False}]},
    ])
    outcome = run_codex(supervisor, Profile(), Gateway(), Bridge(), "work", Timeline(),
                        deadline=10, clock=lambda: 0, poll_interval=0)
    assert outcome["reason"] == "codex_exited"
    assert outcome["event_count"] == 1


def test_real_gateway_prepares_driver_without_contacting_upstream(tmp_path):
    from coding4game_gui.benchmark.code.model_gateway import ModelGateway

    upstream = lambda *_args: (_ for _ in ()).throw(AssertionError("must not call upstream"))
    with ModelGateway("https://provider.invalid/v1", "host-key", "real-model", 10, 60,
                      tmp_path / "gateway.jsonl", transport=upstream,
                      input_token_bound=lambda _request: 1) as gateway:
        supervisor = FakeSupervisor([])
        prepared = prepare_codex_run(supervisor, Profile(), gateway, Bridge(), "work")
        assert prepared["gateway"] == {
            "url": gateway.base_url, "token": gateway.token, "port": gateway.port}
        assert b'model = "real-model"' in supervisor.staged[1][1]["config.toml"]


def failed_poll(message, thread=True):
    events = ([{"type": "thread.started", "thread_id": "11111111-1111-4111-8111-111111111111"}]
              if thread else [])
    events.append({"type": "turn.failed", "error": {"message": message}})
    return {"running": False, "exit_code": 1, "events": [
        {"stream": "stdout", "data": "".join(json.dumps(e) + "\n" for e in events)}]}


def test_transient_exit_resumes_beyond_old_recovery_limit(monkeypatch):
    from coding4game_gui.benchmark.code.codex_driver import _RECOVERY_DELAYS
    monkeypatch.setattr('coding4game_gui.benchmark.code.codex_driver.time.sleep', lambda _: None)
    supervisor = FakeSupervisor([failed_poll('stream closed before response.completed')] * 12 + [
        {'running': False, 'exit_code': 0, 'events': []}])
    timeline = Timeline()
    result = run_codex(supervisor, Profile(), Gateway(), Bridge(), 'work', timeline,
                       deadline=1000, clock=lambda: 0, poll_interval=0)
    assert len(supervisor.launches) == 13
    assert len(supervisor.staged) == 2
    assert result['recovery_attempts'] == 12
    assert [r['details']['delay_seconds'] for r in timeline.rows if r['kind'] == 'codex_recovery'] == [30, 60, 120] + [300] * 9
    for argv, kwargs in supervisor.launches[1:]:
        assert 'resume' in argv and '11111111-1111-4111-8111-111111111111' in argv
        assert '--last' not in argv
        assert kwargs['stdin_text'] != 'work'


def test_upstream_rate_limit_waits_and_resumes_without_reset(monkeypatch):
    sleeps = []
    monkeypatch.setattr('coding4game_gui.benchmark.code.codex_driver.time.sleep', sleeps.append)
    supervisor = FakeSupervisor([
        failed_poll('rate limit exceeded: Upstream rate limit exceeded, please retry later'),
        {'running': False, 'exit_code': 0, 'events': []}])
    timeline = Timeline()
    result = run_codex(supervisor, Profile(), Gateway(), Bridge(), 'work', timeline,
                       deadline=1000, clock=lambda: 0, poll_interval=0)
    assert result['exit_code'] == 0 and result['recovery_attempts'] == 1
    assert sum(sleeps) == 30
    assert len(supervisor.staged) == 2
    assert 'resume' in supervisor.launches[1][0]
    recovery = [row for row in timeline.rows if row['kind'] == 'codex_recovery']
    assert recovery[0]['details']['delay_seconds'] == 30
    assert recovery[0]['details']['maximum'] is None


def test_rate_limit_wait_honors_deadline(monkeypatch):
    now = [0]
    monkeypatch.setattr('coding4game_gui.benchmark.code.codex_driver.time.sleep',
                        lambda seconds: now.__setitem__(0, now[0] + seconds))
    supervisor = FakeSupervisor([failed_poll('rate_limit_exceeded')])
    run_codex(supervisor, Profile(), Gateway(), Bridge(), 'work', Timeline(),
              deadline=5, clock=lambda: now[0], poll_interval=0)
    assert now[0] == 5
    assert len(supervisor.launches) == 1


def test_permanent_errors_and_missing_session_do_not_resume():
    for message, thread in [('403 Forbidden: token_budget_exhausted', True),
                            ('401 Unauthorized', True), ('cancelled by user', True),
                            ('stream closed before response.completed', False)]:
        supervisor = FakeSupervisor([failed_poll(message, thread)])
        run_codex(supervisor, Profile(), Gateway(), Bridge(), 'work', Timeline(),
                  deadline=1000, clock=lambda: 0, poll_interval=0)
        assert len(supervisor.launches) == 1


def test_revoked_gateway_prevents_transient_recovery():
    class StoppedGateway(Gateway):
        def status(self):
            return {'revoked': True, 'stop_reason': 'token_budget_exhausted'}
    supervisor = FakeSupervisor([failed_poll('stream closed before response.completed')])
    run_codex(supervisor, Profile(), StoppedGateway(), Bridge(), 'work', Timeline(),
              deadline=1000, clock=lambda: 0, poll_interval=0)
    assert len(supervisor.launches) == 1


def test_recovery_can_complete_without_restarting_task(monkeypatch):
    monkeypatch.setattr('coding4game_gui.benchmark.code.codex_driver.time.sleep', lambda _: None)
    supervisor = FakeSupervisor([failed_poll('Service temporarily unavailable'),
        {'running': False, 'exit_code': 0, 'events': [
            {'stream': 'stdout', 'data': '{"type":"turn.completed"}\n'}]}])
    result = run_codex(supervisor, Profile(), Gateway(), Bridge(), 'work', Timeline(),
                       deadline=1000, clock=lambda: 0, poll_interval=0)
    assert result['exit_code'] == 0 and result['recovery_attempts'] == 1
    assert not result['explicit_finished']


def test_unfinished_tool_without_host_state_reports_blocker(monkeypatch):
    monkeypatch.setattr("coding4game_gui.benchmark.code.codex_driver.time.sleep", lambda _: None)
    state = failed_poll('stream closed before response.completed')
    state['events'].insert(0, {'stream': 'stdout', 'data': json.dumps({
        'type': 'item.started', 'item': {'id': 'tool1', 'type': 'mcp_tool_call'}}) + '\n'})
    supervisor = FakeSupervisor([state])
    run_codex(supervisor, Profile(), Gateway(), Bridge(), 'work', Timeline(),
              deadline=1000, clock=lambda: 0, poll_interval=0)
    assert len(supervisor.launches) == 1


def test_deadline_during_backoff_prevents_relaunch(monkeypatch):
    ticks = [0]
    monkeypatch.setattr('coding4game_gui.benchmark.code.codex_driver.time.sleep',
                        lambda seconds: ticks.__setitem__(0, ticks[0] + seconds))
    supervisor = FakeSupervisor([failed_poll('stream closed before response.completed')])
    result = run_codex(supervisor, Profile(), Gateway(), Bridge(), 'work', Timeline(),
                       deadline=3, clock=lambda: ticks[0], poll_interval=0)
    assert len(supervisor.launches) == 1
    assert result['reason'] == 'deadline'


def test_large_replay_event_survives_fragmented_pipe():
    line = json.dumps({'type': 'item.completed', 'item': {'type': 'mcp_tool_call',
                      'result': {'content': [{'type': 'image', 'data': 'A' * 1100000}]}}}) + '\n'
    polls = [{'running': True, 'events': [{'stream': 'stdout', 'data': line[i:i+65537]}]}
             for i in range(0, len(line), 65537)]
    polls.append({'running': False, 'exit_code': 0, 'events': []})
    result = run_codex(FakeSupervisor(polls), Profile(), Gateway(), Bridge(), 'work',
                       Timeline(), deadline=100, clock=lambda: 0, poll_interval=0)
    assert result['reason'] == 'codex_exited'
    assert result['event_count'] == 1


def test_line_limit_applies_with_or_without_newline(monkeypatch):
    monkeypatch.setattr('coding4game_gui.benchmark.code.codex_driver._MAX_JSONL_LINE', 100)
    for ending in ('', '\n'):
        state = {'running': True, 'events': [{'stream': 'stdout',
                 'data': json.dumps({'text': 'x' * 101}) + ending}]}
        result = run_codex(FakeSupervisor([state]), Profile(), Gateway(), Bridge(), 'work',
                           Timeline(), deadline=100, clock=lambda: 0, poll_interval=0)
        assert result['reason'] == 'output_overflow'


def test_early_clean_exit_continues_but_stops_repeated_no_progress():
    state = failed_poll('unused')
    state['exit_code'] = 0
    state['events'][0]['data'] = json.dumps({'type': 'thread.started',
        'thread_id': '11111111-1111-4111-8111-111111111111'}) + '\n'
    supervisor = FakeSupervisor([state] * 3)
    result = run_codex(supervisor, Profile(), Gateway(), Bridge(), 'work', Timeline(),
                      deadline=1000, clock=lambda: 0, poll_interval=0,
                      tics_remaining=lambda: 100)
    assert len(supervisor.launches) == 3
    assert result['reason'] == 'development_no_progress'
    assert result['continuation_attempts'] == 2
    reminder = supervisor.launches[1][1]['stdin_text']
    assert 'no final program has been selected' in reminder
    assert '100 game tics remain available' in reminder
    assert 'Unused tics do not block final selection' in reminder


def test_clean_exit_after_tics_exhaustion_continues_for_explicit_selection():
    state = failed_poll('unused')
    state['exit_code'] = 0
    state['events'][0]['data'] = json.dumps({'type': 'thread.started',
        'thread_id': '11111111-1111-4111-8111-111111111111'}) + '\n'
    supervisor = FakeSupervisor([state] * 3)
    result = run_codex(supervisor, Profile(), Gateway(), Bridge(), 'work', Timeline(),
                      deadline=1000, clock=lambda: 0, poll_interval=0,
                      tics_remaining=lambda: 0)
    assert len(supervisor.launches) == 3
    assert result['reason'] == 'development_no_progress'
    assert '0 game tics remain available' in supervisor.launches[1][1]['stdin_text']
    assert not result['explicit_finished']


def test_gameplay_progress_resets_no_progress_counter():
    data = json.dumps({'type': 'thread.started',
        'thread_id': '11111111-1111-4111-8111-111111111111'}) + '\n'
    state = {'running': False, 'exit_code': 0, 'events': [{'stream':'stdout', 'data':data}]}
    supervisor = FakeSupervisor([state] * 9)
    remaining = iter([100, 100, 100, 50, 50, 50, 0, 0, 0, 0])
    result = run_codex(supervisor, Profile(), Gateway(), Bridge(), 'work', Timeline(),
                      deadline=1000, clock=lambda:0, poll_interval=0,
                      tics_remaining=lambda:next(remaining))
    assert result['continuation_attempts'] == 8
    assert result['reason'] == 'development_no_progress'


def test_budget_stop_blocks_clean_exit_continuation():
    class Stopped(Gateway):
        def status(self): return {'revoked':True, 'stop_reason':'token_budget_exhausted'}
    state = {'running':False,'exit_code':0,'events':[{'stream':'stdout','data':json.dumps({
        'type':'thread.started','thread_id':'11111111-1111-4111-8111-111111111111'})+'\n'}]}
    supervisor = FakeSupervisor([state])
    result = run_codex(supervisor, Profile(), Stopped(), Bridge(), 'work', Timeline(),
                      deadline=1000, clock=lambda:0, poll_interval=0, tics_remaining=lambda:100)
    assert len(supervisor.launches) == 1
    assert result['reason'] == 'token_budget_exhausted'


def test_pending_submission_waits_for_host_completion_before_resume(monkeypatch):
    import threading
    from coding4game_gui.benchmark.code.tool_bridge import ToolServer
    entered, release = threading.Event(), threading.Event()

    class Session:
        def dispatch(self, tool, arguments, phase):
            entered.set()
            assert release.wait(5)
            return {"done": True, "score": 0, "tics": 1200}

    bridge = ToolServer(Session())
    worker = threading.Thread(target=lambda: bridge.handle_call({
        "request_id": "rollout", "tool": "submission.run", "arguments": {"id": "saved"}}))
    worker.start()
    assert entered.wait(5)
    # Supply the launch URL without opening a listener.
    bridge._httpd = type("Address", (), {"server_address": ("127.0.0.1", 51002)})()
    state = failed_poll('Our servers are currently overloaded. Please try again later.')
    state['events'].insert(0, {'stream': 'stdout', 'data': json.dumps({
        'type': 'item.started', 'item': {'id': 'tool1', 'type': 'command_execution'}}) + '\n'})
    supervisor = FakeSupervisor([state, {'running': False, 'exit_code': 0, 'events': []}])
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        assert len(supervisor.launches) == 1
        if len(sleeps) == 31:
            release.set()
            worker.join(5)
            assert not worker.is_alive()

    monkeypatch.setattr('coding4game_gui.benchmark.code.codex_driver.time.sleep', sleep)
    try:
        result = run_codex(supervisor, Profile(), Gateway(), bridge, 'work', Timeline(),
                           deadline=1000, clock=lambda: 0, poll_interval=0)
        assert result['recovery_attempts'] == 1
        assert len(sleeps) == 31
        assert 'Read bench.status' in supervisor.launches[1][1]['stdin_text']
        assert len(supervisor.launches) == 2
    finally:
        release.set()
        worker.join(5)


def test_terminal_shows_redacted_api_failure_and_recovery(monkeypatch, capsys):
    monkeypatch.setattr('coding4game_gui.benchmark.code.codex_driver.time.sleep', lambda _: None)
    message = 'rate limit exceeded: ' + Gateway.token
    state = failed_poll(message)
    error = {'stream': 'stdout', 'data': json.dumps({'type': 'error', 'message': message}) + '\n'}
    state['events'].insert(0, error)
    supervisor = FakeSupervisor([state, {'running': False, 'exit_code': 0, 'events': [
        {'stream': 'stdout', 'data': '{"type":"turn.completed"}\n'}]}])
    run_codex(supervisor, Profile(), Gateway(), Bridge(), 'work', Timeline(),
              deadline=1000, clock=lambda: 0, poll_interval=0)
    output = capsys.readouterr().out
    assert output.count('rate limit exceeded: [REDACTED]') == 1
    assert Gateway.token not in output
    assert 'in 30s' in output
    assert 'Recovered: model turn completed.' in output


def test_codex_effort_is_staged_in_persistent_session_config():
    supervisor = FakeSupervisor([])
    prepare_codex_run(supervisor, Profile(), Gateway(), Bridge(), 'task', reasoning_effort='high')
    config = supervisor.staged[1][1]['config.toml'].decode()
    assert 'model_reasoning_effort = "high"' in config


def test_startup_failure_stops_codex_without_continuation():
    class FailedGateway(Gateway):
        def status(self):
            return {'stop_reason': 'game_startup_failed', 'revoked': True}
    supervisor = FakeSupervisor([])
    result = run_codex(supervisor, Profile(), FailedGateway(), Bridge(), 'task',
                       Timeline(), float('inf'), tics_remaining=lambda: 5949)
    assert result['reason'] == 'game_startup_failed'
    assert result['success'] is False
    assert result['continuation_attempts'] == 0
    assert supervisor.terminated == ['child']
