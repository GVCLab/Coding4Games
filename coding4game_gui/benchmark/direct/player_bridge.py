"""Direct-player transport and independent provider-visible attempt logging."""
from __future__ import annotations
import json, os, time, uuid
from pathlib import Path
from coding4game_gui.benchmark.evidence._lock import locked

def _log_llm_call(role, model, system, messages, reply, tokens_in,
                  tokens_out, *, call_status: str = "success",
                  logical_call_id: str | None = None, attempt: int = 1,
                  error_type: str | None = None) -> None:
    """Record the full provider-visible request and the outcome of an attempt."""
    path = os.environ.get("C4G_LLM_LOG")
    if not path:
        return
    try:
        from coding4game_gui.benchmark.evidence.call_context import current_episode
        ep = current_episode() or {}
        record = {"ts": time.time(), "role": role,
                  "phase": os.environ.get("C4G_LLM_PHASE", ""),
                  **({"episode_id": ep.get("episode_id")}
                     if "episode_id" in ep else
                     ({"seed": ep.get("seed")} if "seed" in ep else {})),
                  "ep": ep.get("ep"),
                  "episode_uid": ep.get("uid"),
                  "model": model, "system": system,
                  "messages": messages, "reply": reply,
                  "tokens_in": tokens_in, "tokens_out": tokens_out,
                  "call_status": call_status,
                  "logical_call_id": logical_call_id or uuid.uuid4().hex,
                  "attempt": int(attempt), "error_type": error_type}
        line = json.dumps(record, ensure_ascii=False)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            with locked(f):
                f.write(line + "\n")
                f.flush()  # push bytes to the fd WHILE the lock is held
    except Exception:  # noqa: BLE001 — logging is best-effort, never fatal
        pass


def _player_bridge(model):
    """A contained LiteLLM Player bridge with typed transport failures.

    Infrastructure failures are raised, never returned as assistant text for
    the ACT parser to misclassify as a model-policy failure.

    Every call is also recorded by `_log_llm_call` (see `C4G_LLM_LOG`)."""
    from coding4game_gui.benchmark.direct.llm import PlayerInfrastructureError
    from coding4game_gui.benchmark.direct.llm import call_llm
    from coding4game_gui.benchmark.evidence.event_log import emit
    from coding4game_gui.benchmark.evidence.event_log import emit_error
    backend = "litellm"

    def bridge(_m, system, messages, **kw):
        last: Exception | None = None
        result: tuple | None = None
        attempts = 0
        logical_call_id = uuid.uuid4().hex
        emit("player_call", "begin", backend=backend, model=model,
             message_count=len(messages))

        def observe_retry(event: dict) -> None:
            nonlocal attempts
            kind = event["event"]
            attempt = int(event["attempt"])
            if kind == "attempt":
                attempts = attempt
                emit("player_attempt", "begin", backend=backend,
                     model=model, attempt=attempt,
                     max_attempts=event["max_attempts"])
            elif kind == "failure":
                emit_error("player_attempt", event["exception"],
                           backend=backend, model=model, attempt=attempt,
                           transient=event["transient"])
            elif kind == "retry":
                exc = event["exception"]
                _log_llm_call(
                    "player", model, system, messages, "", 0, 0,
                    call_status="retryable_error",
                    logical_call_id=logical_call_id, attempt=attempt,
                    error_type=type(exc).__name__)
                emit("player_retry", "wait", backend=backend, model=model,
                     attempt=attempt, delay_s=event["delay_s"])

        try:
            kw.pop("timeout_s", None)
            result = call_llm(model, system, messages,
                              retry_observer=observe_retry, **kw)
        except Exception as e:  # noqa: BLE001 — containment is the point
            last = e
            attempts = max(1, attempts)
        if result is None:
            detail = f"{type(last).__name__}: {last}"
            _log_llm_call(
                "player", model, system, messages, "", 0, 0,
                call_status="fatal_error", logical_call_id=logical_call_id,
                attempt=max(1, attempts),
                error_type=type(last).__name__ if last is not None else None)
            failure = PlayerInfrastructureError(detail, attempts=attempts)
            emit_error("player_call", failure, backend=backend, model=model,
                       attempts=attempts, outcome_kind="infrastructure_error")
            raise failure from last
        attempts = max(1, attempts)
        emit("player_attempt", "end", backend=backend, model=model,
             attempt=attempts, tokens_in=result[1], tokens_out=result[2])
        _log_llm_call(
            "player", model, system, messages, *result,
            call_status="success", logical_call_id=logical_call_id,
            attempt=attempts)
        emit("player_call", "end", backend=backend, model=model,
             outcome_kind="valid", tokens_in=result[1], tokens_out=result[2])
        return result
    return bridge
