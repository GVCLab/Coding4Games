import json

from coding4game_gui.benchmark.evidence.episode_journal import append
from coding4game_gui.benchmark.evidence.episode_journal import states
from coding4game_gui.benchmark.evidence.episode_journal import unfinished


def test_journal_recovers_latest_progress_and_ignores_torn_tail(tmp_path):
    path = tmp_path / "journal.jsonl"
    append(path, "begin", "u1", episode_id=4, tics=0,
           resolved_max_burst=4)
    append(path, "progress", "u1", tics=37, calls=2,
           actions=[[0, ["A"], 2, 0.0]])
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"event":"progress"')

    state = states(path)["u1"]
    assert state["episode_id"] == 4
    assert state["tics"] == 37
    assert state["resolved_max_burst"] == 4
    assert [row["episode_uid"] for row in unfinished(path)] == ["u1"]


def test_journal_ledgered_event_makes_recovery_idempotent(tmp_path):
    path = tmp_path / "journal.jsonl"
    append(path, "begin", "u1", episode_id=4, tics=0)
    append(path, "result", "u1", tics=11, score=3.0)
    append(path, "ledgered", "u1")

    assert unfinished(path) == []
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["begin", "result", "ledgered"]
