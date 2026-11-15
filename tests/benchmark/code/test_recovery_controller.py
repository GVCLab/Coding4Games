import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile

import pytest

from coding4game_gui.benchmark.code.recovery_accounting import reconstruct_usage
from coding4game_gui.benchmark.code.recovery_controller import (
    load_recovery, verify_stopped, protocol_hash, prepare_append_logs,
)
from coding4game_gui.benchmark.code.run_codex import parser
from coding4game_gui.benchmark.code.submissions import ProgramStore
from coding4game_gui.wsl2 import WslProfile


SESSION = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("record", [{}, {"format": "c4g-run-agent-upgrade-v1"}])
def test_recovery_rejects_an_unbound_agent_upgrade(closed_run, record):
    args, profile, spec, root, config = closed_run
    config["agent_upgrade"] = record
    (root / "config.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="agent upgrade"):
        load_recovery(args, profile, spec)


@pytest.fixture
def closed_run(tmp_path):
    profile_path=tmp_path/"profile.json"
    profile_path.write_text(json.dumps({"format":"c4g-wsl2-v1","id":"0123456789ab",
        "root":str(tmp_path/"runtime-0123456789ab"),"baseline":str(tmp_path/"baseline.tar"),
        "baseline_sha256":"b"*64,"user":"c4gprobe","uid":1000,"python":"/usr/bin/python3",
        "agents":{"claude":"/opt/c4g-tools/claude"},"ports":dict(zip(
            ['development_control','development_game','final_test_control','final_test_game','gateway'],range(15001,15006)))}))
    profile=WslProfile.load(profile_path)
    root=tmp_path/"source_input-gamepad";root.mkdir();(root/"wsl2_exports").mkdir()
    workspace="run/"+"a"*24
    path=root/"wsl2_exports/original.tar"
    with tarfile.open(path,"w") as stream:
        for name in ["run",workspace,workspace+"/.claude",workspace+"/.claude/projects"]:
            entry=tarfile.TarInfo(name);entry.type=tarfile.DIRTYPE;entry.mode=0o700;stream.addfile(entry)
        history=json.dumps({"sessionId":SESSION,"message":{"content":"preserved user task"}}).encode()
        entry=tarfile.TarInfo(workspace+"/.claude/projects/"+SESSION+".jsonl");entry.size=len(history)
        stream.addfile(entry,io.BytesIO(history))
    descriptor={"role":"development","distro":"C4G-WSL2-Run-0123456789ab-development-abcdef123456",
        "path":str(path),"bytes":path.stat().st_size,"sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
        "profile_id":profile.id,"baseline_sha256":profile.baseline_sha256,"workspace":"/home/c4gprobe/"+workspace}
    records=[{"event":"request","request_id":"known"},{"event":"reservation","request_id":"known","token_accounting":"usage"},
        {"event":"usage","request_id":"known","usage":10,"charged_tokens":10,
         "token_breakdown":{"input_tokens":8,"cached_input_tokens":0,"uncached_input_tokens":8,"output_tokens":2}},
        {"event":"request","request_id":"ambiguous"},{"event":"reservation","request_id":"ambiguous","token_accounting":"usage"}]
    (root/"model_gateway.jsonl").write_text(''.join(json.dumps(x)+'\n' for x in records))
    (root/"claude_events.jsonl").write_text(json.dumps({"type":"system","subtype":"init","session_id":SESSION})+'\n')
    (root/"task_prompt.md").write_bytes(b"Original task\r\nwith unchanged original limits.\r\n")
    args=parser().parse_args(['develop','--profile',str(profile_path),'--scenario','balatro',
        '--out-dir',str(root),'--tic-budget','100','--max-tics','20','--wall-seconds','unlimited',
        '--model','fixture-model','--token-limit','1000','--upstream-url','http://127.0.0.1:1','--resume-run',str(root)])
    spec={'engine':'windows','runtime':{'max_tics':20,'max_action_tics':5}}
    config={"mode":"develop","runtime_backend":"wsl2","status":"failed","profile":str(profile_path),
        **{key:getattr(args,key) for key in ['scenario','windows_input','agent','model','token_limit','tic_budget','max_tics','wall_seconds','token_accounting','reasoning_effort','upstream_api','provider_model','upstream_url']},
        'model_proxy':{'mode':'system','url':None},'runtime_role':{'profile_id':profile.id,'baseline_sha256':profile.baseline_sha256,'distro':descriptor['distro']},
        'runtime_exports':[descriptor],'model_budget':reconstruct_usage(records),'game_budget':{'tics_used':7},
        'task_prompt_sha256':hashlib.sha256((root/'task_prompt.md').read_bytes()).hexdigest(),
        'scenario_protocol_sha256':protocol_hash(spec),'original_started_unix':100}
    (root/'config.json').write_text(json.dumps(config))
    return args,profile,spec,root,config


def test_closed_run_inspection_preserves_unknown_usage_original_files_and_session(closed_run):
    args,profile,spec,root,config=closed_run
    before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    first=load_recovery(args,profile,spec);second=load_recovery(args,profile,spec)
    assert first.usage==second.usage
    assert first.usage['tokens_reported']==10 and first.usage['unknown_usage_requests']==1
    assert first.session_id==SESSION and first.deadline==float('inf')
    assert first.evidence()['route_origin_recorded'] is True
    assert before=={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_recovery_rejects_a_nonboolean_http_400_stop_policy(closed_run):
    args, profile, spec, root, config = closed_run
    config["model_gateway_stop_on_http_400"] = "true"
    (root / "config.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="HTTP 400"):
        load_recovery(args, profile, spec)


def authorize_metadata_compatibility(root, config):
    source = root / "metadata-compatibility.before.json"
    raw = (root / "config.json").read_bytes()
    source.write_bytes(raw)
    config["model_gateway_compatibility"] = {
        "format": "c4g-run-anthropic-user-id-v1", "user_id_mode": "session_uuid",
        "session_id": SESSION, "run_dir": str(root), "source_config": source.name,
        "source_config_sha256": hashlib.sha256(raw).hexdigest(),
        "authorization_basis": "The user authorized repairing the native request while retaining this session and its original resources.",
    }
    (root / "config.json").write_text(json.dumps(config))


def authorize_tool_replay(root, config):
    source = root / "tool-replay.before.json"
    raw = (root / "config.json").read_bytes()
    source.write_bytes(raw)
    config["model_gateway_tool_replay"] = {
        "format": "c4g-run-anthropic-tool-replay-v1", "replay_mode": "restore_leading_cd",
        "session_id": SESSION, "run_dir": str(root), "source_config": source.name,
        "source_config_sha256": hashlib.sha256(raw).hexdigest(),
        "authorization_basis": "The user authorized preserving original Bash inputs and continuing this session.",
    }
    (root / "config.json").write_text(json.dumps(config))


def test_tool_replay_is_default_off_and_bound_to_the_original_session(closed_run):
    args, profile, spec, root, config = closed_run
    assert load_recovery(args, profile, spec).gateway_tool_replay is None
    authorize_metadata_compatibility(root, config)
    authorize_tool_replay(root, config)
    checked = load_recovery(args, profile, spec)
    assert checked.gateway_tool_replay == config["model_gateway_tool_replay"]
    assert checked.evidence()["tool_replay_provenance"]["calls"] == 0
    config["model_gateway_tool_replay"]["session_id"] = "00000000-0000-4000-8000-000000000000"
    (root / "config.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="Tool replay"):
        load_recovery(args, profile, spec)


@pytest.mark.parametrize("require_terminal", [True, False, "true"])
def test_explicit_terminal_review_notice_preserves_game_protocol_and_allowances(closed_run, tmp_path, require_terminal):
    args, profile, spec, root, config = closed_run
    raw = (root / "config.json").read_bytes()
    grant = {"kind": "c4g-recovery-budget-notice-v2", "run_dir": str(root),
             "source_config_sha256": hashlib.sha256(raw).hexdigest(),
             "minimum_development_fraction": .5, "authorization_basis": "The original user acceptance requirement.",
             "require_registered_terminal_evaluation": require_terminal}
    path = tmp_path / "terminal-review.json"
    path.write_text(json.dumps(grant))
    args.recovery_budget_notice = str(path)
    if require_terminal is not True:
        with pytest.raises(ValueError, match="explicit true"):
            load_recovery(args, profile, spec)
        return
    result = load_recovery(args, profile, spec)
    assert "terminal_detected=true" in result.prompt
    assert "does not require winning" in result.prompt
    assert result.usage["tokens_reported"] == 10 and result.usage["unknown_usage_requests"] == 1
    assert (root / "config.json").read_bytes() == raw
    assert result.config["scenario_protocol_sha256"] == protocol_hash(spec)


def test_tool_replay_sources_exclude_diagnostic_generations(closed_run):
    args, profile, spec, root, config = closed_run
    authorize_metadata_compatibility(root, config)
    authorize_tool_replay(root, config)
    native = {"type": "tool_use", "id": "same-id", "name": "Bash",
              "input": {"command": "cd /workspace && run"}}
    diagnostic = {**native, "input": {"command": "diagnostic-only"}}

    def records(request_id, call):
        body = json.dumps({"type": "message", "model": config["model"], "content": [call]}).encode()
        return [{"event": "request", "request_id": request_id},
                {"event": "reservation", "request_id": request_id, "token_accounting": "usage"},
                {"event": "attempt", "request_id": request_id, "upstream_status": 200},
                {"event": "response_chunk", "request_id": request_id, "data_hex": body.hex()},
                {"event": "usage", "request_id": request_id, "usage": 2, "charged_tokens": 2,
                 "token_breakdown": {"input_tokens": 1, "cached_input_tokens": 0,
                                     "uncached_input_tokens": 1, "output_tokens": 1}}]

    with (root / "model_gateway.jsonl").open("a") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in records("native", native))
    path = root / "operator_diagnostics" / "probe.jsonl"
    path.parent.mkdir()
    path.write_text("".join(json.dumps(row) + "\n" for row in records("diagnostic", diagnostic)))
    relative = path.relative_to(root).as_posix()
    config["model_gateway_logs"] = ["model_gateway.jsonl", relative]
    config["operator_diagnostic_gateway_logs"] = [{"relative_path": relative}]
    (root / "config.json").write_text(json.dumps(config))
    checked = load_recovery(args, profile, spec)
    assert checked.tool_replay_sources == [{"request_id": "native", "tool_use": native}]
    assert checked.usage["tokens_reported"] == 14
    assert checked.usage["unknown_usage_requests"] == 1
    assert "2 confirmed tokens" in checked.prompt
    assert "excluding diagnostics is 12 tokens" in checked.prompt
    assert "do not count toward the development review floor" in checked.prompt


def test_metadata_compatibility_is_bound_to_original_session_and_preserved_resources(closed_run):
    args, profile, spec, root, config = closed_run
    authorize_metadata_compatibility(root, config)
    result = load_recovery(args, profile, spec)
    assert result.gateway_compatibility == config["model_gateway_compatibility"]
    assert result.session_id == SESSION
    assert result.usage["tokens_reported"] == 10
    assert result.usage["unknown_usage_requests"] == 1
    assert result.evidence()["gateway_compatibility"] == result.gateway_compatibility


@pytest.mark.parametrize("field,value", [
    ("session_id", "22222222-2222-4222-8222-222222222222"),
    ("user_id_mode", "omit"), ("source_config", "../other.json"),
    ("source_config_sha256", "0" * 64), ("authorization_basis", ""),
])
def test_metadata_compatibility_rejects_mismatched_authorization(closed_run, field, value):
    args, profile, spec, root, config = closed_run
    authorize_metadata_compatibility(root, config)
    config["model_gateway_compatibility"][field] = value
    (root / "config.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="compatibility|evidence"):
        load_recovery(args, profile, spec)


@pytest.mark.parametrize("mutation", ["tokens", "unknown", "tics", "allowance", "route"])
def test_metadata_compatibility_never_grants_another_allowance_or_route(closed_run, mutation):
    args, profile, spec, root, config = closed_run
    authorize_metadata_compatibility(root, config)
    if mutation == "tokens":
        config["model_budget"]["tokens_reported"] = 0
    elif mutation == "unknown":
        config["model_budget"]["unknown_usage_requests"] = 0
    elif mutation == "tics":
        config["game_budget"]["tics_used"] = 0
    elif mutation == "allowance":
        args.token_limit = config["token_limit"] = 2000
    else:
        args.upstream_url = config["upstream_url"] = "https://other.invalid"
    (root / "config.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="compatibility"):
        load_recovery(args, profile, spec)


@pytest.mark.parametrize('field,value', [('tic_budget',101),('token_limit',2000),('model','another-model'),
    ('agent','codex'),('max_tics',21),('upstream_url','https://elsewhere.invalid'),('windows_input','gamepad-kbm')])
def test_no_model_protocol_or_allowance_replacement(closed_run,field,value):
    args,profile,spec,*_=closed_run;setattr(args,field,value)
    with pytest.raises(ValueError,match='original'):
        load_recovery(args,profile,spec)


def test_original_finite_deadline_is_not_extended(closed_run):
    args,profile,spec,root,config=closed_run
    config['wall_seconds']=args.wall_seconds=60;(root/'config.json').write_text(json.dumps(config))
    assert load_recovery(args,profile,spec,now_unix=150,monotonic=500).deadline==510
    with pytest.raises(ValueError,match='exhausted'):
        load_recovery(args,profile,spec,now_unix=160,monotonic=500)


def test_corrupt_task_or_changed_game_protocol_stops_before_restore(closed_run):
    args,profile,spec,root,_=closed_run
    with pytest.raises(ValueError,match='protocol changed'):
        load_recovery(args,profile,{'engine':'windows','runtime':{'max_tics':21}})
    (root/'task_prompt.md').write_text('replacement task')
    with pytest.raises(ValueError,match='task text changed'):
        load_recovery(args,profile,spec)


def test_selected_programme_is_not_restarted_as_development(closed_run):
    args,profile,spec,root,_=closed_run
    store=ProgramStore(root/'programs');program=store.save({'main.py':b'pass'},
        {'runtime':'python','mode':'stepped','argv':['main.py']},execution_platform='linux')
    store.finish(program['id'])
    with pytest.raises(ValueError,match='independent final tests'):
        load_recovery(args,profile,spec)


@pytest.fixture
def authorized_selection(closed_run, tmp_path):
    args, profile, spec, root, config = closed_run
    store = ProgramStore(root / 'programs')
    selected = store.save({'main.py': b'pass'},
        {'runtime': 'python', 'mode': 'stepped', 'argv': ['main.py']}, execution_platform='linux')
    store.finish(selected['id'])
    config.update(status='complete', final_program_id=selected['id'],
                  agent_result={'explicit_finished': True})
    (root / 'config.json').write_text(json.dumps(config))
    grant = {'kind': 'c4g-development-continuation-authorization-v1', 'run_dir': str(root),
             'source_config_sha256': hashlib.sha256((root / 'config.json').read_bytes()).hexdigest(),
             'selected_program_id': selected['id'],
             'selection_sha256': hashlib.sha256((root / 'programs/final.json').read_bytes()).hexdigest(),
             'authorization_basis': 'Human explicitly requested continuing the original development.'}
    path = tmp_path / 'continuation-grant.json'
    path.write_text(json.dumps(grant))
    args.development_continuation_authorization = str(path)
    return args, profile, spec, root, config, path, grant


def test_selected_continuation_preview_preserves_selection_and_all_original_resources(authorized_selection):
    args, profile, spec, root, _, _, grant = authorized_selection
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    result = load_recovery(args, profile, spec)
    assert result.selection_continuation['authorization'] == grant
    assert result.usage['tokens_reported'] == 10 and result.usage['unknown_usage_requests'] == 1
    assert result.session_id == SESSION and result.deadline == float('inf')
    assert 'does not itself require immediate finalization' in result.prompt
    assert 'No independent final-test results' in result.prompt
    assert before == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    args.development_continuation_authorization = None
    with pytest.raises(ValueError, match='independent final tests'):
        load_recovery(args, profile, spec)


@pytest.mark.parametrize('field,value', [
    ('source_config_sha256', '0' * 64), ('selected_program_id', '0' * 64),
    ('selection_sha256', '0' * 64), ('run_dir', '/other-run'),
    ('kind', 'another-kind'), ('authorization_basis', ''), ('unexpected', 'field')])
def test_selected_continuation_rejects_stale_or_unbound_grants(authorized_selection, field, value):
    args, profile, spec, root, _, path, grant = authorized_selection
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    grant[field] = value
    path.write_text(json.dumps(grant))
    with pytest.raises(ValueError, match='authorization'):
        load_recovery(args, profile, spec)
    assert before == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('field,value', [('tic_budget', 101), ('token_limit', 2000),
    ('wall_seconds', 10), ('model', 'another-model'), ('upstream_url', 'https://other.invalid')])
def test_selected_grant_does_not_authorize_budget_model_or_route_changes(authorized_selection, field, value):
    args, profile, spec, *_ = authorized_selection
    setattr(args, field, value)
    with pytest.raises(ValueError, match='original'):
        load_recovery(args, profile, spec)


@pytest.mark.parametrize('field,value', [('status', 'running'), ('status', 'failed'),
    ('agent_result', {'explicit_finished': False})])
def test_continuation_requires_a_proven_closed_selection(authorized_selection, field, value):
    args, profile, spec, root, config, *_ = authorized_selection
    config[field] = value
    (root / 'config.json').write_text(json.dumps(config))
    with pytest.raises(ValueError, match='closed explicitly selected'):
        load_recovery(args, profile, spec)


def test_selected_continuation_archives_pointer_before_worker_and_requires_new_selection(authorized_selection, monkeypatch):
    from contextlib import ExitStack
    from coding4game_gui.benchmark.code import run_codex as runner
    args, profile, spec, root, _, _, grant = authorized_selection
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-host-key')
    args.confirm_paid_run = True
    original = (root / 'config.json').read_bytes()
    pointer = (root / 'programs/final.json').read_bytes()
    captured = []

    class Runtime:
        archives = []
        native = SimpleNamespace(list_distros=lambda: [])
        def __init__(self, *a, **kw): pass
        def preflight(self, *a): return {'synthetic': True}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def start(self, role, recovery):
            current = json.loads((root / 'config.json').read_bytes())
            segment = root / current['recovery_segments'][-1]
            assert (segment / 'config.before.json').read_bytes() == original
            assert (segment / 'final.before.json').read_bytes() == pointer
            assert ProgramStore(root / 'programs').final() is None
            assert current['final_program_id'] is None and current['agent_result'] is None
            assert current['human_assisted'] is True
            assert current['selection_continuations'][-1]['authorization'] == grant
            assert current['game_budget']['tics_used'] == 7
            assert current['model_budget']['tokens_reported'] == 10
            assert current['model_budget']['unknown_usage_requests'] == 1
            captured.append(current)
            raise RuntimeError('fixture stops before any worker, model or game')

    deps = {'ExitStack': ExitStack, 'WslProfile': WslProfile, 'WslRuntime': Runtime,
            'load_game_spec': lambda *a, **kw: spec, 'REPO_ROOT': root.parent}
    with pytest.raises(RuntimeError, match='fixture stops'):
        runner.execute(args, deps)
    assert len(captured) == 1
    assert len(ProgramStore(root / 'programs').list()) == 1
    with pytest.raises(ValueError, match='closed explicitly selected'):
        load_recovery(args, profile, spec)
    args.development_continuation_authorization = None
    assert load_recovery(args, profile, spec).session_id == SESSION


def test_continuation_grant_change_under_lock_preserves_old_selection(authorized_selection, monkeypatch):
    from contextlib import ExitStack
    from coding4game_gui.benchmark.code import run_codex as runner
    args, profile, spec, root, _, path, grant = authorized_selection
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-host-key')
    args.confirm_paid_run = True
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}

    class Runtime:
        archives = []
        def __init__(self, *a, **kw): pass
        def preflight(self, *a): return {}
        def __enter__(self):
            path.write_text(json.dumps(grant, indent=2))
            return self
        def __exit__(self, *a): pass
        def start(self, *a, **kw): pytest.fail('worker must not start')

    deps = {'ExitStack': ExitStack, 'WslProfile': WslProfile, 'WslRuntime': Runtime,
            'load_game_spec': lambda *a, **kw: spec, 'REPO_ROOT': root.parent}
    with pytest.raises(ValueError, match='authorization changed'):
        runner.execute(args, deps)
    assert before == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('payload', [b'[]', b'\xff', b'{', b'x' * 16385])
def test_malformed_continuation_grant_never_withdraws_selection(authorized_selection, payload):
    args, profile, spec, root, _, path, _ = authorized_selection
    before = (root / 'programs/final.json').read_bytes()
    path.write_bytes(payload)
    with pytest.raises(ValueError, match='authorization'):
        load_recovery(args, profile, spec)
    assert (root / 'programs/final.json').read_bytes() == before


def test_budget_review_notice_preserves_usage_limits_and_original_task(closed_run, tmp_path):
    args, profile, spec, root, config = closed_run
    path = tmp_path / 'review.json'
    grant = {'kind': 'c4g-recovery-budget-notice-v1', 'run_dir': str(root),
             'source_config_sha256': hashlib.sha256((root / 'config.json').read_bytes()).hexdigest(),
             'minimum_development_fraction': .5, 'authorization_basis': 'Explicit human review instruction.'}
    path.write_text(json.dumps(grant)); args.recovery_budget_notice = str(path)
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    result = load_recovery(args, profile, spec)
    assert result.config == config and result.usage['tokens_reported'] == 10
    assert result.usage['unknown_usage_requests'] == 1 and result.session_id == SESSION
    assert result.deadline == float('inf') and '50 tics' in result.prompt and '500 tokens' in result.prompt
    assert result.evidence()['budget_notice']['authorization'] == grant
    assert before == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    args.minimum_development_fraction = .5
    with pytest.raises(ValueError, match='original minimum_development_fraction'):
        load_recovery(args, profile, spec)


@pytest.mark.parametrize('tamper_under_lock', [False, True])
def test_budget_notice_rechecked_and_snapshotted_before_worker(closed_run, tmp_path, monkeypatch, tamper_under_lock):
    from contextlib import ExitStack
    from coding4game_gui.benchmark.code import run_codex as runner
    args, profile, spec, root, _ = closed_run
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-host-key'); args.confirm_paid_run = True
    original = (root / 'config.json').read_bytes()
    path = tmp_path / 'review.json'
    grant = {'kind': 'c4g-recovery-budget-notice-v1', 'run_dir': str(root),
             'source_config_sha256': hashlib.sha256(original).hexdigest(),
             'minimum_development_fraction': .5, 'authorization_basis': 'Explicit human review instruction.'}
    path.write_text(json.dumps(grant)); args.recovery_budget_notice = str(path)
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    started = []

    class Runtime:
        archives = []
        native = SimpleNamespace(list_distros=lambda: [])
        def __init__(self, *a, **kw): pass
        def preflight(self, *a): return {}
        def __enter__(self):
            if tamper_under_lock: path.write_text(json.dumps(grant, indent=2))
            return self
        def __exit__(self, *a): pass
        def start(self, *a, **kw):
            current = json.loads((root / 'config.json').read_bytes())
            segment = root / current['recovery_segments'][-1]
            assert (segment / 'config.before.json').read_bytes() == original
            evidence = json.loads((segment / 'recovery.json').read_bytes())
            assert evidence['budget_notice']['authorization'] == grant
            assert current['minimum_development_fraction'] == .5
            assert current['human_assisted'] is True
            assert current['token_limit'] == 1000 and current['tic_budget'] == 100
            assert current['game_budget']['tics_used'] == 7
            assert current['model_budget']['unknown_usage_requests'] == 1
            assert current['agent_runtime'] == runner._agent_runtime(profile, args.agent)
            started.append(current)
            raise RuntimeError('fixture stops before any worker, game or model')

    deps = {'ExitStack': ExitStack, 'WslProfile': WslProfile, 'WslRuntime': Runtime,
            'load_game_spec': lambda *a, **kw: spec, 'REPO_ROOT': root.parent}
    if tamper_under_lock:
        with pytest.raises(ValueError, match='notice changed'):
            runner.execute(args, deps)
        assert not started
        assert before == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    else:
        with pytest.raises(RuntimeError, match='fixture stops'):
            runner.execute(args, deps)
        assert len(started) == 1
        args.recovery_budget_notice = None; args.minimum_development_fraction = .5
        again = load_recovery(args, profile, spec)
        assert '50 tics' in again.prompt and again.session_id == SESSION
        assert again.config['budget_notice_updates'][-1]['authorization'] == grant


def test_live_owner_or_retained_clone_is_refused_without_stopping_it(closed_run):
    args,profile,spec,root,config=closed_run
    class Missing(Exception): pass
    module=SimpleNamespace(NoSuchProcess=Missing,Process=lambda pid:SimpleNamespace(create_time=lambda:1),process_iter=lambda fields:[])
    recovery=load_recovery(args,profile,spec)
    native=SimpleNamespace(list_distros=lambda:[recovery.descriptor['distro']])
    with pytest.raises(ValueError,match='still registered'):
        verify_stopped(recovery,native,process_module=module)
    recovery.config['host_owner']={'pid':100000000,'created':1}
    with pytest.raises(ValueError,match='owner is still live'):
        verify_stopped(recovery,SimpleNamespace(list_distros=lambda:[]),process_module=module)


def test_recovery_append_separators_preserve_partial_history_and_leave_gateway_immutable(tmp_path):
    raw=b'{"seq":1}\n{"seq":2'
    (tmp_path/'development_timeline.jsonl').write_bytes(raw)
    (tmp_path/'model_gateway.jsonl').write_bytes(raw)
    result=prepare_append_logs(tmp_path,'claude')
    assert (tmp_path/'development_timeline.jsonl').read_bytes()==raw+b'\n'
    assert result[0]['sha256_before']==hashlib.sha256(raw).hexdigest()
    assert (tmp_path/'model_gateway.jsonl').read_bytes()==raw
    assert prepare_append_logs(tmp_path,'claude')==[]


def test_public_recovery_execution_remains_gated_before_acceptance(closed_run, monkeypatch):
    from coding4game_gui.benchmark.code import run_codex as runner
    args,*_=closed_run
    monkeypatch.setattr(runner,'RECOVERY_ACCEPTANCE_COMPLETE',False)
    monkeypatch.setattr(runner,'_dependencies',lambda:pytest.fail('runtime graph constructed before acceptance'))
    with pytest.raises(ValueError,match='gated'):
        runner.execute(args)


@pytest.fixture
def authorized_route(closed_run, tmp_path):
    args, profile, spec, root, config = closed_run
    config['upstream_url'] = 'https://old.example.invalid/codex/v1'
    (root / 'config.json').write_text(json.dumps(config))
    grant = {
        'kind': 'c4g-recovery-route-authorization-v1', 'run_dir': str(root),
        'source_config_sha256': hashlib.sha256((root / 'config.json').read_bytes()).hexdigest(),
        'from_upstream_url': config['upstream_url'],
        'to_upstream_url': 'https://new.example.invalid/codex/v1',
        **{key: config[key] for key in ('agent', 'model', 'provider_model', 'upstream_api')},
        'authorization_basis': 'Operator explicitly approved this hostname migration.'}
    path = tmp_path / 'host-authorization.json'
    path.write_text(json.dumps(grant))
    args.upstream_url = grant['to_upstream_url']
    args.recovery_route_authorization = str(path)
    return args, profile, spec, root, config, path, grant


@pytest.fixture
def authorized_endpoint(closed_run, tmp_path):
    args, profile, spec, root, config = closed_run
    config["upstream_url"] = "https://provider.invalid/claude-aws"
    (root / "config.json").write_text(json.dumps(config))
    authorize_metadata_compatibility(root, config)
    authorize_tool_replay(root, config)
    receipt = {"http_status": 200, "generation_accepted": True, "complete_stream_verified": True,
               "stream_errors": [], "serving_model": config["model"], "original_run": str(root),
               "original_session": SESSION, "upstream_endpoint": "https://provider.invalid/v1/messages",
               "native_history_modified": False}
    receipt_path = root / "accepted-request.json"
    receipt_path.write_text(json.dumps(receipt))
    grant = {
        "kind": "c4g-recovery-endpoint-authorization-v1", "run_dir": str(root),
        "source_config_sha256": hashlib.sha256((root / "config.json").read_bytes()).hexdigest(),
        "from_upstream_url": config["upstream_url"], "to_upstream_url": "https://provider.invalid",
        **{key: config[key] for key in ("agent", "model", "provider_model", "upstream_api")},
        "authorization_basis": "The user authorized the accepted same-provider endpoint for original-session recovery.",
        "accepted_request_receipt": receipt_path.name,
        "accepted_request_receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
    }
    path = tmp_path / "endpoint-authorization.json"
    path.write_text(json.dumps(grant))
    args.upstream_url = grant["to_upstream_url"]
    args.recovery_route_authorization = str(path)
    return args, profile, spec, root, config, path, grant


def test_same_origin_endpoint_grant_requires_accepted_original_session_evidence(authorized_endpoint):
    args, profile, spec, root, config, path, grant = authorized_endpoint
    checked = load_recovery(args, profile, spec)
    assert checked.route_migration["authorization"] == grant
    assert checked.config["upstream_url"] == grant["from_upstream_url"]
    assert checked.session_id == SESSION and checked.usage["unknown_usage_requests"] == 1
    receipt_path = root / grant["accepted_request_receipt"]
    receipt = json.loads(receipt_path.read_bytes())
    receipt["generation_accepted"] = False
    receipt_path.write_text(json.dumps(receipt))
    grant["accepted_request_receipt_sha256"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    path.write_text(json.dumps(grant))
    with pytest.raises(ValueError, match="accepted complete-request"):
        load_recovery(args, profile, spec)


@pytest.mark.parametrize("target", [
    "https://other.invalid", "https://provider.invalid:444",
    "http://provider.invalid", "https://provider.invalid/claude-aws",
    "https://provider.invalid/../other", "https://provider.invalid?key=secret",
])
def test_endpoint_grant_cannot_change_hostname_port_or_bypass_path_guards(authorized_endpoint, target):
    args, profile, spec, root, config, path, grant = authorized_endpoint
    grant["to_upstream_url"] = args.upstream_url = target
    path.write_text(json.dumps(grant))
    with pytest.raises(ValueError):
        load_recovery(args, profile, spec)


def preserve_migration(authorized_endpoint):
    args, profile, spec, root, config, path, grant = authorized_endpoint
    checked = load_recovery(args, profile, spec)
    segment = root / "recovery/segments/verified-migration"
    segment.mkdir(parents=True)
    (segment / "config.before.json").write_bytes((root / "config.json").read_bytes())
    (segment / "route-authorization.json").write_bytes(path.read_bytes())
    (segment / "recovery.json").write_text(json.dumps(checked.evidence()))
    config["original_upstream_url"] = config["upstream_url"]
    config["upstream_url"] = args.upstream_url
    config["upstream_route_migrations"] = [
        checked.route_migration | {"segment": segment.relative_to(root).as_posix()}]
    (root / "config.json").write_text(json.dumps(config))
    args.recovery_route_authorization = None
    return segment


def test_later_recovery_accepts_only_verified_route_lineage_for_existing_repairs(authorized_endpoint):
    args, profile, spec, root, config, path, grant = authorized_endpoint
    preserve_migration(authorized_endpoint)
    checked = load_recovery(args, profile, spec)
    assert checked.gateway_compatibility == config["model_gateway_compatibility"]
    assert checked.gateway_tool_replay == config["model_gateway_tool_replay"]
    assert checked.config["upstream_url"] == grant["to_upstream_url"]
    assert checked.usage["tokens_reported"] == 10 and checked.usage["unknown_usage_requests"] == 1


@pytest.mark.parametrize("file", ["config.before.json", "route-authorization.json", "recovery.json"])
def test_edited_migration_history_cannot_bypass_original_identity_guards(authorized_endpoint, file):
    args, profile, spec, root, config, path, grant = authorized_endpoint
    segment = preserve_migration(authorized_endpoint)
    (segment / file).write_text("{}")
    with pytest.raises((ValueError, KeyError)):
        load_recovery(args, profile, spec)


def test_explicit_hostname_migration_preserves_closed_run_session_usage_and_bytes(authorized_route):
    args, profile, spec, root, config, path, grant = authorized_route
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    result = load_recovery(args, profile, spec)
    assert result.route_migration['authorization'] == grant
    assert result.evidence()['route_migration']['authorization_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result.config['upstream_url'] == grant['from_upstream_url']
    assert result.session_id == SESSION and result.deadline == float('inf')
    assert result.usage['tokens_reported'] == 10 and result.usage['unknown_usage_requests'] == 1
    assert before == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    args.recovery_route_authorization = None
    with pytest.raises(ValueError, match='without explicit authorization'):
        load_recovery(args, profile, spec)


@pytest.mark.parametrize('field,value', [
    ('source_config_sha256', '0' * 64), ('run_dir', '/other-run'),
    ('agent', 'codex'), ('model', 'another-model'), ('provider_model', 'another/provider'),
    ('upstream_api', 'chat'), ('from_upstream_url', 'https://other.invalid/codex/v1'),
    ('to_upstream_url', 'https://other.invalid/codex/v1'), ('authorization_basis', ''),
    ('unexpected', 'must not be copied into evidence')])
def test_migration_grant_cannot_authorize_other_configuration_or_route(authorized_route, field, value):
    args, profile, spec, root, _, path, grant = authorized_route
    before = (root / 'config.json').read_bytes()
    grant[field] = value
    path.write_text(json.dumps(grant))
    with pytest.raises(ValueError, match='authorization'):
        load_recovery(args, profile, spec)
    assert (root / 'config.json').read_bytes() == before


@pytest.mark.parametrize('destination', [
    'http://new.example.invalid/codex/v1', 'https://new.example.invalid/another/v1',
    'https://new.example.invalid:8443/codex/v1', 'https://u:p@new.example.invalid/codex/v1',
    'https://new.example.invalid/codex/v1?token=secret', 'https://new.example.invalid/codex/v1#fragment'])
def test_even_matching_grant_cannot_change_path_port_or_transport(authorized_route, destination):
    args, profile, spec, _, _, path, grant = authorized_route
    args.upstream_url = grant['to_upstream_url'] = destination
    path.write_text(json.dumps(grant))
    with pytest.raises(ValueError, match='HTTPS'):
        load_recovery(args, profile, spec)


@pytest.mark.parametrize('payload', [b'[]', b'\xff', b'{', b'x' * 16385])
def test_malformed_or_oversized_grant_never_restores(authorized_route, payload):
    args, profile, spec, _, _, path, _ = authorized_route
    path.write_bytes(payload)
    with pytest.raises(ValueError, match='authorization'):
        load_recovery(args, profile, spec)


def test_migration_execution_records_original_snapshot_and_grant_before_new_runtime(authorized_route, monkeypatch):
    from contextlib import ExitStack
    from coding4game_gui.benchmark.code import run_codex as runner
    args, profile, spec, root, _, path, grant = authorized_route
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-host-key')
    args.confirm_paid_run = True
    original = (root / 'config.json').read_bytes()
    captured = []

    class Runtime:
        archives = []
        native = SimpleNamespace(list_distros=lambda: [])
        def __init__(self, *a, **kw): pass
        def preflight(self, *a): return {'synthetic': True}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def start(self, role, recovery):
            captured.append(json.loads((root / 'config.json').read_bytes()))
            raise RuntimeError('fixture stops before any worker, model or game')

    deps = {'ExitStack': ExitStack, 'WslProfile': WslProfile, 'WslRuntime': Runtime,
            'load_game_spec': lambda *a, **kw: spec, 'REPO_ROOT': root.parent}
    with pytest.raises(RuntimeError, match='fixture stops'):
        runner.execute(args, deps)
    assert len(captured) == 1
    current = captured[0]
    assert current['original_upstream_url'] == grant['from_upstream_url']
    assert current['upstream_url'] == grant['to_upstream_url']
    segment = root / current['recovery_segments'][-1]
    assert (segment / 'config.before.json').read_bytes() == original
    record = json.loads((segment / 'recovery.json').read_text())
    assert record['route_migration']['authorization'] == grant
    assert record['usage_checkpoint']['unknown_usage_requests'] == 1
    assert current['upstream_route_migrations'][0]['segment'] == str(segment.relative_to(root)).replace('\\', '/')
    assert current['model_budget']['tokens_reported'] == 10
    assert current['game_budget']['tics_used'] == 7


def test_route_grant_change_under_profile_lock_is_refused_before_any_write(authorized_route, monkeypatch):
    from contextlib import ExitStack
    from coding4game_gui.benchmark.code import run_codex as runner
    args, profile, spec, root, _, path, grant = authorized_route
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-host-key')
    args.confirm_paid_run = True
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}

    class Runtime:
        archives = []
        def __init__(self, *a, **kw): pass
        def preflight(self, *a): return {}
        def __enter__(self):
            path.write_text(json.dumps(grant, indent=2))
            return self
        def __exit__(self, *a): pass
        def start(self, *a, **kw): pytest.fail('worker must not start')

    deps = {'ExitStack': ExitStack, 'WslProfile': WslProfile, 'WslRuntime': Runtime,
            'load_game_spec': lambda *a, **kw: spec, 'REPO_ROOT': root.parent}
    with pytest.raises(ValueError, match='authorization changed'):
        runner.execute(args, deps)
    assert before == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
