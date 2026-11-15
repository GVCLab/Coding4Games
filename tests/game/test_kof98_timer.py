"""The round clock, and the two scoring branches that depend on it.

Every fixture here came off the production capture path -- either a frame of
`kof_human_rec2` or the evidence PNG a real run settled on -- so a test and the
code under test cannot agree with each other on a reference the game never
produced.

The calibration these tests pin is derived in
`coding4game_gui/benchmark/windows/scorers/kof98_timer_font.py`: the digit cells were found
by temporal variance over the recording and the glyphs were labelled by the
countdown itself. Nothing was annotated by hand, which is exactly why it needs
pinning -- a silently drifted ROI degrades every match instead of failing.
"""
from __future__ import annotations

import pathlib

import cv2
import numpy as np
import pytest
import yaml

from coding4game_gui.benchmark.windows.scorers.kof98_terminal import Kof98TerminalScorer
from coding4game_gui.benchmark.windows.scorers import kof98_timer_font as font

REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "kof98_terminal"
SCENARIO = FIXTURES / "mr_why_calibration.yaml"
CLIENT = (854, 480)


def frame(name: str) -> np.ndarray:
    img = cv2.imread(str(FIXTURES / name))
    assert img is not None, f"fixture missing: {name}"
    return img


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))["scorer"]


@pytest.fixture()
def scorer(spec) -> Kof98TerminalScorer:
    return Kof98TerminalScorer(spec, client_size=CLIENT)


def _live(s: Kof98TerminalScorer) -> None:
    """Skip the hand-over latch; these frames are all mid-match."""
    s._seen_live = True


# --------------------------------------------------------------------------
# the calibration itself
# --------------------------------------------------------------------------
def test_the_digit_cells_land_on_the_calibrated_pixels(scorer):
    """The fractions must resolve to the columns the glyphs were cut from.

    They are stored as fractions so they scale, but they were measured in
    pixels, and `int()` truncation means a fraction one ten-thousandth too
    small silently shifts a cell by a column. That does not fail anywhere; it
    just reads the wrong digit for the rest of the project.
    """
    w, h = CLIENT
    assert (int(scorer.timer_band[0] * h), int(scorer.timer_band[1] * h)) == (0, 33)
    assert (int(scorer.timer_tens[0] * w), int(scorer.timer_tens[1] * w)) == (402, 414)
    assert (int(scorer.timer_ones[0] * w), int(scorer.timer_ones[1] * w)) == (422, 433)


def test_every_glyph_reads_back_as_itself(scorer):
    for digit in range(10):
        bitmap = np.asarray(font.bitmap(digit), dtype=np.float32)
        distances = np.abs(scorer._digits - bitmap[None]).mean(axis=(1, 2))
        assert int(distances.argmin()) == digit
        assert distances[digit] == pytest.approx(0.0)


@pytest.mark.parametrize("name,expected", [
    ("clock_58_f00000.jpg", 58),
    ("clock_50_f00300.jpg", 50),
    ("clock_43_f00700.jpg", 43),
    ("turtle_clock56_854.jpg", 56),
    ("win_clock49_854.jpg", 49),
    ("loss_clock32_854.jpg", 32),
])
def test_the_clock_reads_its_recorded_value(scorer, name, expected):
    assert scorer.read_timer(frame(name)) == expected


def test_a_frame_with_no_clock_reads_nothing(scorer):
    """Absence must be reported as absence, never as a plausible number."""
    assert scorer.read_timer(frame("char_select_768.jpg")) is None
    assert scorer.read_timer(np.zeros((480, 854, 3), dtype=np.uint8)) is None


def test_elapsed_runs_the_right_way(scorer):
    """A larger reading is EARLIER in the round, so elapsed must fall."""
    early = scorer.elapsed_fraction(frame("clock_58_f00000.jpg"))
    late = scorer.elapsed_fraction(frame("clock_43_f00700.jpg"))
    assert early == pytest.approx((60 - 58) / 60)
    assert late == pytest.approx((60 - 43) / 60)
    assert late > early


# --------------------------------------------------------------------------
# the two scoring branches
# --------------------------------------------------------------------------
def test_a_decided_round_scores_by_the_health_difference(scorer):
    """One expression for every decided ending: (1 + own - opp) / 2."""
    _live(scorer)
    scorer.in_combat = lambda _f: True
    scorer.read_timer = lambda _f: 30

    scorer.read = lambda _f: (0.0, scorer.full_bar)          # KO'd, dealt nothing
    assert scorer.snapshot(None)["score"] == pytest.approx(0.0)
    scorer.read = lambda _f: (scorer.full_bar, 0.0)          # KO at full health
    assert scorer.snapshot(None)["score"] == pytest.approx(1.0)
    scorer.read = lambda _f: (0.0, scorer.full_bar / 2)      # KO'd, dealt half
    assert scorer.snapshot(None)["score"] == pytest.approx(0.25)


