import json
import sys
from pathlib import Path

import pytest
from pathlib import PurePosixPath

from coding4game_gui.benchmark.code.agent_driver import prepare_agent_run
from coding4game_gui.benchmark.code.agent_driver import run_agent
from coding4game_gui.benchmark.code.agent_driver import _print_agent_event


class Supervisor:
    workspace = Path("C:/boxed/run")
    def __init__(self): self.staged = []
    def stage(self, files, name="artifact"):
        self.staged.append((name, files))
        return str(self.workspace / name)


class Profile:
    # This fixture tests protocol formatting, not support in a real runtime.
    backend = "protocol-fixture"
    python = Path("C:/runtime/python.exe")
    def agent(self, name): return Path(f"C:/agents/{name}/{name}.exe")


class Gateway:
    base_url = "http://127.0.0.1:51005"
    token = "g" * 43
    port = 51005
    model = "fixed-model"


class Bridge:
    observation_size = (768, 461)
    url = "http://127.0.0.1:51002/call"
    token = "b" * 64


@pytest.mark.parametrize("changed", [False, True])
def test_restored_claude_launch_uses_original_session_and_checks_identity(tmp_path,changed):
    session="11111111-1111-4111-8111-111111111111"
    observed="22222222-2222-4222-8222-222222222222" if changed else session
    class Runner(Supervisor):
        workspace=tmp_path/"box"
        def __init__(self): super().__init__();self.launches=[];self.terminated=[]
        def launch_agent(self,agent,argv,**options):
            self.launches.append((argv,options));return {"handle":"owned"}
        def poll(self,handle):
            return {"running":False,"exit_code":0,"events":[{"stream":"stdout","data":
                    json.dumps({"type":"system","subtype":"init","session_id":observed})+"\n"}]}
        def terminate(self,handle): self.terminated.append(handle)
    class Lease(Gateway):
        revoked=False
        def status(self): return {}
        def revoke(self,reason="session_closed"): self.revoked=True
    class Timeline:
        path=None
        def append(self,*a,**k): pass
    runner=Runner();gateway=Lease()
    if changed:
        with pytest.raises(RuntimeError,match="identity changed"):
            run_agent(runner,Profile(),gateway,Bridge(),"Continue preserved work",Timeline(),
                      float("inf"),agent="claude",poll_interval=0,resume_session_id=session)
        assert runner.terminated==["owned"] and gateway.revoked
    else:
        result=run_agent(runner,Profile(),gateway,Bridge(),"Continue preserved work",Timeline(),
                         float("inf"),agent="claude",poll_interval=0,resume_session_id=session)
        assert result["session_id"]==session and not result["explicit_finished"]
        assert result["recovery_identity_observed"] is True
    assert len(runner.launches)==1
    argv,options=runner.launches[0]
    assert argv[-2:]==["--resume",session]
    assert options["stdin_text"]=="Continue preserved work"


def test_claude_runtime_records_bounded_native_transport_configuration():
    from coding4game_gui.benchmark.code.run_codex import _agent_runtime
    metadata = _agent_runtime(Profile(), "claude")["model_request_timeouts"]
    environment = metadata["environment"]
    assert int(environment["BUN_CONFIG_HTTP_IDLE_TIMEOUT"]) == 30 * 60
    assert int(environment["API_TIMEOUT_MS"]) == 31 * 60 * 1000
    for name in ("CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS", "CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS",
                 "CLAUDE_STREAM_IDLE_TIMEOUT_MS"):
        assert int(environment[name]) == 30 * 60 * 1000
    assert metadata["scope"] == "official_development_cli_initial_and_same_session_resume"
    assert metadata["experiment_limits_unchanged"] is True


def test_agent_terminal_preview_survives_strict_legacy_encoding(monkeypatch):
    class StrictGbkStream:
        encoding = "gbk"
        def __init__(self):
            self.output = ""
        def write(self, value):
            value.encode(self.encoding, errors="strict")
            self.output += value
            return len(value)
        def flush(self): pass

    stream = StrictGbkStream()
    monkeypatch.setattr(sys, "stdout", stream)
    event = {"type": "assistant", "message": {"content": [{
        "type": "text", "text": "loss − 1 🎮",
    }]}}

    _print_agent_event("claude", event)

    assert stream.output == "[C4G claude] loss \\u2212 1 \\U0001f3ae\n"
    assert event["message"]["content"][0]["text"] == "loss − 1 🎮"


