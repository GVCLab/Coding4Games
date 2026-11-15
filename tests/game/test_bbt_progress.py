"""The score is a function of the milestone SET, and the tiers cannot be bought.

Two halves. The first uses real fixtures to check that the results screen and
the curtain -- both mostly red -- are told apart, because conflating them made a
COMPLETED level read as a transition. The second drives the scorer with a fake
frame source, so the scoring semantics can be asserted without a game: what
pays, what pays only once, and what no amount of a cheaper thing can buy.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2  # noqa: E402

from coding4game_gui.benchmark.windows.scorers import bbt_hud  # noqa: E402
from coding4game_gui.benchmark.windows.scorers.bbt_progress import (  # noqa: E402
    BbtProgressScorer,
)

# tests moved into tests/game/ during the main migration; fixtures stayed
# shared at tests/fixtures/, so this walks up one more level than before.
FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "fixtures", "battleblock_11")


# ------------------------------------------------------- the two red screens
def test_the_results_screen_is_not_a_curtain():
    """It was, and that made a finished level look like a transition."""
    img = cv2.imread(os.path.join(FIX, "results_a_plus_plus.png"))
    assert img is not None
    assert bbt_hud.is_results(img), "the completion signal must fire"
    assert not bbt_hud.is_curtain(img), (
        "a completed level was being reported as a transition; red alone "
        "cannot separate them, brightness can")


# --------------------------------------------------------- scoring semantics
class _Feed:
    """A fake frame source. Each 'frame' is a tag the patched readers decode."""

    def __init__(self, monkeypatch):
        self.tag = ("level", (0, 0))
        monkeypatch.setattr(bbt_hud, "is_results",
                            lambda f: self.tag[0] == "results")
        monkeypatch.setattr(bbt_hud, "is_curtain",
                            lambda f: self.tag[0] == "curtain")
        monkeypatch.setattr(
            bbt_hud, "read",
            lambda f, threshold=None: (self.tag[1]
                                       if self.tag[0] == "level" else None))
        self.frame = np.zeros((4, 4, 3), dtype=np.uint8)

    def level(self, gems, yarn=0):
        self.tag = ("level", (gems, yarn))
        return self.frame

    def curtain(self):
        self.tag = ("curtain", None)
        return self.frame

    def results(self):
        self.tag = ("results", None)
        return self.frame


def _run(sc, feed, steps):
    """steps: list of ('level', gems, yarn) | ('curtain',) | ('results',)."""
    paid = 0.0
    for s in steps:
        if s[0] == "level":
            f = feed.level(s[1], s[2] if len(s) > 2 else 0)
        elif s[0] == "curtain":
            f = feed.curtain()
        else:
            f = feed.results()
        paid += sc.delta(f)
    return paid


def _hold(kind, n, *args):
    return [(kind,) + args] * n


def test_an_episode_that_never_entered_the_level_is_not_a_zero(monkeypatch):
    """T0. A failed cold start must be excludable, not averaged in as a zero --
    a zero is a claim about the policy, and no policy played."""
    feed = _Feed(monkeypatch)
    sc = BbtProgressScorer()
    _run(sc, feed, _hold("curtain", 20))
    assert sc.entered_level is False
    assert sc.total == 0.0
    assert sc.ledger()["tier"] == 0


def test_gems_pay_once_each_and_never_unpay(monkeypatch):
    feed = _Feed(monkeypatch)
    sc = BbtProgressScorer(spec={"progress": {"gem_quota": 3, "yarn_max": 1}})
    _run(sc, feed, _hold("level", 10, 1))
    after_one = sc.total
    assert after_one == pytest.approx(0.95 / 3, abs=1e-6)

    # The same gem, seen for another hundred frames, is worth nothing more.
    _run(sc, feed, _hold("level", 100, 1))
    assert sc.total == pytest.approx(after_one)

    # A collapse with no curtain nearby is our reader failing; the milestone
    # stands and the episode continues.
    _run(sc, feed, _hold("level", 10, 0))
    assert sc.total == pytest.approx(after_one)
    assert sc.restarted is False
    assert sc.gems_max == 1


def test_no_amount_of_yarn_buys_a_gem(monkeypatch):
    """The tier property, stated as the thing it forbids."""
    feed = _Feed(monkeypatch)
    poor = BbtProgressScorer(spec={"progress": {"gem_quota": 3, "yarn_max": 1}})
    _run(poor, feed, _hold("level", 10, 0, 1))       # 0 gems, all the yarn

    rich = BbtProgressScorer(spec={"progress": {"gem_quota": 3, "yarn_max": 1}})
    _run(rich, feed, _hold("level", 10, 1, 0))       # 1 gem, no yarn

    assert poor.total == 0.0, "yarn must not score below the finish tier"
    assert rich.total > poor.total


def test_every_full_T1_stays_below_the_T2_floor(monkeypatch):
    feed = _Feed(monkeypatch)
    almost = BbtProgressScorer(spec={"progress": {"gem_quota": 4}})
    _run(almost, feed, _hold("level", 10, 3))        # 3 of 4 gems
    met = BbtProgressScorer(spec={"progress": {"gem_quota": 4}})
    _run(met, feed, _hold("level", 10, 4))           # quota met, exit open
    assert almost.total < 1.0 <= met.total < 2.0


def test_finishing_outranks_any_unfinished_run(monkeypatch):
    feed = _Feed(monkeypatch)
    unfinished = BbtProgressScorer(spec={"progress": {"gem_quota": 3}})
    _run(unfinished, feed, _hold("level", 50, 3, 1))     # quota met, all yarn

    slow = BbtProgressScorer(
        spec={"progress": {"gem_quota": 3, "reference_tics": 10}})
    _run(slow, feed, _hold("level", 1000, 3) + [("results",)])  # crawled home

    assert slow.total >= 2.0 > unfinished.total


def test_speed_is_scored_against_the_human_reference(monkeypatch):
    feed = _Feed(monkeypatch)
    spec = {"progress": {"gem_quota": 1, "yarn_max": 1, "reference_tics": 100}}

    on_pace = BbtProgressScorer(spec=spec)
    _run(on_pace, feed, _hold("level", 100, 1) + [("results",)])
    half_pace = BbtProgressScorer(spec=spec)
    _run(half_pace, feed, _hold("level", 200, 1) + [("results",)])

    # 2 + 0.8*speed, with yarn at 0 in both.
    assert on_pace.total == pytest.approx(2.8, abs=1e-6)
    assert half_pace.total == pytest.approx(2.4, abs=1e-6)
    assert on_pace.total > half_pace.total


def test_yarn_scores_only_after_finishing(monkeypatch):
    feed = _Feed(monkeypatch)
    spec = {"progress": {"gem_quota": 1, "yarn_max": 1, "reference_tics": 10}}
    without = BbtProgressScorer(spec=spec)
    _run(without, feed, _hold("level", 10, 1, 0) + [("results",)])
    with_yarn = BbtProgressScorer(spec=spec)
    _run(with_yarn, feed, _hold("level", 10, 1, 1) + [("results",)])
    assert with_yarn.total - without.total == pytest.approx(0.2, abs=1e-6)


def test_completion_pays_once_however_long_the_results_screen_stays(monkeypatch):
    feed = _Feed(monkeypatch)
    sc = BbtProgressScorer(
        spec={"progress": {"gem_quota": 1, "reference_tics": 10}})
    _run(sc, feed, _hold("level", 10, 1))
    first = _run(sc, feed, [("results",)])
    more = _run(sc, feed, _hold("results", 30))
    assert first > 0 and more == 0.0
    assert sc.completion_tics == 10


def test_a_collapse_next_to_a_curtain_ends_the_episode(monkeypatch):
    feed = _Feed(monkeypatch)
    sc = BbtProgressScorer(spec={"progress": {"gem_quota": 3}})
    _run(sc, feed, _hold("level", 10, 2))
    assert sc.is_terminal(feed.frame) is False
    _run(sc, feed, _hold("curtain", 3) + _hold("level", 3, 0))
    assert sc.restarted is True
    assert sc.is_terminal(feed.frame) is True
    assert sc.gems_max == 2, "the milestone set survives the restart"


def test_the_declared_quota_is_checked_against_the_game(monkeypatch):
    """A hand-entered constant with nothing watching it drifts."""
    feed = _Feed(monkeypatch)
    sc = BbtProgressScorer(spec={"progress": {"gem_quota": 5}})
    _run(sc, feed, _hold("level", 10, 3))
    assert sc.quota_disagreement is False, "the exit never opened; nothing to check"

    sc2 = BbtProgressScorer(spec={"progress": {"gem_quota": 3}})
    _run(sc2, feed, _hold("level", 10, 3))
    assert sc2.exit_open_at_gems == 3
    assert sc2.quota_disagreement is False


def test_classify_phase_names_what_the_lifecycle_watchdog_needs():
    """`level` is the only phase the scenario declares as progress, so a
    curtain and the results screen must NOT be it, or a completed level would
    keep the non-progress watchdog asleep. Checked on real fixtures because
    this is the one place the scorer's answer is a claim about pixels."""
    sc = BbtProgressScorer()
    curtain = cv2.imread(os.path.join(FIX, "restart_curtain.png"))
    results = cv2.imread(os.path.join(FIX, "results_a_plus_plus.png"))
    level = cv2.imread(os.path.join(FIX, "gem1.png"))

    assert sc.classify_phase(curtain) == "curtain"
    assert sc.classify_phase(results) == "results"
    # Before any frame has been scored we have not seen the level yet, and
    # saying `level` there would tell the watchdog a cold start is gameplay.
    assert sc.classify_phase(level) == "unknown"
    sc.delta(level)
    assert sc.classify_phase(level) == "level"


