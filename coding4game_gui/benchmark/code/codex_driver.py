"""Drive the configured Codex CLI inside the persistent development box.

This module prepares an isolated user-level Codex configuration and consumes
the CLI's JSONL output.  It does not discover a Codex executable, read personal
configuration/authentication, or provide a non-gateway model fallback.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any
from typing import Callable
from coding4game_gui.benchmark.code.budget_status import BudgetReporter


_MAX_JSONL_LINE = 4 * 1024 * 1024
CODEX_EXEC_YIELD_MS = 3_600_000
_RECOVERY_DELAYS = (30, 60, 120, 300)
_RESUME_PROMPT = (
    "The previous model turn failed due to a temporary service or transport error. "
    "Continue the same task from the existing workspace and conversation. "
    "Do not replay completed actions. Inspect current state before further actions; "
    "all original budgets and task rules still apply."
)

# Models exposed under gateway aliases are absent from Codex's bundled model
# catalog. Explicit metadata prevents fallback defaults from allowing the
# conversation request to grow beyond the provider's documented window.
def _gateway_metadata(context_window: int, max_output: int | None = None) -> dict[str, int]:
    metadata = {"context_window": context_window,
                "compact_at": context_window * 4 // 5}
    if max_output is not None:
        metadata["max_output"] = max_output
    return metadata


_GATEWAY_MODEL_METADATA = {
    "deepseek-v4.1-flash": _gateway_metadata(1_000_000, 384_000),
    "qwen3.8-flash": _gateway_metadata(1_000_000, 131_072),
    "glm-5.3-flash": _gateway_metadata(1_000_000, 128_000),
    "kimi-k3": _gateway_metadata(1_048_576, 131_072),
    "qwen3.8-27b": _gateway_metadata(262_144),
    "hy4-preview": _gateway_metadata(1_000_000, 128_000),
}


def _transient_failure(message: str) -> bool:
    text = message.lower()
    if any(s in text for s in ('401', '403', 'unauthorized', 'forbidden',
                               'budget_exhausted', 'submission_selected', 'cancelled', 'canceled')):
        return False
    return any(s in text for s in ('stream closed before response.completed',
                                   'rate limit exceeded', 'rate_limit_exceeded',
                                   '429 too many requests', 'too many requests',
                                   'service temporarily unavailable', 'servers are currently overloaded',
                                   'server overloaded', 'timeout', 'connection reset',
                                   'connection closed', 'timed out', '502 bad gateway',
                                   '503 service unavailable', '504 gateway timeout'))


def _toml(value: str) -> str:
    """JSON string syntax is also valid TOML basic-string syntax."""
    return json.dumps(str(value), ensure_ascii=False)


def _configuration(profile, gateway, client_path: str, reasoning_effort=None) -> bytes:
    lines = [
        f"model = {_toml(gateway.model)}",
        'model_provider = "c4g_gateway"',
        'approval_policy = "never"',
        'sandbox_mode = "danger-full-access"',
        'tool_output_token_limit = 8000',
        'web_search = "live"',
        # Match the MCP timeout so long host calls do not require polling turns.
        # The experiment watchdog and original resource deadlines still apply.
        'features.code_mode.enabled = true',
        f'features.code_mode.default_exec_yield_time_ms = {CODEX_EXEC_YIELD_MS}',
        'features.code_mode_host = true',
    ]
    metadata = _GATEWAY_MODEL_METADATA.get(gateway.model)
    if metadata is not None:
        lines.extend([
            f"model_context_window = {metadata['context_window']}",
            f"model_auto_compact_token_limit = {metadata['compact_at']}",
        ])
    lines.extend([
        "",
        "[model_providers.c4g_gateway]",
        'name = "C4G scoped host gateway"',
        f"base_url = {_toml(gateway.base_url)}",
        'env_key = "C4G_GATEWAY_TOKEN"',
        'wire_api = "responses"',
        "requires_openai_auth = false",
        "request_max_retries = 5",
        "stream_max_retries = 5",
        "supports_websockets = false",
        "",
        "[mcp_servers.c4g_game]",
        f"command = {_toml(profile.python)}",
        f"args = [{_toml('-I')}, {_toml('-B')}, {_toml('-X')}, {_toml('utf8')}, {_toml(client_path)}, {_toml('--mcp')}]",
        'env_vars = ["C4G_BRIDGE_URL", "C4G_BRIDGE_TOKEN", "C4G_BRIDGE_PHASE"]',
        "enabled = true",
        "required = true",
        "startup_timeout_sec = 30",
        "tool_timeout_sec = 3600",
        "",
    ])
    if reasoning_effort:
        lines.insert(0, f"model_reasoning_effort = {_toml(reasoning_effort)}")
    return "\n".join(lines).encode("utf-8")


def prepare_codex_run(supervisor, profile, gateway, bridge, prompt: str, *, interactive=False, reasoning_effort=None) -> dict[str, Any]:
    """Stage the isolated user config and MCP client; return launch parameters."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Codex prompt must be nonempty")
    for name, value in (("gateway URL", gateway.base_url), ("gateway token", gateway.token),
                        ("model", gateway.model), ("bridge URL", bridge.url),
                        ("bridge token", bridge.token)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a nonempty string")
    from coding4game_gui.paths import runtime_source
    client_source = runtime_source("client.py")
    stage = getattr(supervisor, "stage_connection", supervisor.stage)
    client_dir = stage(
        {"client.py": client_source, "c4g_client.py": client_source}, name="mcp")
    client_path = (supervisor.native_path(client_dir, "client.py")
                   if hasattr(supervisor, "native_path") else str(Path(client_dir) / "client.py"))
    config_dir = stage(
        {"config.toml": _configuration(profile, gateway, client_path, reasoning_effort)},
        name=".codex")
    expected_config = (supervisor.native_path(supervisor.workspace, ".codex")
                       if hasattr(supervisor, "native_path") else str(Path(supervisor.workspace) / ".codex"))
    if str(config_dir) != expected_config:
        raise RuntimeError("Codex user configuration was not staged under USERPROFILE")
    return {
        "argv": ([str(profile.codex), "--no-alt-screen", "--sandbox", "danger-full-access", prompt]
                 if interactive else [str(profile.codex), "exec", "--json", "--skip-git-repo-check",
                                      "--sandbox", "danger-full-access", "-"]),
        "stdin_text": None if interactive else prompt,
        "gateway": {"url": gateway.base_url, "token": gateway.token,
                    "port": gateway.port},
        "cwd": supervisor.workspace,
        "connection_env": {
            "C4G_BRIDGE_URL": bridge.url,
            "C4G_BRIDGE_TOKEN": bridge.token,
            "C4G_BRIDGE_PHASE": "development",
        },
    }


def _append(timeline, kind: str, details: dict, summary: str = "") -> None:
    timeline.append(kind, source="sandbox", summary=summary,
                    details={"origin": "codex", **details})


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value


def _evidence_path(timeline, name: str) -> Path | None:
    path = getattr(timeline, "path", None)
    return Path(path).parent / name if path is not None else None


def _persist(path: Path | None, data: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write(data)


def run_codex(
    supervisor,
    profile,
    gateway,
    bridge,
    prompt: str,
    timeline,
    deadline: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    poll_interval: float = 0.05,
    reasoning_effort=None,
    finished: Callable[[], bool] = lambda: False,
    tics_remaining: Callable[[], float] | None = None,
    resume_session_id=None,
) -> dict[str, Any]:
    """Run one Codex process until explicit finish, exit, fuse, or overflow.

    ``deadline`` is an absolute value in the supplied monotonic ``clock``
    domain.  A zero exit code alone is not benchmark success; only the
    host-owned ``finished`` predicate identifies explicit submission finish.
    """
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        raise ValueError("deadline must be an absolute clock value")
    if poll_interval < 0:
        raise ValueError("poll_interval must be nonnegative")
    if resume_session_id is not None and (not isinstance(resume_session_id, str)
                                         or str(uuid.UUID(resume_session_id)) != resume_session_id):
        raise ValueError("recovery requires the original canonical Codex thread UUID")
    prepared = prepare_codex_run(supervisor, profile, gateway, bridge, prompt, reasoning_effort=reasoning_effort)
    if resume_session_id is not None:
        prepared["argv"] = [str(profile.codex), "exec", "--sandbox", "danger-full-access",
                            "resume", "--json", "--skip-git-repo-check", resume_session_id, "-"]
    launch = supervisor.launch_codex(
        prepared["argv"], gateway=prepared["gateway"], cwd=prepared["cwd"],
        connection_env=prepared["connection_env"], stdin_text=prepared["stdin_text"])
    handle = launch["handle"]
    stdout_pending = ""
    event_count = 0
    invalid_jsonl = False
    thread_id = resume_session_id
    recovery_identity_observed = False
    failure_message = ""
    recovery_attempts = 0
    continuation_attempts = 0
    no_progress_exits = 0
    previous_remaining = tics_remaining() if tics_remaining is not None else None
    pending_tools = set()
    api_error = ""
    evidence_secrets = (str(gateway.token), str(bridge.token))
    stdout_evidence = _evidence_path(timeline, "codex_events.jsonl")
    stderr_evidence = _evidence_path(timeline, "codex_stderr.log")
    budget_reporter = BudgetReporter(gateway, timeline)

    def outcome(reason: str, *, exit_code=None, explicit=False, success=None):
        budget = budget_reporter.update()
        if reason == "codex_exited" and budget.get("stop_reason"):
            reason, success = budget["stop_reason"], False
        return {"reason": reason, "success": success,
                "thread_id": thread_id,
                "recovery_identity_observed": recovery_identity_observed,
                "budget_message": budget.get("budget_message"),
                "explicit_finished": explicit, "exit_code": exit_code,
                "event_count": event_count,
                "recovery_attempts": recovery_attempts,
                "continuation_attempts": continuation_attempts,
                "evidence": {
                    "events": str(stdout_evidence) if stdout_evidence else None,
                    "stderr": str(stderr_evidence) if stderr_evidence else None,
                }}

    def process_stdout(data: str) -> str | None:
        nonlocal stdout_pending, invalid_jsonl, event_count, thread_id, failure_message, api_error, recovery_identity_observed
        stdout_pending += data
        if "\n" not in stdout_pending and len(stdout_pending.encode('utf-8')) > _MAX_JSONL_LINE:
            return "output_overflow"
        while "\n" in stdout_pending:
            line, stdout_pending = stdout_pending.split("\n", 1)
            if len(line.encode('utf-8')) > _MAX_JSONL_LINE:
                return "output_overflow"
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Codex JSONL event is not an object")
            except (json.JSONDecodeError, ValueError) as error:
                invalid_jsonl = True
                _append(timeline, "codex_diagnostic",
                        {"data": line, "error": f"{type(error).__name__}: {error}"},
                        "invalid Codex JSONL")
                continue
            safe_event = json.loads(_redact(json.dumps(
                event, ensure_ascii=False, separators=(",", ":")), evidence_secrets))
            if event.get("type") == "thread.started":
                try:
                    thread_id = str(uuid.UUID(event.get("thread_id", "")))
                except (ValueError, TypeError, AttributeError):
                    thread_id = None
                if resume_session_id is not None and thread_id != resume_session_id:
                    _append(timeline, "codex_recovery_identity_mismatch", {
                        "expected_thread_id": resume_session_id, "observed_thread_id": thread_id})
                    gateway.revoke("session_identity_changed")
                    supervisor.terminate(handle)
                    raise RuntimeError("recovered Codex CLI thread identity changed")
                recovery_identity_observed = resume_session_id is not None
            if event.get("type") == "turn.failed":
                failure_message = str((event.get("error") or {}).get("message", ""))
            if event.get("type") == "turn.completed":
                failure_message = ""
            if event.get("type") in ("error", "turn.failed"):
                message = (event.get("message") if event.get("type") == "error"
                           else (event.get("error") or {}).get("message"))
                message = " ".join(_redact(str(message or "Unknown API error"),
                                           evidence_secrets).split())
                if message != api_error:
                    api_error = message
                    print(f"[C4G API {time.strftime('%H:%M:%S')}] {message}", flush=True)
            elif event.get("type") == "turn.completed" and api_error:
                print(f"[C4G API {time.strftime('%H:%M:%S')}] Recovered: model turn completed.",
                      flush=True)
                api_error = ""
            item = event.get("item") or {}
            if item.get("type") in ("mcp_tool_call", "command_execution"):
                if event.get("type") == "item.started":
                    pending_tools.add(item.get("id"))
                elif event.get("type") == "item.completed":
                    pending_tools.discard(item.get("id"))
            event_count += 1
            _persist(stdout_evidence, json.dumps(
                safe_event, ensure_ascii=False, separators=(",", ":")) + "\n")
            _append(timeline, "codex_event", safe_event,
                    str(safe_event.get("type", "event")))
        if len(stdout_pending.encode('utf-8')) > _MAX_JSONL_LINE:
            return "output_overflow"
        return None

    while True:
        budget = budget_reporter.update()
        if budget.get("stop_reason") == "game_startup_failed":
            supervisor.terminate(handle)
            _append(timeline, "codex_lifecycle", {"reason": "game_startup_failed"},
                    "Game startup retries exhausted; stopping development")
            return outcome("game_startup_failed", success=False)
        if finished():
            gateway.revoke("submission_selected")
            supervisor.terminate(handle)
            _append(timeline, "codex_lifecycle", {"reason": "explicit_finished"})
            return outcome("explicit_finished", explicit=True)
        if clock() >= deadline:
            gateway.revoke("deadline_exhausted")
            supervisor.terminate(handle)
            _append(timeline, "codex_lifecycle", {"reason": "deadline"})
            return outcome("deadline", success=False)
        try:
            state = supervisor.poll(handle)
        except Exception as error:
            gateway.revoke()
            supervisor.terminate(handle)
            _append(timeline, "codex_lifecycle",
                    {"reason": "supervisor_error", "error": f"{type(error).__name__}: {error}"})
            return outcome("supervisor_error", success=False)
        for stream_event in state.get("events", []):
            stream = stream_event.get("stream")
            data = stream_event.get("data", "")
            if not isinstance(data, str):
                data = str(data)
            if stream == "stdout":
                if process_stdout(data) == "output_overflow":
                    state["output_overflow"] = True
            else:
                safe_data = _redact(data, evidence_secrets)
                _persist(stderr_evidence, safe_data)
                safe_stream_event = dict(stream_event)
                safe_stream_event["data"] = safe_data
                _append(timeline, "codex_stderr", safe_stream_event, "Codex stderr")
        if state.get("output_overflow"):
            gateway.revoke()
            supervisor.terminate(handle)
            _append(timeline, "codex_lifecycle", {"reason": "output_overflow"})
            return outcome("output_overflow", success=False)
        if not state.get("running", False) and state.get("output_complete", True):
            if stdout_pending.strip():
                process_stdout("\n")
            if finished():
                gateway.revoke("submission_selected")
                _append(timeline, "codex_lifecycle", {"reason": "explicit_finished",
                                                       "exit_code": state.get("exit_code")})
                return outcome("explicit_finished", exit_code=state.get("exit_code"),
                               explicit=True)
            reason = "invalid_jsonl" if invalid_jsonl else "codex_exited"
            budget = budget_reporter.update()
            continue_task = False
            if (state.get('exit_code') == 0 and tics_remaining is not None
                    and not invalid_jsonl and thread_id and not pending_tools
                    and not budget.get('revoked') and not budget.get('stop_reason')):
                remaining = tics_remaining()
                no_progress_exits = (0 if remaining < previous_remaining else no_progress_exits + 1)
                previous_remaining = remaining
                if no_progress_exits > 2:
                    _append(timeline, 'codex_lifecycle', {'reason': 'development_no_progress',
                            'tics_remaining': remaining},
                            'Repeated exits without final selection or gameplay progress')
                    return outcome('development_no_progress', exit_code=0, success=False)
                continue_task = True
            if continue_task or (state.get("exit_code") == 1 and not invalid_jsonl and thread_id
                    and _transient_failure(failure_message)
                    and not budget.get("revoked") and not budget.get("stop_reason")):
                if continue_task:
                    delay = 0
                    continuation_attempts += 1
                    resume_prompt = (
                        "Development is not finished because no final program has been selected. "
                        f"{remaining:g} game tics remain available. Continue useful development "
                        "or explicitly select a saved candidate with submission.finish(id). "
                        "Unused tics do not block final selection; all budgets are upper bounds. "
                        "Do not replay completed actions or spend tics merely to exhaust a budget. "
                        "Other budgets and stop conditions still apply.")
                    _append(timeline, 'codex_continuation', {'attempt': continuation_attempts,
                            'tics_remaining': remaining, 'no_progress_exits': no_progress_exits,
                            'thread_id': thread_id}, 'Continuing unfinished development')
                    print("[C4G development] Continuing session: no final program selected; "
                          f"{remaining:g} game tics remain available "
                          f"(continuation {continuation_attempts}).", flush=True)
                else:
                    delay = _RECOVERY_DELAYS[min(recovery_attempts, len(_RECOVERY_DELAYS) - 1)]
                    recovery_attempts += 1
                    resume_prompt = _RESUME_PROMPT
                if not continue_task:
                    _append(timeline, "codex_recovery", {
                        "attempt": recovery_attempts, "maximum": None,
                        "delay_seconds": delay, "thread_id": thread_id,
                        "error": _redact(failure_message, evidence_secrets)},
                        "Resuming the same Codex session after a transient failure")
                    print(f"[C4G recovery] Attempt {recovery_attempts} "
                          f"in {delay}s; preserving workspace and budgets.", flush=True)
                for _ in range(delay):
                    if finished() or clock() >= deadline:
                        break
                    budget = budget_reporter.update()
                    if budget.get("revoked") or budget.get("stop_reason"):
                        break
                    time.sleep(1)
                budget = budget_reporter.update()
                if finished() or clock() >= deadline:
                    continue
                if budget.get("revoked") or budget.get("stop_reason"):
                    return outcome("codex_exited", exit_code=1, success=False)
                if pending_tools:
                    _append(timeline, "codex_recovery_wait", {
                        "pending_tool_ids": sorted(pending_tools)},
                        "Waiting for host tool calls to settle before resuming")
                    print("[C4G recovery] Waiting for host tool calls to finish; no replay.", flush=True)
                    # CLI item completion may be lost on transport failure. The
                    # host bridge owns execution and knows when calls settle.
                    settled = getattr(bridge, "calls_settled", None)
                    if settled is None:
                        _append(timeline, "codex_lifecycle", {
                            "reason": "recovery_state_unavailable"})
                        return outcome("recovery_state_unavailable", success=False)
                    while not settled():
                        if finished() or clock() >= deadline:
                            break
                        budget = budget_reporter.update()
                        if budget.get("revoked") or budget.get("stop_reason"):
                            return outcome("codex_exited", exit_code=1, success=False)
                        time.sleep(1)
                    if finished() or clock() >= deadline:
                        continue
                    resume_prompt += (
                        " Some CLI tool completion events were lost. Host tool calls have "
                        "now settled; this does not imply they succeeded. Read bench.status "
                        "and the recorded episode results before deciding what to do. "
                        "Do not rerun the previous submission merely to obtain its result.")
                    _append(timeline, "codex_recovery_settled", {
                        "pending_tool_ids": sorted(pending_tools)})
                    pending_tools.clear()
                supervisor.terminate(handle)
                argv = [str(profile.codex), "exec", "--sandbox", "danger-full-access",
                        "resume", "--json", "--skip-git-repo-check", thread_id, "-"]
                try:
                    launch = supervisor.launch_codex(
                        argv, gateway=prepared["gateway"], cwd=prepared["cwd"],
                        connection_env=prepared["connection_env"], stdin_text=resume_prompt)
                except Exception as error:
                    _append(timeline, "codex_lifecycle", {"reason": "recovery_launch_failed",
                            "error": _redact(str(error), evidence_secrets)})
                    return outcome("recovery_launch_failed", success=False)
                handle = launch["handle"]
                stdout_pending, failure_message = "", ""
                continue
            _append(timeline, "codex_lifecycle", {"reason": reason,
                                                   "exit_code": state.get("exit_code")})
            return outcome(reason, exit_code=state.get("exit_code"),
                           success=False if reason == "invalid_jsonl" else None)
        if poll_interval:
            time.sleep(poll_interval)


__all__ = ["prepare_codex_run", "run_codex"]