def test_claude_configuration_is_isolated_and_contains_no_lease_secret():
    supervisor = Supervisor()
    prepared = prepare_agent_run(supervisor, Profile(), Gateway(), Bridge(), "task",
                                 agent="claude")
    assert [name for name, _ in supervisor.staged] == ["mcp", ".claude"]
    files = supervisor.staged[-1][1]
    config = json.loads(files["mcp.json"])
    assert config["mcpServers"]["c4g_game"]["env"]["C4G_BRIDGE_TOKEN"] == "${C4G_BRIDGE_TOKEN}"
    assert Bridge.token not in files["mcp.json"].decode()
    assert prepared.stdin_text == "task"
    assert "--claude-viewport" in config["mcpServers"]["c4g_game"]["args"]
    assert "--bare" not in prepared.argv and "stream-json" in prepared.argv
    assert prepared.gateway["model"] == "fixed-model"
    assert set(config["mcpServers"]) == {"c4g_game"}
    for command in (prepared.argv, prepared.resume_prefix):
        assert "--disallowedTools" not in command
        assert command[command.index("--autocompact") + 1] == "auto"


def test_wsl_claude_uses_linux_paths_and_keeps_default_system_and_bash_tools():
    class WslSupervisor(Supervisor):
        workspace = PurePosixPath("/home/c4g/run")
        def stage(self, files, name="artifact"):
            self.staged.append((name, files))
            return f"{self.workspace}/{name}"
        def native_path(self, directory, *parts):
            return str(PurePosixPath(directory).joinpath(*parts))
    class WslProfile(Profile):
        backend = "wsl2"
        python = PurePosixPath("/usr/bin/python3")
        def agent(self, name): return PurePosixPath("/home/c4g/.local/bin/claude")
    supervisor = WslSupervisor()
    prepared = prepare_agent_run(supervisor, WslProfile(), Gateway(), Bridge(), "task",
                                 agent="claude")
    assert [name for name, _ in supervisor.staged] == ["mcp", ".claude"]
    assert "--bare" not in prepared.argv
    assert "--disallowedTools" not in prepared.argv
    assert "Bash,PowerShell" not in prepared.argv
    assert prepared.argv[prepared.argv.index("--name") + 1] == "Coding4Game development"
    assert prepared.resume_prefix[prepared.resume_prefix.index("--name") + 1] == "Coding4Game development"
    assert "/home/c4g/run/.claude/mcp.json" in prepared.argv
    config = json.loads(supervisor.staged[-1][1]["mcp.json"])
    assert config["mcpServers"]["c4g_game"]["command"] == "/usr/bin/python3"
    assert config["mcpServers"]["c4g_game"]["args"][4] == "/home/c4g/run/mcp/client.py"
    assert "c4g_dev" not in config["mcpServers"]


def test_unknown_agent_fails_closed():
    with pytest.raises(ValueError, match="unsupported"):
        prepare_agent_run(Supervisor(), Profile(), Gateway(), Bridge(), "task", agent="gemini")


def test_selected_agent_clean_exit_requests_explicit_selection_even_at_zero_tics(tmp_path):
    session="12345678-1234-1234-1234-123456789abc"
    line=lambda value: json.dumps(value)+"\n"
    class RunningSupervisor(Supervisor):
        workspace=tmp_path/"box"
        def __init__(self):
            super().__init__(); self.launches=[]; self.poll_count=0; self.polls=iter([
                {"running":False,"output_complete":True,"exit_code":0,"events":[
                    {"stream":"stdout","data":line({"type":"system","subtype":"init","session_id":session})+line({"type":"result","session_id":session})}]},
                {"running":False,"output_complete":True,"exit_code":0,"events":[
                    {"stream":"stdout","data":line({"type":"result","session_id":session})}]},
                {"running":False,"output_complete":True,"exit_code":0,"events":[
                    {"stream":"stdout","data":line({"type":"result","session_id":session})}]},
            ])
        def launch_agent(self,agent,argv,**kwargs):
            self.launches.append((agent,argv,kwargs)); return {"handle":"h"}
        def poll(self,handle):
            self.poll_count += 1
            return next(self.polls)
        def terminate(self,handle): pass
    class ActiveGateway(Gateway):
        def status(self): return {"revoked":False,"stop_reason":None}
        def revoke(self,reason="session_closed"): pass
    class Timeline:
        path=tmp_path/"timeline.jsonl"
        def __init__(self): self.rows=[]
        def append(self,kind,**kwargs): self.rows.append((kind,kwargs))
    remaining=iter([1,0,0])
    supervisor=RunningSupervisor()
    result=run_agent(supervisor,Profile(),ActiveGateway(),Bridge(),"task",Timeline(),
                     float("inf"),agent="claude",poll_interval=0,
                     tics_remaining=lambda:next(remaining),
                     finished=lambda:supervisor.poll_count >= 3)
    assert result["reason"]=="explicit_finished"
    assert result["continuation_attempts"]==2
    assert len(supervisor.launches)==3
    assert supervisor.launches[1][1][-1]==session
    reminder = supervisor.launches[1][2]["stdin_text"]
    assert "no final program has been selected" in reminder
    assert "0 game tics remain available" in reminder
    assert "Unused tics do not block final selection" in reminder


