"""Explicit preview-first runner for the WSL2 development and independent final tests."""
from __future__ import annotations
import argparse, hashlib, json, os, stat, time
from pathlib import Path
import uuid
from coding4game_gui.catalog import FORMAL_SCENARIOS
from coding4game_gui.benchmark.windows.timing import STEP_BUDGET_TOLERANCE_TICS

MAX_PROGRAM_BYTES = 64 * 1024 * 1024
MAX_PROGRAM_FILES = 4096
MAX_TASK_INSTRUCTIONS_BYTES = 64 * 1024
RECOVERY_ACCEPTANCE_COMPLETE = True


def read_task_instructions_file(value):
    """Return an immutable text/descriptor snapshot of one public task file."""
    path = Path(value).resolve()
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("task instructions must be a regular file")
        if metadata.st_size > MAX_TASK_INSTRUCTIONS_BYTES:
            raise ValueError("task instructions exceed 65536 bytes")
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"task instructions are not readable: {exc}") from exc
    if len(raw) > MAX_TASK_INSTRUCTIONS_BYTES:
        raise ValueError("task instructions exceed 65536 bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("task instructions must be valid UTF-8") from exc
    return {
        "text": text,
        "descriptor": {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)},
    }


def _task_instructions_snapshot(args):
    """Read optional operator instructions once, before runtime side effects."""
    if args.command != "develop":
        return None
    cached = getattr(args, "_task_instructions_snapshot", None)
    if cached is not None:
        return cached
    value = getattr(args, "task_instructions_file", None)
    if value is None:
        return None
    snapshot = read_task_instructions_file(value)
    setattr(args, "_task_instructions_snapshot", snapshot)
    return snapshot


def _append_task_instructions(prompt, snapshot):
    if snapshot is None:
        return prompt
    return prompt + "\n\n## Additional operator instructions\n\n" + snapshot["text"]


