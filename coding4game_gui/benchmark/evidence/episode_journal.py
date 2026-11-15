"""Durable, append-only progress journal for Windows benchmark episodes.

Flush each transition so an interrupted episode can be recovered from its
recorded progress and charged for the tics it used.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from coding4game_gui.benchmark.evidence._lock import locked


def append(path: Path | str | None, event: str, episode_uid: str,
           **fields) -> None:
    if path is None:
        return
    journal = Path(path)
    journal.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.time(), "event": event, "episode_uid": episode_uid,
        **fields,
    }
    with journal.open("a", encoding="utf-8") as stream:
        with locked(stream):
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def states(path: Path | str) -> dict[str, dict]:
    """Fold the journal into the latest durable state for each episode UID."""
    result: dict[str, dict] = {}
    journal = Path(path)
    if not journal.is_file():
        return result
    with journal.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # A process may die during its final append. Earlier flushed
                # rows remain sufficient to recover a conservative prefix.
                continue
            uid = row.get("episode_uid")
            if not uid:
                continue
            state = result.setdefault(uid, {"episode_uid": uid})
            state.update({k: v for k, v in row.items()
                          if k not in {"ts", "event"}})
            state["journal_event"] = row.get("event")
            state["journal_ts"] = row.get("ts")
            if row.get("event") == "ledgered":
                state["ledgered"] = True
    return result


def unfinished(path: Path | str) -> list[dict]:
    return [state for state in states(path).values()
            if not state.get("ledgered")]