def test_selected_agent_finish_from_limit_crossing_response_wins_over_token_stop(
        tmp_path):
    session = "12345678-1234-1234-1234-123456789abc"
    line = lambda value: json.dumps(value) + "\n"

    class Runner(Supervisor):
        workspace = tmp_path / "box"
        def __init__(self):
            super().__init__()
            self.launches = []
            self.poll_count = 0
        def launch_agent(self, agent, argv, **kwargs):
            self.launches.append((agent, argv, kwargs))
            return {"handle": "child"}
        def poll(self, handle):
            self.poll_count += 1
            return {"running": False, "output_complete": True, "exit_code": 1,
                    "events": [{"stream": "stdout", "data": line({
                        "type": "system", "subtype": "init", "session_id": session})}]}
        def terminate(self, handle): pass

    class ExhaustedGateway(Gateway):
        def status(self):
            return {"revoked": True, "stop_reason": "token_budget_exhausted",
                    "budget_message": "tokens 101 / 100"}
        def revoke(self, reason="session_closed"): pass

    class Timeline:
        path = tmp_path / "timeline.jsonl"
        def append(self, *args, **kwargs): pass

    runner = Runner()
    result = run_agent(
        runner, Profile(), ExhaustedGateway(), Bridge(), "task", Timeline(),
        float("inf"), agent="claude", poll_interval=0,
        finished=lambda: runner.poll_count >= 1)
    assert result["reason"] == "explicit_finished"
    assert result["explicit_finished"] is True
    assert result["success"] is None


def test_selected_agent_token_stop_before_finish_does_not_continue(tmp_path):
    session = "12345678-1234-1234-1234-123456789abc"

    class Runner(Supervisor):
        workspace = tmp_path / "box"
        def __init__(self):
            super().__init__()
            self.launches = []
        def launch_agent(self, agent, argv, **kwargs):
            self.launches.append((agent, argv, kwargs))
            return {"handle": "child"}
        def poll(self, handle):
            return {"running": False, "output_complete": True, "exit_code": 0,
                    "events": [{"stream": "stdout", "data": json.dumps({
                        "type": "system", "subtype": "init",
                        "session_id": session}) + "\n"}]}
        def terminate(self, handle): pass

    class ExhaustedGateway(Gateway):
        def status(self):
            return {"revoked": True, "stop_reason": "token_budget_exhausted",
                    "budget_message": "tokens 100 / 100"}
        def revoke(self, reason="session_closed"): pass

    class Timeline:
        path = tmp_path / "timeline.jsonl"
        def append(self, *args, **kwargs): pass

    runner = Runner()
    result = run_agent(
        runner, Profile(), ExhaustedGateway(), Bridge(), "task", Timeline(),
        float("inf"), agent="claude", poll_interval=0,
        tics_remaining=lambda: 50)
    assert len(runner.launches) == 1
    assert result["reason"] == "token_budget_exhausted"
    assert result["success"] is False
    assert result["explicit_finished"] is False


def test_api_error_is_visible_and_retained(capsys):
    from types import SimpleNamespace
    message = 'API Error: 403 {"error":"余额不足"} ' + Gateway.token
    events = [
        {"type": "assistant", "is_api_error_message": True,
         "message": {"content": [{"type": "text", "text": message}]}},
        {"type": "result", "is_error": True, "result": message},
    ]
    class Runner(Supervisor):
        def launch_agent(self, *args, **kwargs): return {"handle": "child"}
        def poll(self, handle):
            return {"running": False, "exit_code": 1, "events": [{
                "stream": "stdout", "data": ''.join(json.dumps(e) + '\n' for e in events)}]}
    gateway = Gateway()
    gateway.status = lambda: {}
    result = run_agent(Runner(), Profile(), gateway, Bridge(), 'task',
                       SimpleNamespace(append=lambda *a, **kw: None), 100,
                       agent="claude", clock=lambda: 0, poll_interval=0)
    output = capsys.readouterr().out
    assert output.count('API Error: 403') == 1
    assert '余额不足' in output
    assert Gateway.token not in output
    assert '[REDACTED]' in result['error']