def _write_config(path, config):
    """Atomically persist public run state without exposing a partial JSON file."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(config, stream, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

def _out_dir(args):
    path=Path(args.out_dir).resolve(); suffix=f"_input-{args.windows_input}"
    return path if path.name.endswith(suffix) else path.with_name(path.name+suffix)

def _positive(value, name):
    if type(value) is not int or value <= 0: raise ValueError(f"{name} must be positive")
    return value


def _agent_runtime(profile, agent, upgrade=None):
    result = {"kind": agent, "protocol": {"claude": "anthropic", "codex": "responses"}[agent], "backend": "wsl2",
              "executable": str(profile.agent(agent)), "manifest": None}
    if agent == "codex":
        from .codex_driver import CODEX_EXEC_YIELD_MS
        result["tool_wait"] = {"code_mode_host": True,
                               "default_exec_yield_time_ms": CODEX_EXEC_YIELD_MS,
                               "mcp_tool_timeout_seconds": 3600,
                               "experiment_deadlines_unchanged": True}
    elif agent == "claude":
        from coding4game_gui.wsl2.supervisor import CLAUDE_TRANSPORT_ENV
        result["model_request_timeouts"] = {
            "environment": dict(CLAUDE_TRANSPORT_ENV),
            "scope": "official_development_cli_initial_and_same_session_resume",
            "experiment_limits_unchanged": True,
        }
        if upgrade is not None:
            result["configured_version"] = upgrade.record["version"]
            result["manifest"] = upgrade.record
    return result


def _optional_limit(value):
    if value.lower() == 'unlimited':
        return None
    try:
        return _positive(int(value), 'limit')
    except ValueError as exc:
        raise argparse.ArgumentTypeError('use a positive integer or unlimited') from exc

def _credential_roots(args):
    roots=[]
    for value in list(getattr(args,"credential_root",[]) or []): roots.append(Path(value).resolve())
    for key in ("CODEX_HOME","USERPROFILE","HOME"):
        value=os.environ.get(key)
        if value:
            base=Path(value).resolve(); roots.append(base if key=="CODEX_HOME" else base/".codex")
            if key != "CODEX_HOME":
                roots.extend((base/".claude", base/".gemini"))
    for key in ("CLAUDE_CONFIG_DIR",):
        if os.environ.get(key): roots.append(Path(os.environ[key]).resolve())
    unique=[]
    for path in roots:
        if path not in unique: unique.append(path)
    return unique


def _context_compaction_plan(agent, token_accounting, model=None):
    if agent == "codex":
        from coding4game_gui.benchmark.code.codex_driver import _GATEWAY_MODEL_METADATA
        metadata = _GATEWAY_MODEL_METADATA.get(model)
        enabled = token_accounting != "strict"
        return {
            "enabled": enabled,
            "mechanism": ("codex_cli_explicit_auto_compact" if enabled and metadata else
                          "codex_cli_model_catalog_auto" if enabled else None),
            "absolute_trigger_tokens": metadata["compact_at"] if enabled and metadata else None,
            "context_fraction_trigger": None,
            "scope": ("gateway_model_override" if enabled and metadata else
                      "pinned_model_catalog_default" if enabled else None),
            "disabled_reason": None if enabled else "strict_accounting_cannot_reserve_compaction_output",
        }
    if agent != "claude":
        raise ValueError("WSL2 development supports Claude Code and Codex only")
    return {"enabled": True, "mechanism": "claude_code_autocompact_auto",
            "absolute_trigger_tokens": None, "context_fraction_trigger": None,
            "scope": "model_tuned_auto", "disabled_reason": None}


def _validate_runtime(args):
    if getattr(args, "runtime_backend", "wsl2") != "wsl2":
        raise ValueError("Only the WSL2 runtime is supported")
    if getattr(args, "resume_checkpoint", None):
        raise ValueError("WSL2 development resume is not implemented")
    if getattr(args, "resume_run", None) and args.command != "develop":
        raise ValueError("Independent final tests cannot restore development state")
    if getattr(args, "recovery_route_authorization", None) and not getattr(args, "resume_run", None):
        raise ValueError("Recovery route authorization requires --resume-run")
    if getattr(args, "development_continuation_authorization", None) and not getattr(args, "resume_run", None):
        raise ValueError("Development continuation authorization requires --resume-run")
    if getattr(args, "recovery_budget_notice", None) and not getattr(args, "resume_run", None):
        raise ValueError("Recovery budget notice requires --resume-run")
    from .development_policy import review_fraction
    review_fraction(getattr(args, "minimum_development_fraction", None))
    if getattr(args, "interactive_debug", False):
        raise ValueError("Visible development debug shells are unsupported by WSL2")
    if args.command == "develop" and getattr(args, "agent", "claude") not in {"claude", "codex"}:
        raise ValueError("WSL2 development supports Claude Code and Codex only")
    if args.command == "develop":
        from urllib.parse import urlsplit
        route = urlsplit(args.upstream_url)
        if (route.scheme not in {"http", "https"} or not route.hostname or route.username
                or route.password or route.query or route.fragment):
            raise ValueError("Upstream route must be an HTTP(S) URL without embedded credentials, query or fragment")


def _plan(args):
    _validate_runtime(args)
    from coding4game_gui.benchmark.code.agent_driver import validate_effort
    validate_effort(getattr(args, "agent", "claude"), getattr(args, "reasoning_effort", None))
    from coding4game_gui.benchmark.code.model_gateway import validate_proxy
    proxy = (validate_proxy(getattr(args, 'model_proxy_mode', 'system'),
                            getattr(args, 'model_proxy_url', None))
             if args.command == 'develop' else None)
    backend = getattr(args, "runtime_backend", "wsl2")
    task_instructions = None if getattr(args, "resume_run", None) else _task_instructions_snapshot(args)
    side_effects = [
        "create one clean WSL clone; export its runtime archive before cleanup",
        "launch and close real Windows game",
        "write run evidence and private runtime archive exports"]
    if getattr(args, "resume_run", None):
        side_effects[0] = "restore the original workspace and official CLI session; retain cumulative accounting and archives"
    return {"mode":args.command,"execute":bool(args.execute),"profile":str(Path(args.profile).resolve()),
            "runtime_backend":backend,
            **({"resume_run": str(Path(args.resume_run).resolve())} if getattr(args, "resume_run", None) else {}),
            **({"task_instructions": task_instructions["descriptor"]}
               if task_instructions is not None else {}),
            "agent":getattr(args,"agent",None) if args.command=="develop" else None,
            "model_proxy":proxy,
            "upstream_api":getattr(args,"upstream_api","responses"),
            "provider_model":getattr(args,"provider_model",None),
            "scenario":args.scenario,"windows_input":args.windows_input,"out_dir":str(_out_dir(args)),
            "tic_budget":args.tic_budget,"max_tics":args.max_tics,"wall_seconds":args.wall_seconds,
            "tic_budget_policy": ("hard_upper_bound_unused_allowed"
                                  if args.command=="develop" else None),
            "token_limit":getattr(args,"token_limit",None),"model":getattr(args,"model",None),"reasoning_effort":getattr(args,"reasoning_effort",None),
            "token_accounting":getattr(args,"token_accounting",None),
            "context_compaction": (_context_compaction_plan(
                getattr(args,"agent","claude"), getattr(args,"token_accounting",None),
                getattr(args,"model",None))
                if args.command=="develop" else None),
            "finalization_warning_fraction": (0.7 if args.command=="develop" and
                                               getattr(args,"token_limit",None) is not None else None),
            "finalization_warning_delivery": ("next_admitted_model_request_once"
                                                if args.command=="develop" and
                                                getattr(args,"token_limit",None) is not None else None),
            "unknown_usage_policy": ("strict_stop" if getattr(args,"token_accounting",None)=="strict"
                else "unlimited_warn_continue" if getattr(args,"token_limit",None) is None
                else "warn_continue") if args.command=="develop" else None,
            "human_assisted":False if args.command=="develop" else None,
            **({"model_gateway_stop_on_http_400": True}
               if args.command == "develop" and getattr(args, "stop_on_http_400", False) else {}),
            **({"minimum_development_fraction": args.minimum_development_fraction}
               if getattr(args, "minimum_development_fraction", None) is not None else {}),
            "program_id":getattr(args,"program_id",None),"source_programs":getattr(args,"source_programs",None),
            "billing_backend":"host-model-gateway" if args.command=="develop" else None,
            "cost_estimate":("unbounded model cost; no token limit; missing usage warns without stopping; recorded usage may be incomplete and exact total cost unknown"
                             if args.command=="develop" and getattr(args,"token_limit",None) is None else
                             (("unknown; post-response usage accounting; final request may exceed token_limit; "
                               "one finalization warning is injected into the next admitted request after 70% confirmed usage (about 30% remaining); "
                               + "missing usage warns and ordinary recovery may continue; actual total then unbounded by token threshold"
                             if getattr(args,"token_accounting","usage")=="usage" else
                             "unknown; strict prospective total-token reservation; one finalization warning is injected into the next admitted request after 70% confirmed usage (about 30% remaining)")
                             if args.command=="develop" else "no model tokens")),
            "wall_enforcement":("no experiment deadline; per-request network timeout remains finite"
                                if args.wall_seconds is None else
                                "cooperative watchdog; blocked host native/capture/scorer calls are not hard-killed"),
            "side_effects":side_effects}

def _classify(command, *, driver=None, selected=None, result=None):
    if command=="develop":
        if not isinstance(driver,dict) or driver.get("success") is False:
            return "failed",False
        if driver.get("explicit_finished") is True:
            return ("complete",True) if selected else ("incomplete",False)
        if driver.get("exit_code") not in (None, 0):
            return "failed",False
        return "incomplete",False
    if not isinstance(result,dict): return "failed",False
    program_returncode = result.get("program_returncode")
    program_failed = isinstance(program_returncode, int) and program_returncode > 0
    valid=(not program_failed and
           result.get("outcome_kind","valid")!="infrastructure_error" and
           result.get("end_reason") not in {"program_error", "program_runner_error"} and
           result.get("complete",True) is not False and
           (result.get("budget_overshoot_tics",0) or 0) <=
           (STEP_BUDGET_TOLERANCE_TICS if result.get("mode") == "stepped" else 0))
    return ("complete",True) if valid else ("failed",False)


def _game_budget(game):
    status_method = getattr(game, "status", None)
    if not callable(status_method):
        return None
    status = status_method()
    keys = ("tics_budget", "tics_used", "tics_remaining",
            "budget_stop_reason", "budget_overshoot_tics")
    return {key: status.get(key) for key in keys}


def _record_runtime_exports(config, runtime):
    records = list(config.get("runtime_exports", ()))
    for record in getattr(runtime, "archives", ()):
        if record not in records: records.append(record)
    config["runtime_exports"] = records

def _dependencies():
    from contextlib import ExitStack
    from coding4game_gui.wsl2 import WslProfile
    from coding4game_gui.wsl2 import WslRuntime
    from coding4game_gui.benchmark.windows.session import GameSession
    from coding4game_gui.benchmark.code.tool_bridge import ToolServer
    from coding4game_gui.benchmark.code.submissions import ProgramStore
    from coding4game_gui.benchmark.evidence.timeline import DevelopmentTimeline
    from coding4game_gui.benchmark.code.model_gateway import ModelGateway
    from coding4game_gui.benchmark.code.agent_driver import run_agent
    from coding4game_gui.benchmark.code.open_session import OpenSession
    from coding4game_gui.benchmark.spec import load_game_spec
    from coding4game_gui.benchmark.spec import REPO_ROOT
    from coding4game_gui.benchmark.windows.env import make_bench_env
    from coding4game_gui.benchmark.windows.settings_session import benchmark_settings
    from coding4game_gui.benchmark.windows.save_session import benchmark_save
    return locals()

def _public_prompt(spec, **budgets):
    from coding4game_gui.benchmark.code.prompts import render_development_prompt
    return render_development_prompt(spec, **budgets)


def execute(args, deps=None):
    started = time.monotonic()
    started_unix = time.time()
    _validate_runtime(args)
    recovering = bool(getattr(args, "resume_run", None))
    if recovering and getattr(args, "stop_on_http_400", False):
        raise ValueError("Resume retains the original HTTP 400 forwarding policy")
    if recovering and not RECOVERY_ACCEPTANCE_COMPLETE and deps is None:
        raise ValueError("Public recovery execution is gated until serial recovery acceptance completes")
    task_instructions = None if recovering else _task_instructions_snapshot(args)
    backend = "wsl2"
    if deps is None: deps=_dependencies()
    _positive(args.tic_budget,"tic_budget")
    if args.wall_seconds is not None: _positive(args.wall_seconds,"wall_seconds")
    if args.max_tics is not None: _positive(args.max_tics,"max_tics")
    if args.command=="develop" and not args.confirm_paid_run: raise ValueError("develop execution requires --confirm-paid-run")
    if args.command=="develop" and args.token_limit is not None: _positive(args.token_limit,"token_limit")
    if args.command=="develop" and args.token_limit is None and getattr(args,'token_accounting','usage') == 'strict':
        raise ValueError('unlimited tokens require explicit usage accounting')
    api_key=None
    if args.command=="develop":
        api_key=os.environ.get(args.api_key_env)
        if not api_key: raise ValueError(f"missing credential environment variable: {args.api_key_env}")
    source_record = source_files = None
    if args.command == "test":
        source_store = deps["ProgramStore"](
            Path(args.source_programs).resolve(), max_bytes=MAX_PROGRAM_BYTES,
            max_files=MAX_PROGRAM_FILES)
        source_record = (source_store.get(args.program_id)[0]
                         if args.program_id else source_store.final())
        if source_record is None:
            raise ValueError("test requires --program-id or an explicit source final pointer")
        source_record, source_files = source_store.get(source_record["id"])
        if source_record.get("execution_platform") != "linux":
            raise ValueError("WSL2 final test requires a frozen Linux Python program")
        from coding4game_gui.benchmark.code.submissions import validate_program_contract
        validate_program_contract(source_record["entrypoint"])
    out=_out_dir(args)
    if out.exists() and not recovering: raise ValueError("out_dir already exists")
    profile_type = deps["WslProfile"]
    runtime_type = deps["WslRuntime"]
    profile=profile_type.load(args.profile); spec=deps["load_game_spec"](args.scenario,windows_input=args.windows_input)
    from coding4game_gui.benchmark.windows.terminal import validate_terminal_detection
    terminal_detection = validate_terminal_detection(spec)
    terminal_only = (spec.get("runtime") or {}).get("terminal_only_final_tests", False)
    if args.command == "develop":
        profile.agent(args.agent)
    if spec.get("engine")!="windows": raise ValueError("scenario must use the Windows engine")
    wall_remaining = args.wall_seconds
    deadline=started+wall_remaining if wall_remaining is not None else float('inf')
    make_env=lambda:deps["make_bench_env"](args.scenario,windows_input=args.windows_input)
    scenario_max=(spec.get("runtime") or {}).get("max_tics")
    if scenario_max is not None: _positive(scenario_max,"scenario runtime.max_tics")
    resolved_max=min(x for x in (args.max_tics,scenario_max) if x is not None) if any(x is not None for x in (args.max_tics,scenario_max)) else None
    recovery = None
    if recovering:
        from .recovery_controller import load_recovery
        recovery = load_recovery(args, profile, spec)
        deadline = recovery.deadline
        config = dict(recovery.config)
    else:
        out.mkdir(parents=True)
        config=_plan(args)|{"protocol":"open-code-v1","status":"running","resolved_max_tics":resolved_max,
                        "terminal_detection":"registered_terminal" if terminal_detection else "disabled",
                        "terminal_only_final_tests":terminal_only,
                        "autonomous_benchmark_eligible":False,
                        "tics_per_second":(spec.get("runtime") or {}).get("tics_per_second",10),
                        "program_limits":{"bytes":MAX_PROGRAM_BYTES,"files":MAX_PROGRAM_FILES}}
        from .recovery_controller import protocol_hash
        config.update(original_started_unix=started_unix, scenario_protocol_sha256=protocol_hash(spec))
        if args.command == "develop": config["upstream_url"] = args.upstream_url
        if Path(args.profile).is_file():
            config["runtime_profile_sha256"] = hashlib.sha256(Path(args.profile).read_bytes()).hexdigest()
    if args.command == "develop":
        config["agent_runtime"] = _agent_runtime(profile, args.agent)
        observation_size = (768, 461) if args.agent == "claude" else (1280, 768)
        # Persist the effective viewport even if runtime setup fails.
        config["observation_size"] = list(observation_size)
    else:
        observation_size = (1280, 768)
    if not recovering: _write_config(out/"config.json", config)
    timeline=store=gateway=runtime=game=None
    recovery_started = False
    evidence_root = out
    try:
        runtime=runtime_type(profile,evidence_dir=out)
        protected=[deps["REPO_ROOT"],out,*_credential_roots(args)]
        preflight=runtime.preflight(protected)
        if not recovering:
            config["runtime_preflight"]=preflight
            _write_config(out/"config.json", config)
        with deps["ExitStack"]() as stack:
            stack.enter_context(runtime)
            if recovering:
                from .recovery_controller import (load_recovery, verify_stopped,
                    prepare_append_logs, archive_selection_for_continuation)
                checked = load_recovery(args, profile, spec)
                if checked.original_config != recovery.original_config:
                    raise ValueError("Original development changed during recovery validation")
                if checked.route_migration != recovery.route_migration:
                    raise ValueError("Route authorization changed during recovery validation")
                if checked.selection_continuation != recovery.selection_continuation:
                    raise ValueError("Development continuation authorization changed during recovery validation")
                if checked.budget_notice != recovery.budget_notice:
                    raise ValueError("Recovery budget notice changed during recovery validation")
                if checked.agent_upgrade != recovery.agent_upgrade:
                    raise ValueError("Agent upgrade authorization changed during recovery validation")
                if checked.gateway_compatibility != recovery.gateway_compatibility:
                    raise ValueError("Gateway compatibility changed during recovery validation")
                if (checked.gateway_tool_replay != recovery.gateway_tool_replay
                        or checked.tool_replay_sources != recovery.tool_replay_sources):
                    raise ValueError("Tool replay provenance changed during recovery validation")
                verify_stopped(checked, runtime.native)
                recovery = checked
                deadline = recovery.deadline
                evidence_root = out / "recovery" / "segments" / uuid.uuid4().hex
                evidence_root.mkdir(parents=True, exist_ok=False)
                with (evidence_root / "config.before.json").open("xb") as snapshot:
                    snapshot.write(recovery.original_config); snapshot.flush(); os.fsync(snapshot.fileno())
                separators = prepare_append_logs(out, args.agent)
                _write_config(evidence_root / "recovery.json", recovery.evidence() | {
                    "append_separators": separators})
                archive_selection_for_continuation(recovery, evidence_root)
                config = dict(recovery.config)
                config.update(status="running", result_valid=False, runtime_preflight=preflight)
                if recovery.budget_notice is not None:
                    config["minimum_development_fraction"] = recovery.budget_notice["authorization"]["minimum_development_fraction"]
                    config["budget_notice_updates"] = [*config.get("budget_notice_updates", []),
                        recovery.budget_notice | {"segment": evidence_root.relative_to(out).as_posix()}]
                    config["human_assisted"] = True
                if recovery.selection_continuation is not None:
                    config["selection_continuations"] = [
                        *config.get("selection_continuations", []),
                        recovery.selection_continuation | {"segment": evidence_root.relative_to(out).as_posix()}]
                    config.update(final_program_id=None, agent_result=None, human_assisted=True)
                if recovery.route_migration is not None:
                    grant = recovery.route_migration["authorization"]
                    authorization_bytes = Path(args.recovery_route_authorization).read_bytes()
                    if hashlib.sha256(authorization_bytes).hexdigest() != recovery.route_migration["authorization_sha256"]:
                        raise ValueError("Route authorization changed before preservation")
                    with (evidence_root / "route-authorization.json").open("xb") as authorization_snapshot:
                        authorization_snapshot.write(authorization_bytes)
                        authorization_snapshot.flush()
                        os.fsync(authorization_snapshot.fileno())
                    config.setdefault("original_upstream_url", config["upstream_url"])
                    config["upstream_url"] = grant["to_upstream_url"]
                    config["upstream_route_migrations"] = [
                        *config.get("upstream_route_migrations", []),
                        recovery.route_migration | {"segment": evidence_root.relative_to(out).as_posix()}]
                logs = list(config.get("model_gateway_logs", ["model_gateway.jsonl"]))
                (evidence_root / "model_gateway.jsonl").touch(exist_ok=False)
                logs.append((evidence_root / "model_gateway.jsonl").relative_to(out).as_posix())
                config["model_gateway_logs"] = logs
                config.setdefault("recovery_segments", []).append(evidence_root.relative_to(out).as_posix())
                runtime.evidence_dir = evidence_root
                recovery_started = True
            if args.command == "develop":
                config["agent_runtime"] = _agent_runtime(
                    profile, args.agent, recovery.agent_upgrade if recovering else None)
                import psutil
                config["host_owner"] = {"pid": os.getpid(), "created": psutil.Process().create_time()}
            _write_config(out/"config.json", config)
            if "benchmark_settings" in deps:
                config["game_settings"] = stack.enter_context(
                    deps["benchmark_settings"](spec, evidence_root / "game-settings"))
            if "benchmark_save" in deps:
                config["save_protection"] = stack.enter_context(
                    deps["benchmark_save"](spec, evidence_root / "save-protection"))
            role="development" if args.command=="develop" else "final_test"
            must_submit = role == "development"
            if recovering:
                runtime_options = {"recovery": recovery.descriptor}
                if recovery.agent_upgrade is not None:
                    runtime_options["agent_upgrade"] = recovery.agent_upgrade
                supervisor = runtime.start(role, **runtime_options)
            else:
                supervisor = runtime.start(role)
            config["runtime_role"] = runtime.role_evidence(role)
            _record_runtime_exports(config, runtime)
            _write_config(out/"config.json", config)
            if time.monotonic() >= deadline:
                raise TimeoutError("wall deadline exhausted during setup/archive; model and game not started")
            terminal_options = {"require_terminal": True} if terminal_only and args.command == "test" else {}
            game=stack.enter_context(deps["GameSession"](
                make_env, out, tic_budget=args.tic_budget, max_tics=resolved_max,
                max_action_tics=(spec.get("runtime") or {}).get(
                    "max_action_tics", 1000),
                deadline=deadline, **terminal_options))
            if recovering and game.status()["tics_used"] < (recovery.config.get("game_budget") or {}).get("tics_used", 0):
                raise ValueError("Recovered gameplay ledger is below the original cumulative usage")
            selected = None
            if args.command=="develop":
                store=deps["ProgramStore"](out/"programs",max_bytes=MAX_PROGRAM_BYTES,max_files=MAX_PROGRAM_FILES)
            else:
                store=deps["ProgramStore"](out/"programs",max_bytes=MAX_PROGRAM_BYTES,max_files=MAX_PROGRAM_FILES)
                selected=store.save(source_files,source_record["entrypoint"],
                    description=source_record.get("description",""),
                    dependencies=source_record.get("dependencies"),
                    execution_platform=source_record.get("execution_platform"),
                    **({"observation_size": source_record["observation_size"]}
                       if "observation_size" in source_record else {}))
                observation_size = tuple(source_record.get("observation_size", [1280, 768]))
                if selected["id"] != source_record["id"]: raise ValueError("copied program identity changed")
                source_config = Path(args.source_programs).resolve().parent / "config.json"
                provenance = json.loads(source_config.read_text(encoding="utf-8")) if source_config.is_file() else {}
                config["human_assisted"] = provenance.get("human_assisted")
                config["source_run_dir"] = str(source_config.parent)
            secrets=[]; timeline=deps["DevelopmentTimeline"](out/"development_timeline.jsonl",secrets=secrets)
            session=deps["OpenSession"](
                game, supervisor, store, timeline, profile.python, phase=role,
                deadline=deadline,
                require_evaluated_submission=must_submit,
                execution_platform=getattr(profile, "execution_platform", None),
                **({"require_terminal_submission": True} if terminal_only else {}))
            stack.callback(session.close)
            if recovering: session.restore_evaluations(recovery.evaluations)
            config["observation_size"] = list(observation_size)
            bridge_options = ({"observation_size": observation_size}
                              if observation_size != (1280, 768) else {})
            bridge=stack.enter_context(deps["ToolServer"](session,port=profile.ports[f"{role}_game"],phase=role,
                                                        **bridge_options))
            session.bind_bridge(bridge)
            if args.command=="develop":
                from .agent_driver import protocol_for

                strict=getattr(args,"token_accounting","usage")=="strict"
                wall_remaining = None if deadline == float('inf') else deadline - time.monotonic()
                if wall_remaining is not None and wall_remaining <= 0:
                    raise TimeoutError("Original wall deadline exhausted before gateway startup")
                gateway_options = {}
                if recovering and recovery.gateway_compatibility is not None:
                    gateway_options = {
                        "anthropic_user_id_mode": recovery.gateway_compatibility["user_id_mode"],
                        "anthropic_session_id": recovery.session_id,
                    }
                if config.get("model_gateway_stop_on_http_400", False):
                    gateway_options["stop_on_http_400"] = True
                if recovering and recovery.gateway_tool_replay is not None:
                    gateway_options["anthropic_tool_replay_mode"] = recovery.gateway_tool_replay["replay_mode"]
                    gateway_options["anthropic_tool_replay_sources"] = recovery.tool_replay_sources
                gateway=deps["ModelGateway"](args.upstream_url,api_key,args.model,args.token_limit,wall_remaining,evidence_root/"model_gateway.jsonl",port=profile.ports["gateway"],count_input=strict,strict_total_enforcement=strict,
                    proxy_mode=args.model_proxy_mode,proxy_url=args.model_proxy_url,
                    protocol=protocol_for(args.agent),
                    upstream_api=getattr(args,"upstream_api","responses"),
                    provider_model=getattr(args,"provider_model",None), **gateway_options)
                if recovering: gateway.restore_usage(recovery.usage)
                gateway=stack.enter_context(gateway)
                session.gateway=gateway
                timeline.secrets = tuple(timeline.secrets) + (gateway.token, bridge.token, api_key)
                task_prompt = recovery.prompt if recovering else _public_prompt(
                    spec, tic_budget=args.tic_budget, wall_seconds=args.wall_seconds,
                    token_limit=args.token_limit, max_tics=resolved_max,
                    token_accounting=getattr(args, "token_accounting", "usage"),
                    agent_name=args.agent, runtime_backend=backend,
                )
                if not recovering:
                    from .development_policy import review_notice
                    task_prompt += review_notice(config.get("minimum_development_fraction"),
                        tic_budget=args.tic_budget, token_limit=args.token_limit)
                if observation_size != (1280, 768):
                    width, height = observation_size
                    task_prompt = (task_prompt.replace("1280x768", f"{width}x{height}")
                                   .replace("1280×768", f"{width}×{height}")
                                   .replace("y=0..767", f"y=0..{height - 1}")
                                   .replace("x=0..1279", f"x=0..{width - 1}")
                                   .replace("640,384", f"{width // 2},{height // 2}"))
                if task_instructions is not None:
                    task_prompt = _append_task_instructions(task_prompt, task_instructions)
                prompt_path = evidence_root / ("resume_prompt.md" if recovering else "task_prompt.md")
                prompt_path.write_text(task_prompt, encoding="utf-8")
                if not recovering:
                    config["task_prompt_sha256"] = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
                _write_config(out/"config.json", config)
                driver = deps["run_agent"]
                agent_options = {"agent": args.agent}
                if recovering: agent_options["resume_session_id"] = recovery.session_id
                if getattr(args, "reasoning_effort", None):
                    agent_options["reasoning_effort"] = args.reasoning_effort
                continuation = {'tics_remaining': lambda: game.status()['tics_remaining']}
                result=driver(supervisor,profile,gateway,bridge,task_prompt,timeline,deadline,
                              finished=lambda:session.finished, **continuation, **agent_options)
                if recovering and not result.get("recovery_identity_observed"):
                    raise RuntimeError("Original official CLI session identity was not acknowledged")
                selected=store.final()
                status,valid=_classify("develop",driver=result,selected=selected)
                config.update(agent_result=result,final_program_id=selected["id"] if selected else None,
                              final_score=None,final_score_status="unavailable_not_tested",
                              autonomous_benchmark_eligible=False,
                              result_valid=valid,status=status)
            else:
                result=session.run_program(selected["id"],max_tics=resolved_max)
                status,valid=_classify("test",result=result)
                config.update(program_id=selected["id"],result=result,result_valid=valid,status=status)
                config["autonomous_benchmark_eligible"] = False
        timeline.render_html(out/"development_timeline.html",submissions=store.list())
    except BaseException as exc:
        if recovering and not recovery_started:
            raise
        config.update(status="failed",result_valid=False,autonomous_benchmark_eligible=False,error=f"{type(exc).__name__}: {exc}")
        if timeline is not None:
            try: timeline.render_html(out/"development_timeline.html",submissions=store.list() if store else ())
            except Exception as report_error: config["report_error"]=f"{type(report_error).__name__}: {report_error}"
        if gateway is not None: config["model_budget"]=gateway.status()
        if game is not None:
            try:
                game_budget = _game_budget(game)
                if game_budget is not None: config["game_budget"]=game_budget
            except Exception as budget_error: config["game_budget_error"]=f"{type(budget_error).__name__}: {budget_error}"
        if runtime is not None: _record_runtime_exports(config, runtime)
        config['elapsed_wall_seconds'] = (time.time() - config['original_started_unix']
            if 'original_started_unix' in config else
            recovery.config.get('elapsed_wall_seconds', 0) + time.monotonic() - started if recovering
            else time.monotonic() - started)
        _write_config(out/"config.json", config)
        raise
    if gateway is not None: config["model_budget"]=gateway.status()
    if game is not None:
        game_budget = _game_budget(game)
        if game_budget is not None: config["game_budget"]=game_budget
    if runtime is not None: _record_runtime_exports(config, runtime)
    config['elapsed_wall_seconds'] = (time.time() - config['original_started_unix']
        if 'original_started_unix' in config else
        recovery.config.get('elapsed_wall_seconds', 0) + time.monotonic() - started if recovering
        else time.monotonic() - started)
    _write_config(out/"config.json", config)
    return config

def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("develop", "test"):
        command = sub.add_parser(name)
        command.add_argument("--profile", required=True)
        command.add_argument("--runtime-backend", choices=("wsl2",), default="wsl2")
        command.add_argument("--scenario", choices=FORMAL_SCENARIOS, required=True)
        command.add_argument("--windows-input", choices=("gamepad", "gamepad-kbm"), default="gamepad")
        command.add_argument("--out-dir", required=True)
        command.add_argument("--tic-budget", type=int, required=True)
        command.add_argument("--max-tics", type=int)
        command.add_argument("--wall-seconds", type=_optional_limit, required=True,
                             help="Positive seconds or unlimited (no experiment deadline)")
        command.add_argument("--credential-root", action="append", default=[])
        command.add_argument("--execute", action="store_true")
    d = sub.choices["develop"]
    d.add_argument("--confirm-paid-run", action="store_true")
    d.add_argument("--agent", choices=("claude", "codex"), default="claude")
    d.add_argument("--model", required=True)
    d.add_argument("--token-limit", type=_optional_limit, required=True,
                   help="Positive tokens or unlimited; finite limits inject one finalization "
                        "warning after 70%% confirmed usage (about 30%% remaining)")
    d.add_argument("--upstream-url", required=True)
    d.add_argument("--api-key-env", default="OPENAI_API_KEY")
    d.add_argument('--model-proxy-mode', choices=('system','direct','explicit'), default='system')
    d.add_argument('--upstream-api', choices=('responses','chat'), default='responses')
    d.add_argument('--provider-model', help='Provider-qualified model used by a configured protocol adapter')
    d.add_argument('--task-instructions-file',
                   help='UTF-8 file (maximum 64 KiB) appended to this development task')
    d.add_argument("--reasoning-effort")
    d.add_argument('--token-accounting', choices=('usage','strict'), default='usage',
                   help='usage: settle after each response; warn and continue recovery when usage is unknown; final or unknown requests may exceed token-limit. strict: require a compatible input counter.')
    d.add_argument('--model-proxy-url', help='Host-only HTTP proxy URL; requires explicit mode; no embedded credentials')
    d.add_argument('--resume-run', help='Continue a closed failed/incomplete WSL2 development in its original output directory and allowance')
    d.add_argument('--recovery-route-authorization',
                   help='Host-only JSON grant for one explicitly authorized HTTPS hostname migration of this closed run')
    d.add_argument('--development-continuation-authorization',
                   help='Host-only JSON grant to continue this exact selected development with original cumulative limits')
    d.add_argument('--minimum-development-fraction', type=float,
                   help='Human review guideline: continue meaningful work until either original resource reaches this fraction')
    d.add_argument('--stop-on-http-400', action='store_true',
                   help='Revoke paid forwarding after the first upstream HTTP 400')
    d.add_argument('--recovery-budget-notice',
                   help='Host-only configuration-bound JSON authorization for a generic development review notice')
    t = sub.choices["test"]
    t.add_argument("--source-programs", required=True)
    t.add_argument("--program-id")
    return p

def main(argv=None):
    args=parser().parse_args(argv); plan=_plan(args)
    if getattr(args, "resume_run", None):
        from .recovery_controller import load_recovery
        from coding4game_gui.wsl2 import WslProfile
        from coding4game_gui.benchmark.spec import load_game_spec
        recovery = load_recovery(args, WslProfile.load(args.profile),
                                 load_game_spec(args.scenario, windows_input=args.windows_input))
        plan["recovery"] = recovery.evidence()
    print(json.dumps(plan,indent=2))
    if not args.execute: return plan
    result = execute(args)
    summary = {"status": result["status"], "out_dir": plan["out_dir"]}
    if result["status"] == "failed":
        agent_result = result.get("agent_result") or {}
        summary.update(reason=agent_result.get("reason"),
                       error=agent_result.get("error"),
                       exit_code=agent_result.get("exit_code"))
    print(json.dumps(summary, ensure_ascii=False))
    return result
if __name__=="__main__": main()
