"""Check the Direct progress stream, opt-out, and terminal colors."""
import io

import pytest

from coding4game_gui.benchmark.evidence import progress

@pytest.fixture
def feed(monkeypatch):
    """Capture narration into a StringIO; on by default, color off (not a tty)."""
    monkeypatch.delenv("C4G_PROGRESS", raising=False)
    monkeypatch.delenv("C4G_PROGRESS_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    buf = io.StringIO()
    progress.set_stream(buf)
    try:
        yield buf
    finally:
        progress.set_stream(None)


def test_reports_direct_episode_progress(feed):
    progress.banner("DIRECT", "balatro")
    progress.note("Starting the selected task")
    progress.play_start(7, 0, 3, label="episode")
    progress.play_done(7, 42.0, 130, 5, label="episode")
    out = feed.getvalue()
    assert "DIRECT" in out and "balatro" in out
    assert "Starting the selected task" in out
    assert "episode 1/3" in out and "episode 7" in out
    assert "score +42.0" in out and "5 calls, 130 tics" in out
    assert "\033[" not in out                     # no ANSI on a non-tty stream


def test_disabled_via_env_emits_nothing(feed, monkeypatch):
    monkeypatch.setenv("C4G_PROGRESS", "0")
    progress.banner("DIRECT")
    progress.note("anything")
    assert feed.getvalue() == ""
