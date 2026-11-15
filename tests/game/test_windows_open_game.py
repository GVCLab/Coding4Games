import numpy as np
import pytest
import threading
import time
import json

from coding4game_gui.benchmark.windows.session import GameSession


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeGame:
    """External game substitute; no Windows apps or model calls."""
    TICRATE = 10
    def __init__(self):
        self.tic = 0
        self.total_reward = 0.0
        self.done = False
        self.end_reason = None
        self.closed = False
        self.actions = []
        self.closed_event = threading.Event()
        self.halted_event = threading.Event()
        self.close_calls = 0
        self.fail_reset = False

    def reset(self, episode_id=0):
        if self.fail_reset:
            raise RuntimeError("startup failed")
        self.tic = 0

    def frame(self):
        return np.zeros((10, 20, 3), dtype=np.uint8)

    def window_info(self):
        return {"hwnd": 123, "client_rect": [0, 0, 20, 10]}

    def validate_action_plan(self, acts):
        if any(buttons != ["NEUTRAL"] for buttons, _ in acts):
            raise ValueError("invalid controls")

    def act(self, buttons, tics):
        self.actions.append((buttons, tics))
        self.tic += tics
        self.total_reward += tics
        return float(tics)

    def halt_actions(self):
        self.halted_event.set()

    def close(self):
        self.close_calls += 1
        self.closed = True
        self.closed_event.set()


def test_stepped_game_clamps_budget_and_rejects_invalid_plan_without_play(tmp_path):
    game = FakeGame()
    with GameSession(lambda: game, tmp_path, tic_budget=5, max_tics=20) as session:
        first = session.start()
        uid = first["episode_id"]
        with pytest.raises(ValueError):
            session.step(uid, ["BAD"], 2)
        assert session.status()["tics_used"] == 0
        result = session.step(uid, ["NEUTRAL"], 9)
        assert result["tics"] == 5
        assert result["done"] is True
        assert game.actions == [(["NEUTRAL"], 5)]
        assert session.status()["tics_remaining"] == 0
        assert result['budget_stop_reason'] == 'tic_budget_exhausted'
        assert '5 / 5' in result['budget_message']
        assert "This run's tic limit has been reached" in result['budget_message']
        assert 'prepare and submit your program' in result['budget_message']
        assert 'Gameplay cannot continue' in session.status()['budget_message']
        assert session.observe(uid)['budget_message'] == result['budget_message']
        with pytest.raises(RuntimeError, match='tic_budget_exhausted.*5 / 5'):
            session.start()
        with pytest.raises(RuntimeError, match='tic_budget_exhausted.*5 / 5'):
            session.step(uid, ['NEUTRAL'], 1)
        assert game.closed


def test_episode_limit_is_not_the_experiment_budget(tmp_path):
    with GameSession(FakeGame, tmp_path, tic_budget=10, max_tics=2) as session:
        uid = session.start()['episode_id']
        result = session.step(uid, ['NEUTRAL'], 3)
        assert result['budget_stop_reason'] == 'episode_tic_limit'
        assert '2 / 10' in result['budget_message']
        assert "This episode's tic limit has been reached" in result['budget_message']
        assert 'you may still start the next episode' in result['budget_message']
        assert session.status()['budget_stop_reason'] is None
        assert session.start()['episode_id'] != uid


def test_expired_game_start_reports_wall_limit_without_launch(tmp_path):
    with GameSession(lambda: pytest.fail('No launch after deadline'), tmp_path,
                     tic_budget=10, deadline=1, clock=lambda:2) as session:
        with pytest.raises(RuntimeError, match='wall_budget_exhausted'):
            session.start()
        assert session.status()['budget_stop_reason'] == 'wall_budget_exhausted'
        assert "This run's time limit has been reached" in session.status()['budget_message']


