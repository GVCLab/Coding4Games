import json
import hashlib
from pathlib import Path
import pytest
from contextlib import ExitStack


def test_settings_failure_precedes_agent_and_sandbox_start(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture')
    parsed = args(tmp_path)
    parsed.confirm_paid_run = True
    parsed.agent = 'claude'
    profile = SimpleNamespace(
        codex=tmp_path / 'codex.exe',
        agent=lambda name: tmp_path / f'{name}.exe')

    class Runtime:
        def __init__(self, *a, **k): pass
        def preflight(self, *a): return {'ok': True}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def start(self, role): pytest.fail('WSL clone started before settings passed')

    @contextmanager
    def broken_settings(*a):
        raise RuntimeError('fixture settings failure')
        yield

    deps = {'WslProfile': SimpleNamespace(load=lambda _: profile),
            'load_game_spec': lambda *a, **k: {'engine': 'windows'},
            'WslRuntime': Runtime, 'REPO_ROOT': tmp_path,
            'ExitStack': ExitStack, 'benchmark_settings': broken_settings}
    with pytest.raises(RuntimeError, match='fixture settings failure'):
        runner.execute(parsed, deps)
    result = json.loads((tmp_path / 'out_input-gamepad/config.json').read_text())
    assert result['status'] == 'failed'
    assert result['observation_size'] == [768, 461]
from coding4game_gui.benchmark.code import run_codex as runner
from types import SimpleNamespace

def args(tmp_path,mode="develop",execute=False):
    base=[mode,"--profile",str(tmp_path/"p.json"),"--scenario","balatro","--out-dir",str(tmp_path/"out"),"--tic-budget","10","--wall-seconds","5"]
    if mode=="develop": base += ["--model","m","--token-limit","20","--upstream-url","https://api.example/v1"]
    else: base += ["--source-programs",str(tmp_path/"programs"),"--program-id","a"*64]
    if execute: base.append("--execute")
    return runner.parser().parse_args(base)

def test_preview_has_no_files_or_dependency_construction(tmp_path,monkeypatch):
    monkeypatch.setattr(runner,"_dependencies",lambda:pytest.fail("preview imported runtime"))
    result=runner.main(["develop","--profile",str(tmp_path/"p"),"--scenario","balatro","--out-dir",str(tmp_path/"out"),"--tic-budget","1","--wall-seconds","1","--model","m","--token-limit","1","--upstream-url","https://example/v1"])
    assert result["execute"] is False and not (tmp_path/"out").exists()
    assert result["token_accounting"] == "usage"
    assert result["context_compaction"] == {
        "enabled": True,
        "mechanism": "claude_code_autocompact_auto",
        "absolute_trigger_tokens": None,
        "context_fraction_trigger": None,
        "scope": "model_tuned_auto",
        "disabled_reason": None,
    }
    assert result["tic_budget_policy"] == "hard_upper_bound_unused_allowed"
    assert result["finalization_warning_fraction"] == .7
    assert result["finalization_warning_delivery"] == "next_admitted_model_request_once"
    assert "may exceed" in result["cost_estimate"]
    assert "after 70% confirmed usage" in result["cost_estimate"]
    assert "30% remaining" in result["cost_estimate"]


@pytest.mark.parametrize("enabled", [False, True])
def test_http_400_forwarding_policy_is_recorded_only_when_enabled(tmp_path, enabled):
    parsed = args(tmp_path)
    parsed.stop_on_http_400 = enabled
    plan = runner._plan(parsed)
    assert plan.get("model_gateway_stop_on_http_400", False) is enabled


def test_resume_cannot_override_http_400_policy_before_runtime_entry(tmp_path, monkeypatch):
    parsed = args(tmp_path)
    parsed.resume_run = str(tmp_path / "closed_run")
    parsed.stop_on_http_400 = True
    monkeypatch.setattr(runner, "_dependencies", lambda: pytest.fail("runtime constructed"))
    with pytest.raises(ValueError, match="retains the original HTTP 400"):
        runner.execute(parsed)


def test_task_instructions_preview_snapshots_descriptor_without_disclosing_text(tmp_path):
    instruction = tmp_path / "acceptance.md"
    raw = "Inspect the known panel.\n保留 UTF-8。".encode("utf-8")
    instruction.write_bytes(raw)
    parsed = args(tmp_path)
    parsed.task_instructions_file = str(instruction)
    plan = runner._plan(parsed)
    assert plan["task_instructions"] == {
        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    assert "Inspect the known panel" not in json.dumps(plan)
    instruction.write_text("changed after snapshot", encoding="utf-8")
    snapshot = runner._task_instructions_snapshot(parsed)
    assert snapshot["text"].encode("utf-8") == raw
    assert runner._append_task_instructions("base", snapshot) == (
        "base\n\n## Additional operator instructions\n\n" + raw.decode("utf-8"))


def test_omitted_task_instructions_preserve_prompt_and_plan(tmp_path):
    parsed = args(tmp_path)
    assert runner._task_instructions_snapshot(parsed) is None
    assert runner._append_task_instructions("unchanged", None) == "unchanged"
    assert "task_instructions" not in runner._plan(parsed)


@pytest.mark.parametrize("payload,error", [
    (b"x" * (64 * 1024 + 1), "exceed 65536"),
    (b"\xff", "valid UTF-8"),
], ids=["too-large", "invalid-utf8"])
def test_invalid_task_instructions_fail_before_runtime_dependencies(
        tmp_path, monkeypatch, payload, error):
    instruction = tmp_path / "bad.md"
    instruction.write_bytes(payload)
    parsed = args(tmp_path, execute=True)
    parsed.confirm_paid_run = True
    parsed.task_instructions_file = str(instruction)
    monkeypatch.setattr(runner, "_dependencies",
                        lambda: pytest.fail("runtime dependencies constructed"))
    with pytest.raises(ValueError, match=error):
        runner.execute(parsed)


def test_task_instructions_reject_non_regular_file(tmp_path):
    with pytest.raises(ValueError, match="regular file"):
        runner.read_task_instructions_file(tmp_path)

def test_develop_execute_requires_separate_paid_confirmation(tmp_path):
    with pytest.raises(ValueError,match="confirm-paid-run"): runner.execute(args(tmp_path,execute=True),deps={})


def test_wsl_preflight_is_persisted_before_runtime_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "host-key")
    parsed=args(tmp_path,execute=True); parsed.confirm_paid_run=True
    parsed.runtime_backend="wsl2"; parsed.agent="claude"
    snapshots=[]
    profile=SimpleNamespace(backend="wsl2",execution_platform="linux",id="profile",
                            python=Path("/usr/bin/python3"),agent=lambda name:Path("/opt/claude"))
    class Runtime:
        def __init__(self,*a,**k): pass
        def preflight(self,*a): return {"probe":"verified-before-entry"}
        def __enter__(self):
            snapshots.append(json.loads((tmp_path/"out_input-gamepad"/"config.json").read_text()))
            raise RuntimeError("entry failed")
        def __exit__(self,*a): pass
    deps={"ExitStack":ExitStack,"WslProfile":SimpleNamespace(load=lambda _:profile),
          "WslRuntime":Runtime,"load_game_spec":lambda *a,**k:{"engine":"windows"},
          "REPO_ROOT":tmp_path/"repo"}
    with pytest.raises(RuntimeError,match="entry failed"): runner.execute(parsed,deps)
    assert snapshots[0]["status"]=="running"
    assert snapshots[0]["runtime_preflight"]=={"probe":"verified-before-entry"}


def test_wsl_role_evidence_is_persisted_before_gateway_and_driver(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "host-key")
    parsed=args(tmp_path,execute=True); parsed.confirm_paid_run=True
    parsed.runtime_backend="wsl2"; parsed.agent="claude"
    snapshots=[]; supervisor=object()
    profile=SimpleNamespace(backend="wsl2",execution_platform="linux",id="profile",
                            python=Path("/usr/bin/python3"),ports={"development_game":51002,"gateway":51005},
                            agent=lambda name:Path("/opt/claude"))
    role_evidence={"role":"development","distro":"run-fixture","profile_id":"profile",
        "baseline_sha256":"a"*64,"network_namespace":"net:[1]",
        "network_topology":"shared_wsl_network_namespace",
        "isolation":{"uid":23456,"network_canary":"development_private_ranges_filtered",
                     "dependencies":{"cv2":"4.10.0","numpy":"2.2.4"}}}
    class Runtime:
        archives=[]
        def __init__(self,*a,**k): pass
        def preflight(self,*a): return {"probe":"verified"}
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def start(self,role): return supervisor
        def role_evidence(self,role): return {"role": role}
        def role_evidence(self,role): return role_evidence
    class FailingGame:
        def __init__(self,*a,**k):
            snapshot=json.loads((tmp_path/"out_input-gamepad"/"config.json").read_text())
            snapshots.append(snapshot)
            assert snapshot["runtime_preflight"]=={"probe":"verified"}
            assert snapshot["runtime_role"]==role_evidence
            raw=json.dumps(snapshot)
            assert "host-key" not in raw and "gateway-secret" not in raw
            raise RuntimeError("game setup failed")
    class Context:
        def __init__(self,*a,**k): pass
        def __enter__(self): return self
        def __exit__(self,*a): pass
    deps={"ExitStack":ExitStack,"WslProfile":SimpleNamespace(load=lambda _:profile),
          "WslRuntime":Runtime,"load_game_spec":lambda *a,**k:{"engine":"windows"},
          "make_bench_env":lambda *a,**k:object(),"GameSession":FailingGame,"ToolServer":Context,
          "DevelopmentTimeline":lambda *a,**k:object(),"ProgramStore":lambda *a,**k:object(),
          "OpenSession":lambda *a,**k:object(),"ModelGateway":lambda *a,**k:pytest.fail("gateway must not start"),
          "run_agent":lambda *a,**k:pytest.fail("driver must not run"),"REPO_ROOT":tmp_path/"repo"}
    with pytest.raises(RuntimeError,match="game setup failed"): runner.execute(parsed,deps)
    assert len(snapshots)==1


def test_atomic_config_write_preserves_previous_json_when_replace_fails(tmp_path, monkeypatch):
    path=tmp_path/"config.json"; path.write_text('{"old": true}',encoding="utf-8")
    monkeypatch.setattr(runner.os,"replace",lambda *a:(_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError,match="replace failed"): runner._write_config(path,{"new":True})
    assert json.loads(path.read_text(encoding="utf-8"))=={"old":True}
    assert list(tmp_path.glob(".config.json.*.tmp"))==[]

def test_test_parser_has_no_gateway_or_codex_options(tmp_path):
    value=args(tmp_path,"test")
    assert not hasattr(value,"model") and not hasattr(value,"confirm_paid_run")
    assert not hasattr(value,"token_accounting")
    assert not hasattr(value,"task_instructions_file")


def test_strict_mode_remains_explicit_in_preview(tmp_path):
    value = args(tmp_path)
    value.token_accounting = "strict"
    plan = runner._plan(value)
    assert plan["token_accounting"] == "strict"
    assert plan["context_compaction"] == {
        "enabled": True,
        "mechanism": "claude_code_autocompact_auto",
        "absolute_trigger_tokens": None,
        "context_fraction_trigger": None,
        "scope": "model_tuned_auto",
        "disabled_reason": None,
    }
    assert "strict prospective" in plan["cost_estimate"]




@pytest.mark.parametrize("agent,mechanism,absolute,fraction,scope", [
    ("claude", "claude_code_autocompact_auto", None, None, "model_tuned_auto"),
])
def test_non_codex_compaction_is_explicit_in_preview(
        tmp_path, agent, mechanism, absolute, fraction, scope):
    value = args(tmp_path)
    value.agent = agent
    plan = runner._plan(value)
    assert plan["context_compaction"] == {
        "enabled": True,
        "mechanism": mechanism,
        "absolute_trigger_tokens": absolute,
        "context_fraction_trigger": fraction,
        "scope": scope,
        "disabled_reason": None,
    }



def test_unlimited_plan_discloses_missing_usage_continuation(tmp_path):
    value = args(tmp_path)
    value.token_limit = None
    plan = runner._plan(value)
    assert plan['unknown_usage_policy'] == 'unlimited_warn_continue'
    assert 'recorded usage may be incomplete' in plan['cost_estimate']
    prompt = runner._public_prompt({}, token_limit=None)
    assert 'missing usage warns without stopping' in prompt
    assert 'a second unknown-usage request' not in prompt
    value.token_limit = 20
    plan = runner._plan(value)
    assert plan['unknown_usage_policy'] == 'warn_continue'
    assert 'recovery may continue' in plan['cost_estimate']


def test_public_prompt_excludes_infrastructure_fields():
    text=runner._public_prompt({"title":"T","gameplay":"G","objective":"O","episode_contract":"E","runtime":{"max_tics":7,"secret":1},"startup":{"launch_path":"secret"},"scorer":{"ocr":1}})
    assert "launch_path" not in text and "scorer" not in text and 'capped at 7 tics' in text
    assert "cannot prove" in text and "must not use any LLM" in text


def test_public_prompt_forwards_wsl_runtime_environment():
    text = runner._public_prompt({}, agent_name='claude', runtime_backend='wsl2')
    assert 'run-specific unprivileged Linux user `c4gprobe`' in text
    assert 'Linux uid 1000' not in text
    assert 'does not preinstall' in text

def test_outcome_classification_does_not_claim_missing_final_or_infrastructure_success():
    assert runner._classify("develop",driver={"success":True},selected=None)==("incomplete",False)
    assert runner._classify("develop",driver={"success":False},selected={"id":"x"})==("failed",False)
    assert runner._classify("test",result={"complete":False,"outcome_kind":"infrastructure_error"})==("failed",False)
    assert runner._classify("test",result={"complete":True,"budget_overshoot_tics":1})==("failed",False)
    assert runner._classify("test",result={"complete":True,"mode":"stepped","budget_overshoot_tics":1})==("complete",True)
    assert runner._classify("test",result={"complete":True,"mode":"stepped","budget_overshoot_tics":2})==("complete",True)
    assert runner._classify("test",result={"complete":True,"mode":"stepped","budget_overshoot_tics":3})==("failed",False)


def test_final_classification_rejects_program_error_but_accepts_host_termination_cleanup_code():
    actual_failure = {
        'complete': True, 'program_returncode': 1, 'tics': 0, 'score': 0,
        'end_reason': 'program_error', 'budget_overshoot_tics': 0,
    }
    assert runner._classify('test', result=actual_failure) == ('failed', False)
    # A valid gameplay endpoint must not hide a later Python assertion failure.
    after_budget_failure = actual_failure | {
        'end_reason': 'tic_budget', 'outcome_kind': 'valid', 'tics': 100,
    }
    assert runner._classify('test', result=after_budget_failure) == ('failed', False)
    for end_reason in ('game_over', 'max_tics (100) reached'):
        controlled = actual_failure | {
            'end_reason': end_reason, 'program_returncode': -15, 'tics': 100,
        }
        assert runner._classify('test', result=controlled) == ('complete', True)




def test_classification_accepts_actual_driver_explicit_finish_contract():
    outcome = {"reason": "explicit_finished", "success": None,
               "explicit_finished": True, "exit_code": None}
    assert runner._classify("develop", driver=outcome,
                            selected={"id": "selected"}) == ("complete", True)
    assert runner._classify("develop", driver=outcome, selected=None) == ("incomplete", False)
    assert runner._classify("develop", driver={"reason": "codex_exited", "success": None,
        "explicit_finished": False, "exit_code": 0}, selected=None) == ("incomplete", False)
    assert runner._classify("develop", driver={"reason": "codex_exited", "success": None,
        "explicit_finished": False, "exit_code": 1}, selected=None) == ("failed", False)

def test_credential_roots_are_selected_without_reading_files(tmp_path,monkeypatch):
    monkeypatch.setenv("CODEX_HOME",str(tmp_path/"auth")); monkeypatch.setenv("USERPROFILE",str(tmp_path/"user"))
    value=args(tmp_path,"test"); value.credential_root=[str(tmp_path/"operator-secret")]
    assert runner._credential_roots(value)==[tmp_path/"operator-secret",tmp_path/"auth",
        tmp_path/"user"/".codex",tmp_path/"user"/".claude",tmp_path/"user"/".gemini"]

@pytest.mark.parametrize("mode, size", [
    ("gamepad", None), ("gamepad-kbm", (768, 461)), ("gamepad", (1000, 1000)),
])
def test_final_test_copies_selected_program_and_never_constructs_gateway(tmp_path, mode, size):
    from coding4game_gui.benchmark.code.submissions import ProgramStore
    source=tmp_path/"programs"; saved=ProgramStore(source,max_bytes=runner.MAX_PROGRAM_BYTES,max_files=runner.MAX_PROGRAM_FILES).save(
        {"main.py":b"print('ok')"},{"argv":["main.py"],"mode":"stepped","runtime":"python"},
        observation_size=size, execution_platform="linux")
    (tmp_path/"config.json").write_text(json.dumps({"human_assisted":True}))
    parsed=args(tmp_path,"test",execute=True); parsed.source_programs=str(source); parsed.program_id=saved["id"]
    parsed.windows_input = mode
    events=[]
    class Profile:
        python=tmp_path/"python.exe"; ports={"final_test_game":51004}
        @classmethod
        def load(cls,path): return cls()
    class Runtime:
        def __init__(self,p,**kwargs): pass
        def preflight(self,p): return {"ok":True}
        def __enter__(self): events.append("runtime_enter"); return self
        def __exit__(self,*x): events.append("runtime_exit")
        def start(self,role): events.append("start_"+role); return object()
        def role_evidence(self,role): return {"role": role}
    class Context:
        def __init__(self,*a,**k): pass
        def __enter__(self): return self
        def __exit__(self,*x): pass
    class Session:
        finished=False
        def __init__(self,*a,**k): pass
        def bind_bridge(self,b): pass
        def run_program(self,id,max_tics=None): events.append("run_"+id); return {"score":1}
        def close(self): events.append("session_close")
    class Timeline:
        def __init__(self,*a,**k): pass
        def render_html(self,*a,**k): events.append("report")
    deps={"ExitStack":ExitStack,"WslProfile":Profile,"WslRuntime":Runtime,"GameSession":Context,
          "ToolServer":Context,"ProgramStore":ProgramStore,"DevelopmentTimeline":Timeline,
          "OpenSession":Session,"ModelGateway":lambda *a,**k:pytest.fail("final created gateway"),
          "run_codex":lambda *a,**k:pytest.fail("final launched Codex"),"load_game_spec":lambda *a,**k:{"engine":"windows"},
          "make_bench_env":lambda *a,**k:object(),"REPO_ROOT":tmp_path/"repo"}
    result=runner.execute(parsed,deps)
    assert result["program_id"]==saved["id"] and events[1]=="start_final_test"
    assert result["observation_size"] == list(size or (1280, 768))
    assert result["human_assisted"] is True and result["autonomous_benchmark_eligible"] is False
    assert (tmp_path/f"out_input-{mode}"/"programs"/saved["id"]/"manifest.json").is_file()

@pytest.mark.parametrize("agent, accounting, review_policy", [
    ("claude", "usage", None), ("claude", "strict", .5),
    ("codex", "usage", .5), ("codex", "strict", None),
])
def test_develop_whole_path_uses_real_protocol_components(tmp_path,monkeypatch,agent,accounting,review_policy):
    import numpy as np
    from coding4game_gui.runtime_client.client import Client
    from coding4game_gui.benchmark.windows.session import GameSession
    from coding4game_gui.benchmark.code.open_session import OpenSession
    from coding4game_gui.benchmark.code.submissions import ProgramStore
    from coding4game_gui.benchmark.evidence.timeline import DevelopmentTimeline
    from coding4game_gui.benchmark.code.tool_bridge import ToolServer
    monkeypatch.setenv("FAKE_RUNNER_KEY","host-secret")
    events=[]
    instruction = tmp_path / "acceptance.md"
    instruction.write_bytes(b"Exercise the run-specific capability.\n")
    class Env:
        TICRATE=10
        def __init__(self): self.tic=0;self.total_reward=0.;self.done=False;self.end_reason=None
        def reset(self,*a): pass
        def frame(self): return np.zeros((8,8,3),dtype=np.uint8)
        def window_info(self): return {"client_width":8,"client_height":8}
        def validate_action_plan(self,*a): pass
        def act(self,b,t): self.tic+=t;self.total_reward+=1

        def close(self): pass
    class Profile:
        execution_platform="linux"
        python=tmp_path/"python.exe"; codex=tmp_path/"codex.exe"; ports={"development_game":0,"gateway":50005}
        def agent(self,name): return self.codex if name=="codex" else tmp_path/f"{name}.exe"
        @classmethod
        def load(cls,p): return cls()
    class Supervisor:
        workspace=tmp_path/"boxed"; source={"main.py":b"print('program')"}
        def read_tree(self,path): return dict(self.source)
        def freeze_dependencies(self,files,dependencies,deadline): return dependencies or {}
        def prepare_program(self,record,directory,deadline): return {"python": str(tmp_path/"python.exe"), "env": {}, "verified": True}
        def stage(self,files,name="artifact"):
            path=self.workspace/name;path.mkdir(parents=True,exist_ok=True)
            for n,data in files.items(): (path/n).write_bytes(data)
            return str(path)
        def launch(self,*a,**k): events.append("program_launch");return {"handle":"h"}
        def poll(self,*a,**k): return {"running":False,"returncode":0,"exit_code":0,"events":[],"output_complete":True}
        def terminate(self,*a): pass
    supervisor=Supervisor()
    class Runtime:
        def __init__(self,p,**kwargs): pass
        def preflight(self,p): return {"ok":True}
        def __enter__(self): return self
        def __exit__(self,*x): pass
        def start(self,role): return supervisor
        def role_evidence(self,role): return {"role": role}
    class Gateway:
        token="gateway-secret";base_url="http://127.0.0.1:50005/v1";port=50005;model="m"
        def __init__(self,*a,**k):
            assert k["strict_total_enforcement"] is (accounting == 'strict')
            assert k.get("stop_on_http_400", False) is parsed.stop_on_http_400
            assert k["count_input"] is (accounting == 'strict')
            assert k["protocol"] == {"codex":"responses","claude":"anthropic"}[agent]
            events.append("gateway_created")
        def __enter__(self): return self
        def __exit__(self,*x): pass
        def revoke(self, reason="session_closed"): events.append("revoked")
        def status(self): return {"tokens_remaining":10}
    def drive(supervisor,profile,gateway,bridge,prompt,timeline,deadline,finished,**kwargs):
        assert prompt.endswith(
            "## Additional operator instructions\n\nExercise the run-specific capability.\n")
        if review_policy is not None:
            assert 'confirmed model-token usage reaches 10 tokens' in prompt
            assert 'cumulative development gameplay reaches 5 tics' in prompt
        else:
            assert 'Human-approved development review policy' not in prompt
        assert bridge.observation_size == ((768, 461) if agent == "claude" else (1280, 768))
        if agent == "claude":
            assert "1280x768" not in prompt
            assert "x=0..1279" not in prompt
        client=Client(bridge.url,bridge.token)
        record=client.call("submission.save",path="submission",entrypoint={"argv":["main.py"],"mode":"stepped","runtime":"python"})
        assert record.get("observation_size", [1280, 768]) == list(bridge.observation_size)
        client.call("submission.run",id=record["id"],max_tics=9)
        assert client.call('bench.status')['tics_remaining'] > 0
        assert not finished(); client.call("submission.finish",id=record["id"]); assert finished()
        timeline.append("driver",details={"gateway":gateway.token,"bridge":bridge.token})
        return {"reason":"explicit_finished","success":None,"explicit_finished":True}
    parsed=args(tmp_path,"develop",execute=True);parsed.confirm_paid_run=True;parsed.api_key_env="FAKE_RUNNER_KEY";parsed.max_tics=9
    parsed.task_instructions_file = str(instruction)
    parsed.minimum_development_fraction = review_policy
    parsed.stop_on_http_400 = review_policy is not None
    parsed.agent=agent
    parsed.token_accounting=accounting
    deps={"ExitStack":ExitStack,"WslProfile":Profile,"WslRuntime":Runtime,"GameSession":GameSession,"ToolServer":ToolServer,
          "ProgramStore":ProgramStore,"DevelopmentTimeline":DevelopmentTimeline,"OpenSession":OpenSession,"ModelGateway":Gateway,
          "run_codex":drive,"run_codex_debug":drive,"run_agent":drive,"run_agent_debug":drive,"load_game_spec":lambda *a,**k:{"engine":"windows","runtime":{"max_tics":3,"tics_per_second":10},"action_catalog":[{"name":"NEUTRAL","description":"Release controls"}]},
          "make_bench_env":lambda *a,**k:Env(),"REPO_ROOT":tmp_path/"repo"}
    result=runner.execute(parsed,deps); out=tmp_path/"out_input-gamepad"
    assert result["resolved_max_tics"]==3 and result["final_program_id"] and "program_launch" in events
    assert result["game_budget"]["tics_used"] < result["game_budget"]["tics_budget"]
    assert result["game_budget"]["tics_remaining"] > 0
    assert result["status"] == "complete"
    assert result["result_valid"] is True
    assert result["human_assisted"] is False
    assert result["autonomous_benchmark_eligible"] is False
    assert result["model_budget"] == {"tokens_remaining":10}
    assert result.get('minimum_development_fraction') == review_policy
    raw_instruction = instruction.read_bytes()
    assert result["task_instructions"] == {
        "sha256": hashlib.sha256(raw_instruction).hexdigest(),
        "bytes": len(raw_instruction),
    }
    assert (out / "task_prompt.md").read_text(encoding="utf-8").endswith(
        "## Additional operator instructions\n\nExercise the run-specific capability.\n")
    assert not (out/"holdout").exists()
    report=(out/"development_timeline.html").read_text(encoding="utf-8")
    assert "gateway-secret" not in report and "host-secret" not in report and "[REDACTED]" in report