def test_nothing_is_scored_after_the_level_is_finished(monkeypatch):
    """Measured on a real playthrough: after the results screen the game
    returns to the hub, the HUD reads 0 again, and a hub curtain sits right
    next to it -- which used to be classified as a RESTART, about 18 times, on
    an episode that had already been won."""
    feed = _Feed(monkeypatch)
    sc = BbtProgressScorer(
        spec={"progress": {"gem_quota": 3, "reference_tics": 10}})
    _run(sc, feed, _hold("level", 10, 3) + [("results",)])
    assert sc.completed and not sc.restarted
    won = sc.total
    # The hub, exactly as recorded: curtain, then the counter back to zero.
    _run(sc, feed, _hold("curtain", 3) + _hold("level", 20, 0))
    assert sc.restarted is False, "a finished episode cannot be restarted"
    assert sc.total == pytest.approx(won)


def test_tics_are_the_env_s_tics_when_the_env_says_so(monkeypatch):
    """A decision spends several tics and scores once, so counting calls counts
    DECISIONS. Measured live: 120 decisions of 2-3 tics were recorded as 120
    tics, and then compared against a tic-denominated human reference."""
    feed = _Feed(monkeypatch)
    sc = BbtProgressScorer(
        spec={"progress": {"gem_quota": 1, "reference_tics": 30}})
    for _ in range(10):
        sc.note_tics(3)                      # what env_ext stamps on
        sc.delta(feed.level(1))
    sc.delta(feed.results())
    assert sc.tics == 30
    assert sc.completion_tics == 30
    assert sc.ledger()["tic_source"] == "env"
    # 30 tics against a 30-tic reference is exactly on pace.
    assert sc.total == pytest.approx(2.8, abs=1e-6)


