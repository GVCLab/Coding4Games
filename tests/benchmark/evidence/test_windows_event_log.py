"""Structured Windows lifecycle log: durable boundaries, never run behavior."""
from __future__ import annotations

import json

import pytest


def _rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_event_log_is_disabled_without_its_environment_variable(
        tmp_path, monkeypatch):
    from coding4game_gui.benchmark.evidence import event_log

    monkeypatch.delenv(event_log.ENV_VAR, raising=False)
    event_log.emit("should_not_exist", "begin")

    assert not (tmp_path / "windows_events.jsonl").exists()


def test_event_is_fsynced_and_tagged_with_episode_context(tmp_path, monkeypatch):
    from coding4game_gui.benchmark.evidence import call_context
    from coding4game_gui.benchmark.evidence import event_log

    log = tmp_path / "run_123" / "windows_events.jsonl"
    fsync_calls = []
    monkeypatch.setenv(event_log.ENV_VAR, str(log))
    monkeypatch.setenv("C4G_LLM_PHASE", "search")
    monkeypatch.setattr(event_log.os, "fsync", lambda fd: fsync_calls.append(fd))
    call_context.set_episode(seed=None, ep=2, uid="deadbeef", episode_id=17)
    try:
        event_log.emit("player_request", "begin", attempt=1, model="m")
    finally:
        call_context.clear_episode()

    rows = _rows(log)
    assert len(rows) == 1
    expected = {
        "event": "player_request",
        "state": "begin",
        "phase": "search",
        "run_id": "run_123",
        "ep": 2,
        "episode_id": 17,
        "episode_uid": "deadbeef",
        "attempt": 1,
        "model": "m",
    }
    assert {key: rows[0][key] for key in expected} == expected
    assert isinstance(rows[0]["pid"], int)
    assert fsync_calls, "a lifecycle boundary was flushed but not fsync'd"


def test_span_records_matching_end_and_duration(tmp_path, monkeypatch):
    from coding4game_gui.benchmark.evidence import event_log

    log = tmp_path / "windows_events.jsonl"
    monkeypatch.setenv(event_log.ENV_VAR, str(log))

    with event_log.span("start_check", episode_id=3):
        pass

    rows = _rows(log)
    assert [(r["event"], r["state"]) for r in rows] == [
        ("start_check", "begin"),
        ("start_check", "end"),
    ]
    assert rows[-1]["elapsed_s"] >= 0


def test_span_records_error_before_reraising(tmp_path, monkeypatch):
    from coding4game_gui.benchmark.evidence import event_log

    log = tmp_path / "windows_events.jsonl"
    monkeypatch.setenv(event_log.ENV_VAR, str(log))

    with pytest.raises(ValueError, match="signature must be a string"):
        with event_log.span("player_request", attempt=1):
            raise ValueError("signature must be a string")

    rows = _rows(log)
    assert rows[-1]["state"] == "error"
    assert rows[-1]["error_type"] == "ValueError"
    assert rows[-1]["error"] == "signature must be a string"


def test_player_bridge_records_each_attempt_and_terminal_error(
        tmp_path, monkeypatch):
    from coding4game_gui.benchmark.direct import player_bridge as bench_helper
    from coding4game_gui.benchmark.evidence import event_log
    import litellm

    log = tmp_path / "windows_events.jsonl"
    monkeypatch.setenv(event_log.ENV_VAR, str(log))
    monkeypatch.delenv("C4G_LLM_LOG", raising=False)
    calls = []

    async def fail(**_kwargs):
        calls.append(1)
        raise ValueError("ThinkingBlock.signature: None")

    monkeypatch.setattr(litellm.anthropic_interface.messages, "acreate", fail)
    bridge = bench_helper._player_bridge("test-model")

    from coding4game_gui.benchmark.direct.llm import PlayerInfrastructureError
    with pytest.raises(PlayerInfrastructureError, match="ThinkingBlock.signature"):
        bridge("ignored", "system", [{"role": "user", "content": "frame"}])

    assert len(calls) == 1  # schema/config failures are permanent, not retried
    rows = _rows(log)
    attempts = [r for r in rows if r["event"] == "player_attempt"]
    assert [(r["state"], r["attempt"]) for r in attempts] == [
        ("begin", 1), ("error", 1),
    ]
    assert rows[0]["event"] == "player_call"
    assert rows[0]["state"] == "begin"
    assert rows[-1]["event"] == "player_call"
    assert rows[-1]["state"] == "error"
    assert rows[-1]["error_type"] == "PlayerInfrastructureError"


def test_no_act_episode_records_reason_before_cleanup(tmp_path, monkeypatch):
    import numpy as np

    from coding4game_gui.benchmark.direct.player import play_one
    from coding4game_gui.benchmark.evidence import event_log

    log = tmp_path / "windows_events.jsonl"
    monkeypatch.setenv(event_log.ENV_VAR, str(log))

    class FakeWindowsEnv:
        done = False
        tic = 0
        total_reward = 0.0
        end_reason = None

        def __init__(self):
            self.finished = False

        def reset(self, episode_id=0):
            self.episode_id = episode_id

        def frame(self):
            return np.zeros((4, 4, 3), dtype=np.uint8)

        def finish_episode(self):
            self.finished = True
            self.done = True

    env = FakeWindowsEnv()
    result = play_one(env, lambda *_args, **_kwargs: ("not an action", 3, 1), "test-model", "notes",
                      max_burst=10, max_calls_per_ep=None, episode_id=7)

    assert result.error == "no ACT lines parsed"
    assert result.calls == 3
    assert result.invalid_action_replies == 3
    assert result.end_reason == "invalid_action_replies_exhausted"
    assert env.finished is True
    rows = _rows(log)
    parse = [r for r in rows if r["event"] == "player_reply_parse"]
    assert len(parse) == 3
    assert [r["state"] for r in parse] == ["error", "error", "error"]
    assert [r["recoverable"] for r in parse] == [True, True, False]
    assert [r["strike"] for r in parse] == [1, 2, 3]
    cleanup = [r for r in rows if r["event"] == "episode_cleanup"]
    assert [(r["state"], r["source"]) for r in cleanup] == [
        ("begin", "play_one.finally"),
        ("end", "play_one.finally"),
    ]