def test_deadline_freezes_game_before_waiting_for_observation(tmp_path):
    clock, game = Clock(), FakeGame()
    with GameSession(lambda: game, tmp_path, tic_budget=10, deadline=1,
                     clock=clock, monitor_interval=.01) as session:
        uid = session.start()["episode_id"]
        entered, release = threading.Event(), threading.Event()
        original_frame = game.frame

        def blocked_frame():
            entered.set()
            release.wait(2)
            return original_frame()

        game.frame = blocked_frame
        observe = threading.Thread(target=session.observe, args=(uid,))
        observe.start()
        try:
            assert entered.wait(1)
            clock.now = 1
            assert game.halted_event.wait(1)
            assert not game.closed
        finally:
            release.set()
            observe.join(2)
        assert game.closed_event.wait(1)
        row = session.observe(uid)
        assert row["end_reason"] == "wall_budget" and row["tics"] == 0


def test_replay_uses_only_the_recorded_observations(tmp_path):
    import base64

    from coding4game_gui.benchmark.code.open_session import OpenSession

    with GameSession(FakeGame, tmp_path, tic_budget=12) as game:
        session = OpenSession(game, None, None, None, "python")
        initial = game.start()
        uid = initial["episode_id"]
        step = game.step(uid, ["NEUTRAL"], 3)
        game.stop(uid)

        replay = session.dispatch("bench.replay", {"uid": uid, "start": 0, "end": 6},
                                  phase="development")
        assert [frame["tic"] for frame in replay["frames"]] == [0, 3]
        for frame, original in zip(replay["frames"], [initial, step], strict=True):
            assert frame["observation"]["mimeType"] == "image/png"
            path = tmp_path / original["observation_path"]
            assert base64.b64decode(frame["observation"]["data"]) == path.read_bytes()


def test_stepped_action_is_clamped_by_remaining_wall_time(tmp_path):
    clock, game = Clock(), FakeGame()
    with GameSession(lambda: game, tmp_path, tic_budget=20, deadline=.25,
                     clock=clock) as session:
        uid = session.start()["episode_id"]
        result = session.step(uid, ["NEUTRAL"], 9)
    assert game.actions == [(["NEUTRAL"], 2)]
    assert result["tics"] == 2
    assert result["done"] and result["end_reason"] == "wall_budget"


def test_startup_failure_closes_game_and_close_is_idempotent(tmp_path):
    game = FakeGame()
    game.fail_reset = True
    session = GameSession(lambda: game, tmp_path, tic_budget=5)
    with pytest.raises(RuntimeError, match="startup failed"):
        session.start()
    assert game.closed and session.status()["active_episode"] is None
    assert game.close_calls == 3
    session.close(); session.close()
    assert game.close_calls == 3


def test_status_discloses_cooperative_wall_enforcement(tmp_path):
    session = GameSession(lambda: FakeGame(), tmp_path, tic_budget=5)
    try:
        status = session.status()
    finally:
        session.close()
    assert status["wall_enforcement"]["kind"] == "cooperative_host_threads"
    assert status["wall_enforcement"]["strict"] is False
    assert "capture" in status["wall_enforcement"]["limitation"]


def test_unledgered_journal_prefix_is_recovered_once_with_same_uid(tmp_path):
    from coding4game_gui.benchmark.evidence import episode_journal
    uid = "interrupted-uid"
    path = tmp_path / "windows_episode_journal.jsonl"
    episode_journal.append(path, "begin", uid, episode_id=uid, tics=0, score=0)
    episode_journal.append(path, "progress", uid, tics=4, score=3,
                           last_action={"actions": ["NEUTRAL"], "requested_tics": 4})
    first = GameSession(lambda: FakeGame(), tmp_path, tic_budget=10)
    try:
        evidence = first.evidence(uid)
        assert evidence["result"]["episode_uid"] == uid
        assert evidence["result"]["tics"] == 4
        assert evidence["result"]["outcome_kind"] == "infrastructure_error"
    finally:
        first.close()
    second = GameSession(lambda: FakeGame(), tmp_path, tic_budget=10)
    try:
        second_result = second.evidence(uid)["result"]
    finally:
        second.close()
    third = GameSession(lambda: FakeGame(), tmp_path, tic_budget=10)
    try:
        third_result = third.evidence(uid)["result"]
    finally:
        third.close()
    rows = (tmp_path / "episodes.jsonl").read_text().splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["episode_uid"] == uid
    for result in (second_result, third_result):
        assert result["score"] is None
        assert result["partial_score"] == 3
        assert result["done"] is True
        assert result["complete"] is False
        assert result["outcome_kind"] == "infrastructure_error"