def test_without_the_env_stamp_the_ledger_admits_it(monkeypatch):
    """The fallback must not report decisions under a tic-shaped name in
    silence -- that is the shape of the bug it replaces."""
    feed = _Feed(monkeypatch)
    sc = BbtProgressScorer(spec={"progress": {"gem_quota": 1}})
    _run(sc, feed, _hold("level", 7, 1))
    assert sc.tics == 7
    assert sc.ledger()["tic_source"] == "per-call"


def test_game_time_behind_a_death_curtain_still_counts(monkeypatch):
    """The human's 518 includes four deaths and their respawn curtains, so an
    agent's must too: tics spent behind an in-level curtain are carried into
    the next readable frame, not dropped."""
    feed = _Feed(monkeypatch)
    sc = BbtProgressScorer(
        spec={"progress": {"gem_quota": 1, "reference_tics": 60}})
    for _ in range(5):
        sc.note_tics(6)
        sc.delta(feed.level(1))
    for _ in range(2):
        sc.note_tics(10)
        sc.delta(feed.curtain())
    sc.note_tics(4)
    sc.delta(feed.level(1))
    assert sc.tics == 30 + 20 + 4
    assert sc.curtain_tics == 20


def test_the_exit_curtain_is_not_part_of_completion(monkeypatch):
    """The reference stops at the exit; the ~5s curtain before the results
    screen is not play and must not be charged to the agent."""
    feed = _Feed(monkeypatch)
    sc = BbtProgressScorer(
        spec={"progress": {"gem_quota": 1, "reference_tics": 50}})
    for _ in range(10):
        sc.note_tics(5)
        sc.delta(feed.level(1))
    for _ in range(5):
        sc.note_tics(10)
        sc.delta(feed.curtain())
    sc.note_tics(10)
    sc.delta(feed.results())
    assert sc.completion_tics == 50
    assert sc.total == pytest.approx(2.8, abs=1e-6)


