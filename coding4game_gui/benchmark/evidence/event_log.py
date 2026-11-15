"""Crash-durable structured events for real Windows benchmark runs.

``llm_calls.jsonl`` deliberately contains only completed model calls.  That is
the right contract for replay/audit, but it leaves no evidence when a process
is killed while it is launching a game, waiting for a player response, or
cleaning an episode up.  This module is the small, independent flight recorder
for those lifecycle boundaries.

The writer is best-effort and inactive unless :data:`ENV_VAR` points at a
file.  Every record is flushed and fsync'd while holding the same cross-process
lock used by the benchmark ledgers, so the last ``begin`` event survives a hard
termination and identifies the operation that was in flight.
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Iterator

from coding4game_gui.benchmark.evidence._lock import locked


ENV_VAR = "C4G_WINDOWS_EVENT_LOG"
MAX_ERROR_CHARS = 2000


def _episode_fields() -> dict[str, Any]:
    try:
        from coding4game_gui.benchmark.evidence.call_context import current_episode
        episode = dict(current_episode() or {})
        uid = episode.pop("uid", None)
        if uid is not None:
            episode["episode_uid"] = uid
        return episode
    except Exception:  # noqa: BLE001 - logging can never break a run
        return {}


def _error_fields(exc: BaseException) -> dict[str, str]:
    return {
        "error_type": type(exc).__name__,
        "error": str(exc)[:MAX_ERROR_CHARS],
    }


def emit(event: str, state: str = "instant", **details: Any) -> None:
    """Append one durable event, or do nothing when the log is disabled.

    Callers must pass metadata rather than prompts, images, replies, or
    credentials.  Full model payloads continue to belong exclusively to
    ``llm_calls.jsonl``.
    """
    raw_path = os.environ.get(ENV_VAR)
    if not raw_path:
        return
    try:
        path = Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        thread = threading.current_thread()
        record: dict[str, Any] = {
            "ts": now,
            "iso_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "event": str(event),
            "state": str(state),
            "pid": os.getpid(),
            "thread_id": threading.get_ident(),
            "thread_name": thread.name,
            "phase": os.environ.get("C4G_LLM_PHASE", ""),
            "run_id": path.parent.name,
            **_episode_fields(),
            **details,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as handle:
            with locked(handle):
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
    except Exception:  # noqa: BLE001 - diagnostics must never change behavior
        pass


@contextmanager
def span(event: str, **details: Any) -> Iterator[None]:
    """Emit durable ``begin`` and matching ``end``/``error`` records."""
    started = time.perf_counter()
    emit(event, "begin", **details)
    try:
        yield
    except BaseException as exc:
        emit(event, "error", elapsed_s=round(time.perf_counter() - started, 6),
             **details, **_error_fields(exc))
        raise
    else:
        emit(event, "end", elapsed_s=round(time.perf_counter() - started, 6),
             **details)


def emit_error(event: str, exc: BaseException, **details: Any) -> None:
    """Record an error caught outside a :func:`span`."""
    emit(event, "error", **details, **_error_fields(exc))


__all__ = ["ENV_VAR", "emit", "emit_error", "span"]
