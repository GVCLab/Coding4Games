"""Thread-local episode context for the LLM-call log.

`play_one` runs one episode in one (possibly worker) thread and calls the LLM
bridge synchronously within it; `_log_llm_call` runs in that same thread. So a
thread-local lets play_one stamp each PLAYER call with the episode it belongs to
(seed / ep index / a stable uid) WITHOUT threading params through the bridge —
and concurrent play_episodes workers never collide (each thread has its own).
Coach calls (no play_one) leave it unset, so they log no episode and group on
their own in the viewer.
"""
from __future__ import annotations

import threading

_ctx = threading.local()


def set_episode(seed: int | None, ep: int, uid: str, *,
                episode_id: int | None = None) -> None:
    _ctx.episode = {"ep": ep, "uid": uid}
    if episode_id is not None:
        _ctx.episode["episode_id"] = episode_id
    else:
        _ctx.episode["seed"] = seed


def clear_episode() -> None:
    _ctx.episode = None


def current_episode() -> dict | None:
    return getattr(_ctx, "episode", None)