def test_time_before_the_level_is_not_counted(monkeypatch):
    """Frames before the HUD first reads are the cold start, not play."""
    feed = _Feed(monkeypatch)
    sc = BbtProgressScorer(spec={"progress": {"gem_quota": 1}})
    for _ in range(3):
        sc.note_tics(40)
        sc.delta(feed.curtain())
    sc.note_tics(2)
    sc.delta(feed.level(0))
    assert sc.tics == 2


def test_a_zero_tic_span_counts_zero(monkeypatch):
    """Two frames stamped at the same tic cover no game time; only the
    per-call fallback assumes one tic per frame."""
    feed = _Feed(monkeypatch)
    sc = BbtProgressScorer(spec={"progress": {"gem_quota": 1}})
    sc.note_tics(5)
    sc.delta(feed.level(0))
    sc.note_tics(0)
    sc.delta(feed.level(0))
    assert sc.tics == 5


def test_per_call_counts_carry_nothing_across_a_curtain(monkeypatch):
    """The fallback is unchanged: readable frames only, one per call."""
    feed = _Feed(monkeypatch)
    sc = BbtProgressScorer(spec={"progress": {"gem_quota": 1}})
    _run(sc, feed, _hold("level", 4, 1) + _hold("curtain", 3)
         + _hold("level", 2, 1))
    assert sc.tics == 6
    assert sc.curtain_tics == 0
    assert sc.ledger()["tic_source"] == "per-call"


def test_the_total_is_monotone_across_a_whole_messy_episode(monkeypatch):
    """The property that makes the score unfarmable: it never goes down, so no
    sequence of actions can be worth repeating."""
    feed = _Feed(monkeypatch)
    sc = BbtProgressScorer(spec={"progress": {"gem_quota": 3, "yarn_max": 1}})
    seen = []
    script = (_hold("level", 6, 0) + _hold("level", 6, 1) + _hold("curtain", 2)
              + _hold("level", 6, 1, 1) + _hold("level", 6, 0)
              + _hold("level", 6, 2) + _hold("level", 6, 3))
    for s in script:
        d = sc.delta(feed.level(s[1], s[2] if len(s) > 2 else 0)
                     if s[0] == "level" else feed.curtain())
        assert d >= 0.0
        seen.append(sc.total)
    assert seen == sorted(seen)


# ---------------------------------------------------------------------------
# Deferred scoring: terminal detection must survive it
# ---------------------------------------------------------------------------

def _img(name):
    img = cv2.imread(os.path.join(FIX, name))
    assert img is not None, name
    return img


def test_is_terminal_fires_on_the_results_frame_alone():
    """The half that main's deferred scoring broke.

    `env.act` only QUEUES frames; `delta` — which sets `completed` — does not
    run until settlement. Terminal detection is live. So a scorer whose
    `is_terminal` consults only its own flags reports False for the entire
    episode and the run burns its whole budget on a level it already finished.

    A fresh scorer here means `delta` has never run, which is precisely the
    state the live path is in.
    """
    sc = BbtProgressScorer()
    results = _img("results_a_plus_plus.png")
    assert sc.completed is False and sc.restarted is False
    assert sc.is_terminal(results) is True


def test_is_terminal_stays_false_on_ordinary_level_frames():
    """The negative control: without it, a scorer that always returned True
    would pass the test above."""
    sc = BbtProgressScorer()
    for name in ("gem0.png", "gem1.png", "gem2.png", "gem3_yarn1_exitopen.png"):
        assert sc.is_terminal(_img(name)) is False, name


def test_is_terminal_still_honours_the_settled_flags():
    """Settlement replays every frame, so the flags remain the path that
    carries a RESTART — which no single frame shows."""
    sc = BbtProgressScorer()
    assert sc.is_terminal(_img("gem0.png")) is False
    sc.restarted = True
    assert sc.is_terminal(_img("gem0.png")) is True
