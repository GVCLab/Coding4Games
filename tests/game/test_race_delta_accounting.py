"""Ordered observations and repeated reads preserve TrackMania score accounting."""
from __future__ import annotations

import pytest

from coding4game_gui.benchmark.windows.scorers.race import RaceScorer

# A03-Race as the scenario declares it.
SPEC = {
    "calibrated_client_size": [1280, 720],
    "finish_floor": 0.6,
    "regions": {n: {"rect": [0, 0, 10, 10]}
                for n in ("clock", "distance", "finish_time", "track", "finish_clock", "finish_banner")},
    "track": {"gold_seconds": 19.84, "silver_seconds": 22.29,
              "bronze_seconds": 28.61, "track_length_m": 1521},
}
LENGTH = SPEC["track"]["track_length_m"]

# The run's own shape: 10 tics per act, a frame per tic, scan every 3rd.
TICS_PER_ACT = 10
SCAN_TICS = 3


class _Frame:
    """One captured frame, carrying what OCR would have read off it."""

    def __init__(self, clock: float | None, distance: int | None):
        self.clock, self.distance = clock, distance


class _Scorer(RaceScorer):
    """The real logic, reading _Frame objects instead of pixels."""

    def __init__(self, spec=None):
        super().__init__(spec or SPEC, (1280, 720), ocr=lambda crop: [])

    def _text_in(self, frame, name):
        if name == "clock":
            v = frame.clock
            text = "" if v is None else f"{int(v // 60)}:{v % 60:05.2f}"
        elif name == "distance":
            v = frame.distance
            text = "" if v is None else f"{v}m"
        else:
            text = ""
        self._texts[name] = text
        return text


def act(readings: list[tuple]) -> tuple[list[_Frame], _Frame]:
    """One act: the per-tic sink, and the SEPARATE capture taken after it.

    Two objects, not one, and that distinction is the harness earning its
    keep. The env grabs `pil` after the act finishes and reads that, while the
    sink holds the frames captured during it; the scorer caches on frame
    IDENTITY, so making the settled frame the last sink entry would suppress
    a read the real env performs. A finish that needs one more confirming read
    than the scan provides is exactly the case these tests are about.
    """
    return [_Frame(c, d) for c, d in readings], _Frame(*readings[-1])


def run_episode(scorer: _Scorer, acts: list[tuple[list[_Frame], _Frame]],
                scan_tics: int = SCAN_TICS) -> tuple[float, bool]:
    """WindowsGameEnv.act's frame handling, reproduced."""
    total, done = 0.0, False
    for sink, settled in acts:
        frame = settled
        if scan_tics and sink and len(sink) > scan_tics:
            for i in range(0, len(sink), scan_tics):
                if scorer.is_terminal(sink[i]):
                    frame = sink[i]
                    break
        total += scorer.delta(frame)
        done = bool(scorer.is_terminal(frame))
        if done:
            break
    return total, done


def straight_drive(acts: int, metres_per_tic: int = 4
                   ) -> list[tuple[list[_Frame], _Frame]]:
    """A car at 40 m/s, one frame per tic — the human lap's 42.7 m/s pace."""
    out, tic = [], 0
    for _ in range(acts):
        readings = []
        for _ in range(TICS_PER_ACT):
            tic += 1
            readings.append((round(tic * 0.1, 2), tic * metres_per_tic))
        out.append(act(readings))
    return out


# ---------------------------------------------------------------- the invariant
def test_the_total_reflects_the_whole_drive_not_the_last_tic():
    """The measured symptom, stated as a number.

    Ten acts at 4 m/tic is 400 m of a 1521 m track. Crediting only the last
    scan stride of each act would report about a tenth of that -- which is
    what the run did.
    """
    s = _Scorer()
    total, _ = run_episode(s, straight_drive(10))
    assert s.best_distance == 400
    expected = (400 / LENGTH) * 0.6 * 0.9
    assert total == pytest.approx(expected, rel=1e-6), total
    assert total == pytest.approx(s.total)


def test_delta_does_not_pay_twice_for_one_frame():
    """The other direction: a private baseline must not turn a repeated call
    into repeated income."""
    s = _Scorer()
    frame = _Frame(1.0, 100)
    first = s.delta(frame)
    assert first > 0
    assert s.delta(frame) == 0.0


def test_phase_readers_cannot_change_the_total():
    plain = _Scorer()
    total_plain, _ = run_episode(plain, straight_drive(6), scan_tics=0)

    watched = _Scorer()
    total_watched = 0.0
    for sink, settled in straight_drive(6):
        for f in sink:                       # every tic read by someone else
            watched.classify_phase(f)
        total_watched += watched.delta(settled)
    assert total_watched == pytest.approx(total_plain)
    assert total_watched == pytest.approx(watched.total)