def test_claude_conversation_is_mirrored_without_binary_payloads(tmp_path, capsys):
    from types import SimpleNamespace
    session = "12345678-1234-1234-1234-123456789abc"
    events = [
        {"type": "system", "subtype": "init", "session_id": session},
        {"type": "assistant", "session_id": session, "message": {"content": [
            {"type": "text", "text": "I will write the player now."},
            {"type": "tool_use", "name": "mcp__c4g_dev__shell",
             "input": {"command": "echo " + Gateway.token}},
        ]}},
        {"type": "user", "session_id": session, "message": {"content": [
            {"type": "tool_result", "content": [
                {"type": "text", "text": "created player.py"},
                {"type": "image", "source": {"type": "base64",
                                                "media_type": "image/png",
                                                "data": "SECRET_BASE64"}},
            ]},
        ]}},
        {"type": "result", "session_id": session, "result": "done"},
    ]

    class Runner(Supervisor):
        workspace = tmp_path / "box"
        def launch_agent(self, *args, **kwargs): return {"handle": "child"}
        def poll(self, handle):
            return {"running": False, "output_complete": True, "exit_code": 0,
                    "events": [{"stream": "stdout",
                                "data": "".join(json.dumps(row) + "\n" for row in events)}]}
        def terminate(self, handle): pass

    gateway = Gateway()
    gateway.status = lambda: {"revoked": False, "stop_reason": None}
    gateway.revoke = lambda reason="session_closed": None
    run_agent(Runner(), Profile(), gateway, Bridge(), "task",
              SimpleNamespace(path=tmp_path / "timeline.jsonl",
                              append=lambda *args, **kwargs: None),
              float("inf"), agent="claude", poll_interval=0)
    output = capsys.readouterr().out
    assert "[C4G claude] I will write the player now." in output
    assert "[C4G claude tool] mcp__c4g_dev__shell" in output
    assert "[C4G tool result] created player.py" in output
    assert "binary image retained in evidence" in output
    assert "SECRET_BASE64" not in output
    assert Gateway.token not in output
    assert "[REDACTED]" in output


def test_claude_unknown_usage_502_uses_normal_transient_recovery(tmp_path, monkeypatch):
    from types import SimpleNamespace
    session = "12345678-1234-1234-1234-123456789abc"
    error = "API Error: 502 upstream_usage_unknown: billing outcome unknown"
    line = lambda value: json.dumps(value) + "\n"

    class Runner(Supervisor):
        workspace = tmp_path / "box"
        def __init__(self):
            super().__init__()
            self.launches = []
            self.polls = iter([
                {"running": False, "output_complete": True, "exit_code": 1,
                 "events": [{"stream": "stdout", "data":
                    line({"type": "system", "subtype": "init", "session_id": session}) +
                    line({"type": "assistant", "session_id": session,
                          "is_api_error_message": True,
                          "message": {"content": [{"type": "text", "text": error}]}}) +
                    line({"type": "result", "session_id": session,
                          "is_error": True, "result": error})}]},
                {"running": False, "output_complete": True, "exit_code": 0,
                 "events": [{"stream": "stdout", "data":
                    line({"type": "result", "session_id": session,
                          "is_error": False, "result": "recovered"})}]},
            ])
        def launch_agent(self, agent, argv, **kwargs):
            self.launches.append((agent, argv, kwargs))
            return {"handle": "child"}
        def poll(self, handle): return next(self.polls)
        def terminate(self, handle): pass

    class ActiveGateway(Gateway):
        def status(self): return {"revoked": False, "stop_reason": None}
        def revoke(self, reason="session_closed"): pass

    timeline = SimpleNamespace(path=tmp_path / "timeline.jsonl",
                               append=lambda *args, **kwargs: None)
    monkeypatch.setattr("coding4game_gui.benchmark.code.agent_driver.time.sleep",
                        lambda _seconds: None)
    runner = Runner()
    result = run_agent(runner, Profile(), ActiveGateway(), Bridge(), "task",
                       timeline, float("inf"), agent="claude", poll_interval=0)
    assert result["reason"] == "claude_exited" and result["recovery_attempts"] == 1
    assert len(runner.launches) == 2
    assert runner.launches[1][1][-1] == session
    assert "temporary transport error" in runner.launches[1][2]["stdin_text"]


def test_claude_effort_is_preserved_on_resume():
    prepared = prepare_agent_run(Supervisor(), Profile(), Gateway(), Bridge(), 'task',
                                 agent='claude', reasoning_effort='high')
    for command in (prepared.argv, prepared.resume_prefix):
        assert command[command.index('--effort') + 1] == 'high'