def test_losing_is_always_below_a_half_and_winning_always_above(scorer):
    _live(scorer)
    scorer.in_combat = lambda _f: True
    scorer.read_timer = lambda _f: 30
    for opp in (0.01, 0.25, 0.5, 0.99):
        scorer.read = lambda _f, o=opp: (0.0, o * scorer.full_bar)
        assert scorer.snapshot(None)["score"] < 0.5
    for own in (0.01, 0.25, 0.5, 0.99):
        scorer.read = lambda _f, m=own: (m * scorer.full_bar, 0.0)
        assert scorer.snapshot(None)["score"] > 0.5


def test_a_timed_out_round_is_decided_by_the_bars(scorer):
    """Neither bar is empty, so only the clock says the round is over."""
    _live(scorer)
    scorer.in_combat = lambda _f: True
    scorer.read_timer = lambda _f: 0
    scorer.read = lambda _f: (0.8 * scorer.full_bar, 0.3 * scorer.full_bar)
    reading = scorer.snapshot(None)
    assert reading["ending"] == "timeout"
    assert reading["score"] == pytest.approx((1 + 0.8 - 0.3) / 2)


def test_an_undecided_round_credits_damage_only(scorer):
    """Surviving is worth nothing until the round is actually decided."""
    _live(scorer)
    scorer.in_combat = lambda _f: True
    scorer.read_timer = lambda _f: 30                  # half the round gone
    scorer.read = lambda _f: (scorer.full_bar, scorer.full_bar)
    assert scorer.snapshot(None)["score"] == pytest.approx(0.0), (
        "full health with the opponent untouched must score zero")
    scorer.read = lambda _f: (scorer.full_bar, 0.5 * scorer.full_bar)
    assert scorer.snapshot(None)["score"] == pytest.approx(0.5 * 0.5 / 2)


def test_stopping_early_can_never_beat_playing_on(scorer):
    """The property the old rule lacked, stated as an inequality.

    `elapsed` rises with the round, and damage can only rise as a fight
    continues, so the score of a rollout cut short is bounded by the score of
    the same rollout carried further.
    """
    _live(scorer)
    scorer.in_combat = lambda _f: True
    scorer.read = lambda _f: (scorer.full_bar, 0.5 * scorer.full_bar)
    scores = []
    for remaining in (55, 40, 25, 10):
        scorer.read_timer = lambda _f, r=remaining: r
        scores.append(scorer.snapshot(None)["score"])
    assert scores == sorted(scores), scores
    assert max(scores) < 0.5, "an undecided round must not reach the win half"


def test_an_undecided_frame_without_a_clock_is_not_priced(scorer):
    """Refuse to invent a duration; let the backward walk find another frame."""
    _live(scorer)
    scorer.in_combat = lambda _f: True
    scorer.read = lambda _f: (scorer.full_bar, 0.5 * scorer.full_bar)
    scorer.read_timer = lambda _f: None
    assert scorer.snapshot(None) is None


def test_a_ko_still_scores_when_the_clock_is_unreadable(scorer):
    """A decided round needs no duration, so it must not depend on one."""
    _live(scorer)
    scorer.in_combat = lambda _f: True
    scorer.read_timer = lambda _f: None
    scorer.read = lambda _f: (0.0, 0.5 * scorer.full_bar)
    assert scorer.snapshot(None)["score"] == pytest.approx(0.25)


# --------------------------------------------------------------------------
# the regression this rule exists for
# --------------------------------------------------------------------------
def test_the_crouch_block_frame_no_longer_scores(scorer):
    """claude-opus-5's submitted program, on the frame it scored 0.436 with.

    It is a 1.5 KB loop that crouch-blocks and never attacks, capped at 900
    tics by the agent itself so the round never resolved. Under the old rule
    the untouched opponent cost it nothing and its own full bar paid 0.436 --
    the best score of that entire run. The frozen tests, which do not let a
    policy stop the clock, scored the same program 0.000 three times.
    """
    _live(scorer)
    reading = scorer.snapshot(frame("turtle_clock56_854.jpg"))
    assert reading is not None
    assert reading["opponent_health"] == pytest.approx(1.0, abs=0.01), (
        "this frame is the one where the opponent was never touched")
    assert reading["own_health"] > 0.8, "and the policy kept its own bar full"
    assert reading["ending"] is None, "the round never resolved"
    assert reading["score"] == pytest.approx(0.0, abs=1e-9)


def test_the_real_ko_frames_keep_their_scores(scorer):
    """The fix must not move any round that actually finished.

    Both frames are evidence PNGs from frozen tests, whose scores are reported
    results; if this rule changed them it would be rewriting outcomes rather
    than closing a hole.
    """
    _live(scorer)
    win = scorer.snapshot(frame("win_clock49_854.jpg"))
    assert win["ending"] == "ko"
    assert win["score"] == pytest.approx(0.9054451096410847, abs=5e-4)

    loss = Kof98TerminalScorer(
        yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))["scorer"],
        client_size=CLIENT)
    _live(loss)
    reading = loss.snapshot(frame("loss_clock32_854.jpg"))
    assert reading["ending"] == "ko"
    assert reading["score"] == pytest.approx(0.10469199911046673, abs=5e-4)