def test_existing_result_ledger_wins_when_journal_ledger_marker_was_lost(tmp_path):
    from coding4game_gui.benchmark.evidence import episode_journal
    uid = "valid-result-uid"
    authoritative = {
        "episode_uid": uid, "episode_id": uid, "tics": 5, "score": 11,
        "done": True, "complete": True, "outcome_kind": "valid",
        "end_reason": "terminal", "evidence_refs": []}
    (tmp_path / "episodes.jsonl").write_text(
        json.dumps(authoritative) + "\n", encoding="utf-8")
    episode_journal.append(tmp_path / "windows_episode_journal.jsonl", "begin",
                           uid, episode_id=uid, tics=0, score=0)
    episode_journal.append(tmp_path / "windows_episode_journal.jsonl", "progress",
                           uid, tics=3, score=2)

    for _ in range(3):
        session = GameSession(lambda: FakeGame(), tmp_path, tic_budget=20)
        try:
            result = session.evidence(uid)["result"]
            assert {key: result[key] for key in authoritative} == authoritative
        finally:
            session.close()
    rows = (tmp_path / "episodes.jsonl").read_text().splitlines()
    assert len(rows) == 1


def test_stale_ids_and_repeated_stop_never_reuse_closed_environment(tmp_path):
    game = FakeGame()
    with GameSession(lambda: game, tmp_path, tic_budget=5) as session:
        uid = session.start()["episode_id"]
        result = session.stop(uid)
        assert session.stop(uid) == result
        with pytest.raises(ValueError, match="stale"):
            session.observe("not-this-episode")
    assert game.close_calls == 1


def test_step_failure_finalizes_actual_progress_as_infrastructure_error(tmp_path):
    class FailingGame(FakeGame):
        def act(self, buttons, tics):
            self.tic += 2
            self.total_reward = 7
            raise RuntimeError("driver failed")

    game = FailingGame()
    with GameSession(lambda: game, tmp_path, tic_budget=1) as session:
        uid = session.start()["episode_id"]
        with pytest.raises(RuntimeError, match="driver failed"):
            session.step(uid, ["NEUTRAL"], 4)
        result = session.observe(uid)
    assert result["tics"] == 2
    assert result["score"] is None and result["partial_score"] == 7
    assert result["outcome_kind"] == "infrastructure_error"
    assert result["budget_overshoot_tics"] == 1
    assert result["end_reason"] == "step_error"
    assert game.closed


def test_startup_retry_recovers_without_resetting_budget(tmp_path):
    games = []
    def factory():
        game = FakeGame()
        game.fail_reset = len(games) < 2
        games.append(game)
        return game
    with GameSession(factory, tmp_path, tic_budget=10) as session:
        result = session.start()
        assert len(games) == 3
        assert [g.closed for g in games] == [True, True, False]
        assert result['episode_index'] == 2
        assert session.status()['tics_remaining'] == 10
        assert [r['end_reason'] for r in session.status()['episodes']] == ['startup_error'] * 2
        assert all(r['score'] is None for r in session.status()['episodes'])
        session.step(result['episode_id'], ['NEUTRAL'], 2)
        assert session.status()['tics_remaining'] == 8


def test_startup_retry_is_bounded(tmp_path):
    games = []
    def factory():
        game = FakeGame()
        game.fail_reset = True
        games.append(game)
        return game
    with GameSession(factory, tmp_path, tic_budget=10) as session:
        with pytest.raises(RuntimeError, match='startup_retries_exhausted'):
            session.start()
        assert len(games) == 3
        assert all(g.closed for g in games)
        with pytest.raises(RuntimeError):
            session.start()
        assert len(games) == 3


