"""Agent CLI adapters for the shared open-development protocol.

The public seam is deliberately small: prepare a selected CLI, then drive its
structured event stream.  Game access, budgets, submissions and runtime
ownership remain host concerns and do not vary by CLI.
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from coding4game_gui.benchmark.code.budget_status import BudgetReporter


AGENTS = ("codex", "claude")
CLAUDE_AUTO_COMPACT_ARGUMENT = "auto"
_MAX_JSONL_LINE = 4 * 1024 * 1024
_RECOVERY_DELAYS = (10, 30)
_TERMINAL_PREVIEW_CHARS = 2000


def _terminal_print(value):
    """Print an operator message without letting a legacy encoding stop a run."""
    text = str(value)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        stream = sys.stdout
        encoding = getattr(stream, "encoding", None) or "utf-8"
        escaped = text.encode(encoding, errors="backslashreplace").decode(encoding)
        stream.write(escaped + "\n")
        stream.flush()


def _preview(value, limit=_TERMINAL_PREVIEW_CHARS):
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [terminal preview truncated; {len(text) - limit} chars remain in evidence]"


def _content_preview(value):
    """Render tool results without ever expanding inline image/base64 payloads."""
    if isinstance(value, str):
        return _preview(value)
    if isinstance(value, list):
        parts = [_content_preview(item) for item in value]
        return _preview("\n".join(part for part in parts if part))
    if not isinstance(value, dict):
        return _preview(value)
    kind = value.get("type")
    if kind == "text":
        return _preview(value.get("text"))
    if kind == "image":
        source = value.get("source") or {}
        media = source.get("media_type") or value.get("mime_type") or "image"
        dimensions = value.get("dimensions") or {}
        size = (f" {dimensions.get('originalWidth')}x{dimensions.get('originalHeight')}"
                if dimensions.get("originalWidth") and dimensions.get("originalHeight") else "")
        return f"[{media}{size}; binary image retained in evidence]"
    if kind == "tool_result":
        return _content_preview(value.get("content"))
    # Avoid dumping unknown nested structures because they may contain binary data.
    return _preview(json.dumps({k: v for k, v in value.items()
                                if k not in {"data", "base64"}},
                               ensure_ascii=False, default=str))


def _print_agent_event(agent, event):
    """Mirror readable structured CLI events to the operator terminal."""
    kind = event.get("type")
    if kind == "assistant" and not event.get("is_api_error_message"):
        message = event.get("message") or {}
        for block in message.get("content", []) if isinstance(message, dict) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                value = _preview(block.get("text"))
                if value:
                    _terminal_print(f"[C4G {agent}] {value}")
            elif block.get("type") == "tool_use":
                value = _preview(json.dumps(block.get("input") or {}, ensure_ascii=False,
                                            separators=(",", ":"), default=str))
                _terminal_print(f"[C4G {agent} tool] {block.get('name', 'unknown')} {value}")
    elif kind == "user":
        message = event.get("message") or {}
        for block in message.get("content", []) if isinstance(message, dict) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            value = _content_preview(block.get("content"))
            if value:
                _terminal_print(f"[C4G tool result] {value}")
    elif kind == "system" and event.get("subtype") == "api_retry":
        _terminal_print(f"[C4G {agent}] API retry {event.get('attempt')}/{event.get('max_retries')}: "
                        f"{event.get('error_status')} {event.get('error')}")


def validate_effort(agent, effort):
    if effort is None:
        return
    supported = {'codex': {'low', 'medium', 'high', 'xhigh', 'max', 'ultra'},
                 'claude': {'low', 'medium', 'high', 'xhigh', 'max'}}
    if effort not in supported.get(agent, set()):
        raise ValueError(f"Unsupported reasoning effort {effort!r} for {agent}; "
                         f"supported: {sorted(supported.get(agent, set()))}. "
                         "Omit the option to use the CLI default.")


def protocol_for(agent: str) -> str:
    try:
        return {"codex": "responses", "claude": "anthropic"}[agent]
    except KeyError as exc:
        raise ValueError(f"unsupported agent CLI: {agent}") from exc


def _mcp(profile, client_path: str) -> dict:
    return {
        "command": str(profile.python),
        "args": ["-I", "-B", "-X", "utf8", client_path, "--mcp"],
        "env": {
            "C4G_BRIDGE_URL": "${C4G_BRIDGE_URL}",
            "C4G_BRIDGE_TOKEN": "${C4G_BRIDGE_TOKEN}",
            "C4G_BRIDGE_PHASE": "${C4G_BRIDGE_PHASE}",
        },
    }


@dataclass(frozen=True)
class PreparedAgent:
    agent: str
    argv: list[str]
    resume_prefix: list[str]
    stdin_text: str | None
    gateway: dict
    cwd: Path
    connection_env: dict


def _gateway(gateway) -> dict:
    return {"url": gateway.base_url, "token": gateway.token, "port": gateway.port,
            "model": gateway.model}


def _connection(bridge) -> dict:
    return {"C4G_BRIDGE_URL": bridge.url, "C4G_BRIDGE_TOKEN": bridge.token,
            "C4G_BRIDGE_PHASE": "development"}


def _stage_client(supervisor) -> str:
    from coding4game_gui.paths import runtime_source
    source = runtime_source("client.py")
    stage = getattr(supervisor, "stage_connection", supervisor.stage)
    folder = stage({"client.py": source, "c4g_client.py": source}, name="mcp")
    return supervisor.native_path(folder, "client.py") if hasattr(supervisor, "native_path") else str(Path(folder) / "client.py")


def prepare_agent_run(supervisor, profile, gateway, bridge, prompt: str,
                      *, agent: str, reasoning_effort=None) -> PreparedAgent:
    validate_effort(agent, reasoning_effort)
    if agent not in AGENTS:
        raise ValueError(f"unsupported agent CLI: {agent}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("agent prompt must be nonempty")
    if agent == "codex":
        from coding4game_gui.benchmark.code.codex_driver import prepare_codex_run
        value = prepare_codex_run(supervisor, profile, gateway, bridge, prompt,
                                  reasoning_effort=reasoning_effort)
        return PreparedAgent(agent, value["argv"], [], value["stdin_text"],
                             value["gateway"], value["cwd"], value["connection_env"])

    executable = str(profile.agent("claude"))
    client_path = _stage_client(supervisor)
    config = {"mcpServers": {"c4g_game": {"type": "stdio", **_mcp(profile, client_path)}}}
    if getattr(bridge, "observation_size", (1280, 768)) == (768, 461):
        config["mcpServers"]["c4g_game"]["args"].append("--claude-viewport")
    stage = getattr(supervisor, "stage_connection", supervisor.stage)
    folder = stage({
        "mcp.json": json.dumps(config, indent=2).encode("utf-8"),
        "settings.json": json.dumps({"permissions": {"defaultMode": "bypassPermissions"}},
                                    indent=2).encode("utf-8"),
    }, name=".claude")
    mcp_config = (supervisor.native_path(folder, "mcp.json")
                  if hasattr(supervisor, "native_path") else str(Path(folder) / "mcp.json"))
    argv = [executable, "--dangerously-skip-permissions",
            "--strict-mcp-config", "--mcp-config", mcp_config,
            "--model", gateway.model,
            "--autocompact", CLAUDE_AUTO_COMPACT_ARGUMENT,
            "--name", "Coding4Game development"]
    if reasoning_effort:
        argv += ["--effort", reasoning_effort]
    argv += ["--print", "--verbose", "--output-format", "stream-json"]
    resume = [*argv, "--resume"]
    return PreparedAgent(agent, argv, resume, prompt, _gateway(gateway),
                         supervisor.workspace, _connection(bridge))


def _claude_event_facts(event: dict) -> tuple[str | None, str | None, bool, list[tuple[str, str]]]:
    """Return session id, failure text, completion, and pending-tool mutation."""
    session = event.get("session_id")
    if session is not None:
        try:
            session = str(uuid.UUID(str(session)))
        except ValueError:
            session = None
    failure = None
    completed = False
    mutations = []
    kind = event.get("type")
    if kind == "result":
        completed = True
        if event.get("is_error"):
            failure = str(event.get("result") or event.get("error") or "Claude turn failed")
    message = event.get("message") or {}
    if event.get("is_api_error_message"):
        failure = " ".join(str(block.get("text", ""))
                           for block in message.get("content", [])
                           if isinstance(block, dict) and block.get("type") == "text")
        failure = failure or str(event.get("error") or "Claude API error")
    for block in message.get("content", []) if isinstance(message, dict) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("id"):
            mutations.append(("add", str(block["id"])))
        elif block.get("type") == "tool_result" and block.get("tool_use_id"):
            mutations.append(("remove", str(block["tool_use_id"])))
    return session, failure, completed, mutations


def _transient(message: str) -> bool:
    text = message.lower()
    if any(value in text for value in ("401", "403", "unauthorized", "forbidden",
                                       "budget_exhausted", "submission_selected")):
        return False
    return any(value in text for value in ("connection", "stream", "timed out", "timeout",
                                            "temporarily unavailable", "502", "503", "504"))


def run_agent(supervisor, profile, gateway, bridge, prompt, timeline, deadline, *,
              agent: str, clock=time.monotonic, poll_interval=.05,
              finished=lambda: False, tics_remaining=None, reasoning_effort=None,
              resume_session_id=None):
    """Run one selected CLI while preserving the shared development contract."""
    if agent == "codex":
        from coding4game_gui.benchmark.code.codex_driver import run_codex
        return run_codex(supervisor, profile, gateway, bridge, prompt, timeline, deadline,
                         clock=clock, poll_interval=poll_interval, finished=finished,
                         tics_remaining=tics_remaining, reasoning_effort=reasoning_effort,
                         **({"resume_session_id":resume_session_id} if resume_session_id is not None else {}))
    if agent != "claude":
        raise ValueError(f"unsupported agent CLI: {agent}")
    if resume_session_id is not None:
        if (not isinstance(resume_session_id, str)
                or str(uuid.UUID(resume_session_id)) != resume_session_id):
            raise ValueError("recovery requires the original canonical Claude session UUID")
    timeline.append("agent_viewport", details={
        "agent": agent, "observation_size": list(getattr(bridge, "observation_size", (1280, 768))),
        "canonical_image_size": [1280, 768],
        "pointer_mapping": "endpoint_preserving_per_axis",
        "python_client_coordinates": "same_as_mcp"})
    prepared = prepare_agent_run(
        supervisor, profile, gateway, bridge, prompt, agent=agent,
        reasoning_effort=reasoning_effort)

    def launch(argv, stdin_text):
        return supervisor.launch_agent(agent, argv, gateway=prepared.gateway,
                                       cwd=prepared.cwd,
                                       connection_env=prepared.connection_env,
                                       stdin_text=stdin_text)

    child = launch([*prepared.resume_prefix, resume_session_id], prompt) if resume_session_id else launch(prepared.argv, prepared.stdin_text)
    handle = child["handle"]
    pending = ""
    event_count = recovery_attempts = continuation_attempts = no_progress_exits = 0
    invalid_jsonl = False
    session_id, failure = resume_session_id or "", ""
    recovery_identity_observed = False
    pending_tools = set()
    last_api_notice = ""
    previous_remaining = tics_remaining() if tics_remaining else None
    secrets_to_redact = (str(gateway.token), str(bridge.token))
    root = Path(timeline.path).parent if getattr(timeline, "path", None) else None
    events_path = root / f"{agent}_events.jsonl" if root else None
    stderr_path = root / f"{agent}_stderr.log" if root else None
    reporter = BudgetReporter(gateway, timeline)

    def redact(text):
        for secret in secrets_to_redact:
            text = text.replace(secret, "[REDACTED]")
        return text

    def persist(path, text):
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="") as stream:
                stream.write(text)

    def append(kind, details, summary=""):
        timeline.append(kind, source="sandbox", summary=summary,
                        details={"origin": agent, **details})

    def outcome(reason, *, exit_code=None, explicit=False, success=None):
        budget = reporter.update()
        if reason == f"{agent}_exited" and budget.get("stop_reason"):
            reason, success = budget["stop_reason"], False
        return {"agent": agent, "reason": reason, "success": success,
                "session_id": session_id or None,
                "recovery_identity_observed": recovery_identity_observed,
                "explicit_finished": explicit, "exit_code": exit_code,
                "event_count": event_count, "recovery_attempts": recovery_attempts,
                "continuation_attempts": continuation_attempts,
                "error": redact(failure) or None,
                "budget_message": budget.get("budget_message"),
                "evidence": {"events": str(events_path) if events_path else None,
                             "stderr": str(stderr_path) if stderr_path else None}}

    def stdout(data):
        nonlocal pending, invalid_jsonl, event_count, session_id, failure, last_api_notice, recovery_identity_observed
        pending += data
        if "\n" not in pending and len(pending.encode("utf-8")) > _MAX_JSONL_LINE:
            return False
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > _MAX_JSONL_LINE:
                return False
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("event is not an object")
            except (json.JSONDecodeError, ValueError) as error:
                invalid_jsonl = True
                append("agent_diagnostic", {"data": redact(line),
                       "error": f"{type(error).__name__}: {error}"}, "invalid agent JSONL")
                continue
            safe = json.loads(redact(json.dumps(event, ensure_ascii=False,
                                                separators=(",", ":"))))
            _print_agent_event(agent, safe)
            found_session, found_failure, completed, mutations = _claude_event_facts(event)
            if found_session:
                if resume_session_id is not None and found_session != resume_session_id:
                    append("agent_recovery_identity_mismatch", {
                        "expected_session_id": resume_session_id, "observed_session_id": found_session})
                    gateway.revoke("session_identity_changed")
                    supervisor.terminate(handle)
                    raise RuntimeError("recovered Claude CLI session identity changed")
                session_id = found_session
                recovery_identity_observed = resume_session_id is not None
            if found_failure is not None:
                failure = found_failure
                notice = " ".join(redact(failure).split())
                if notice != last_api_notice:
                    _terminal_print(f"[C4G API {time.strftime('%H:%M:%S')} {agent}] {notice}")
                    last_api_notice = notice
            elif completed:
                failure = ""
                if last_api_notice:
                    _terminal_print(
                        f"[C4G API {time.strftime('%H:%M:%S')} {agent}] Recovered: model turn completed.")
                    last_api_notice = ""
            for mutation in mutations:
                (pending_tools.add if mutation[0] == "add" else pending_tools.discard)(mutation[1])
            event_count += 1
            persist(events_path, json.dumps(safe, ensure_ascii=False,
                                             separators=(",", ":")) + "\n")
            append("agent_event", safe, str(safe.get("type", "event")))
        return len(pending.encode("utf-8")) <= _MAX_JSONL_LINE

    while True:
        reporter.update()
        if finished():
            gateway.revoke("submission_selected")
            supervisor.terminate(handle)
            return outcome("explicit_finished", explicit=True)
        if clock() >= deadline:
            gateway.revoke("deadline_exhausted")
            supervisor.terminate(handle)
            return outcome("deadline", success=False)
        try:
            state = supervisor.poll(handle)
        except Exception as error:
            gateway.revoke()
            supervisor.terminate(handle)
            append("agent_lifecycle", {"reason": "supervisor_error",
                   "error": f"{type(error).__name__}: {error}"})
            return outcome("supervisor_error", success=False)
        overflow = bool(state.get("output_overflow"))
        for item in state.get("events", []):
            data = item.get("data", "")
            data = data if isinstance(data, str) else str(data)
            if item.get("stream") == "stdout":
                overflow = not stdout(data) or overflow
            else:
                safe = redact(data)
                persist(stderr_path, safe)
                append("agent_stderr", {**item, "data": safe}, f"{agent} stderr")
        if overflow:
            gateway.revoke()
            supervisor.terminate(handle)
            return outcome("output_overflow", success=False)
        if not state.get("running", False) and state.get("output_complete", True):
            if pending.strip():
                stdout("\n")
            if finished():
                gateway.revoke("submission_selected")
                return outcome("explicit_finished", exit_code=state.get("exit_code"), explicit=True)
            reason = "invalid_jsonl" if invalid_jsonl else f"{agent}_exited"
            budget = reporter.update()
            continue_task = False
            if (state.get("exit_code") == 0 and tics_remaining and not invalid_jsonl
                    and session_id and not pending_tools and not budget.get("revoked")
                    and not budget.get("stop_reason")):
                remaining = tics_remaining()
                no_progress_exits = 0 if remaining < previous_remaining else no_progress_exits + 1
                previous_remaining = remaining
                if no_progress_exits > 2:
                    return outcome("development_no_progress", exit_code=0, success=False)
                continue_task = True
            recover = (state.get("exit_code") not in (None, 0) and not invalid_jsonl
                       and session_id and not pending_tools and _transient(failure)
                       and recovery_attempts < len(_RECOVERY_DELAYS)
                       and not budget.get("revoked") and not budget.get("stop_reason"))
            if continue_task or recover:
                if continue_task:
                    continuation_attempts += 1
                    delay = 0
                    resume_prompt = (
                        "Development is not finished because no final program has been selected. "
                        f"{remaining:g} game tics remain available. Continue useful development "
                        "or explicitly select a saved candidate with submission.finish(id). "
                        "Unused tics do not block final selection; all budgets are upper bounds. "
                        "Do not replay completed actions or spend tics merely to exhaust a budget.")
                else:
                    delay = _RECOVERY_DELAYS[recovery_attempts]
                    recovery_attempts += 1
                    resume_prompt = ("The previous turn failed due to a temporary transport error. "
                                     "Continue the same task after inspecting current state. Do not replay completed actions.")
                for _ in range(delay):
                    if finished() or clock() >= deadline:
                        break
                    time.sleep(1)
                if finished() or clock() >= deadline:
                    continue
                supervisor.terminate(handle)
                argv = [*prepared.resume_prefix, session_id]
                stdin_text = resume_prompt
                child = launch(argv, stdin_text)
                handle = child["handle"]
                pending = failure = ""
                continue
            append("agent_lifecycle", {"reason": reason, "exit_code": state.get("exit_code")})
            return outcome(reason, exit_code=state.get("exit_code"),
                           success=False if invalid_jsonl else None)
        if poll_interval:
            time.sleep(poll_interval)


__all__ = [
    "AGENTS",
    "CLAUDE_AUTO_COMPACT_ARGUMENT",
    "PreparedAgent",
    "prepare_agent_run",
    "protocol_for",
    "run_agent",
]