def test_pending_steam_launch_closes_session_without_outer_retry(tmp_path):
    from coding4game_gui.benchmark.windows.game_launch import SteamLaunchPendingError
    calls = []
    def factory():
        calls.append('launch')
        raise SteamLaunchPendingError('Steam launch is unresolved; no retry')
    with GameSession(factory, tmp_path, tic_budget=10) as session:
        with pytest.raises(RuntimeError, match='steam_launch_pending'):
            session.start()
        assert calls == ['launch']
        assert len(session.status()['episodes']) == 1
        assert session.status()['episodes'][0]['score'] is None
        with pytest.raises(RuntimeError, match='session_closed'):
            session.start()
        assert calls == ['launch']


def test_startup_retry_respects_deadline(tmp_path):
    clock = Clock()
    games = []
    def factory():
        game = FakeGame()
        game.fail_reset = True
        games.append(game)
        clock.now = 10
        return game
    with GameSession(factory, tmp_path, tic_budget=10, deadline=5, clock=clock) as session:
        with pytest.raises(RuntimeError, match='wall_budget_exhausted'):
            session.start()
        assert len(games) == 1


def test_startup_interrupt_is_not_retried(tmp_path):
    calls = []
    def factory():
        calls.append(1)
        raise KeyboardInterrupt()
    with GameSession(factory, tmp_path, tic_budget=10) as session:
        with pytest.raises(KeyboardInterrupt):
            session.start()
        assert calls == [1]


def test_retry_charges_failed_attempt_and_reduces_next_cap(tmp_path):
    class PartialFailure(FakeGame):
        def reset(self, episode_id=0):
            self.tic = 3
            raise RuntimeError('failure after progress')
    games = iter([PartialFailure(), FakeGame()])
    with GameSession(lambda: next(games), tmp_path, tic_budget=10) as session:
        result = session.start(max_tics=10)
        assert result['resolved_max_tics'] == 7
        assert session.status()['tics_used'] == 3
        assert session.status()['episodes'][0]['score'] is None


def test_cleanup_failure_prevents_retry(tmp_path):
    class DirtyFailure(FakeGame):
        def reset(self, episode_id=0):
            raise RuntimeError('startup failure')
        def close(self):
            raise RuntimeError('cleanup failure')
    calls = []
    def factory():
        calls.append(1)
        return DirtyFailure()
    with GameSession(factory, tmp_path, tic_budget=10) as session:
        with pytest.raises(RuntimeError, match='startup_cleanup_failed'):
            session.start()
        assert calls == [1]
        assert 'cleanup failure' in session.status()['episodes'][0]['cleanup_error']


def test_invalid_configuration_is_not_retried(tmp_path):
    from coding4game_gui.benchmark.windows.session import StartupRetriesExhausted
    calls = []
    def factory():
        calls.append(1)
        raise ValueError('invalid configuration')
    with GameSession(factory, tmp_path, tic_budget=10) as session:
        with pytest.raises(StartupRetriesExhausted, match='invalid configuration') as failed:
            session.start()
        assert isinstance(failed.value.__cause__, ValueError)
        assert calls == [1]


def test_invalid_start_arguments_remain_correctable_without_closing_session(tmp_path):
    with GameSession(FakeGame, tmp_path, tic_budget=10) as session:
        with pytest.raises(ValueError, match='game mode must be stepped'):
            session.start(mode='invalid')
        with pytest.raises(ValueError, match='positive integer'):
            session.start(max_tics=0)
        assert session.status()['episodes'] == []
        assert session.start()['tics'] == 0


def test_environment_construction_failure_can_retry(tmp_path):
    calls = []
    def factory():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('process not found')
        return FakeGame()
    with GameSession(factory, tmp_path, tic_budget=10) as session:
        session.start()
        assert len(calls) == 2
        row = session.status()['episodes'][0]
        assert row['startup_error'] == 'RuntimeError: process not found'
        assert row['startup_attempt'] == 1
