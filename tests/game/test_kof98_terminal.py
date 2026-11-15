"""Pin KOF98 terminal detection and health-bar calibration to REAL frames.

Every fixture here is a frame from `kof_human_rec2` — one full human match
played on mr-why and captured through the production renderer (PrintWindow,
DPI-2x crop 1744x1042 -> 854x480). They are not screenshots taken by a
separate script: a fixture produced off the production path lets the test and
the code under test share the same wrong frame of reference and agree forever.

The numbers in this file are not decoration. Three of these tests exist because
a calibration that LOOKED fine was wrong in a way no smoke run would surface:

  * main's fractions do not read zero on this capture, they read plausible
    scene-dependent garbage (0.31 on a bar that is visibly full);
  * our own first pass measured P1's box on a damaged frame, so it saturates
    at 1.000 for every health above 75% — a third of the recording;
  * the CONTINUE? gate needs BOTH of its conditions, and the recording
    contains a frame that defeats each one alone.
"""
from __future__ import annotations

import pathlib

import cv2
import numpy as np
import pytest
import yaml

from coding4game_gui.benchmark.windows.scorers.kof98_terminal import Kof98TerminalScorer

REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "kof98_terminal"
SCENARIO = FIXTURES / "mr_why_calibration.yaml"
CLIENT = (854, 480)


def frame(name: str) -> np.ndarray:
    img = cv2.imread(str(FIXTURES / name))
    assert img is not None, f"fixture missing: {name}"
    assert img.shape[:2] == (CLIENT[1], CLIENT[0]), (
        f"{name} is {img.shape[:2]}, expected {(CLIENT[1], CLIENT[0])}")
    return img


@pytest.fixture(scope="module")
def spec() -> dict:
    """Keep historical human frames bound to their measured capture geometry."""
    scenario = yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))
    return scenario["scorer"]


@pytest.fixture()
def scorer(spec) -> Kof98TerminalScorer:
    return Kof98TerminalScorer(spec, client_size=CLIENT)


# --------------------------------------------------------------------------
# CONTINUE? detection
# --------------------------------------------------------------------------

# (fixture, expected blue, expected white_bottom, expected gate verdict).
# Values are bit-identical between mr-why (where they were calibrated) and
# macOS/OpenCV 4.13, so they are pinned tight rather than loosely bounded.
SIGNATURES = [
    ("combat_f00434.jpg",              0.104089, 0.131203, False),
    ("combat_f00667.jpg",              0.087151, 0.123569, False),
    ("dark_transition_f00199.jpg",     0.000000, 0.000000, False),
    ("white_flash_f01032.jpg",         0.000000, 0.559962, False),
    ("winner_pose_f01038.jpg",         0.241694, 0.020591, False),
    ("continue_f01055.jpg",            0.310009, 0.337557, True),
    ("continue_margin_f01072.jpg",     0.241908, 0.263068, True),
    ("continue_static_late_f01132.jpg", 0.026720, 0.023629, False),
]


@pytest.mark.parametrize("name,blue,white,hit", SIGNATURES)
def test_continue_signature_is_stable(scorer, name, blue, white, hit):
    got_blue, got_white = scorer.continue_signature(frame(name))
    assert got_blue == pytest.approx(blue, abs=2e-3), name
    assert got_white == pytest.approx(white, abs=2e-3), name
    assert scorer.is_continue_screen(frame(name)) is hit, name


def test_each_gate_is_defeated_by_a_real_frame(scorer):
    """Neither half of the conjunction survives alone — measured, not argued.

    This is why the gate is `blue AND white_bot` rather than either one: the
    recording contains a frame that clears each threshold on its own.
    """
    flash_blue, flash_white = scorer.continue_signature(
        frame("white_flash_f01032.jpg"))
    assert flash_white > scorer.white_bot_min, (
        "the KO whiteout clears the white gate, so white alone is not enough")
    assert flash_blue < scorer.blue_min

    pose_blue, pose_white = scorer.continue_signature(
        frame("winner_pose_f01038.jpg"))
    assert pose_blue > scorer.blue_min, (
        "the winner pose clears the blue gate, so blue alone is not enough")
    assert pose_white < scorer.white_bot_min


def test_late_continue_frame_is_a_known_miss(scorer):
    """Documented coverage limit, pinned so it cannot become a surprise.

    The CONTINUE? screen ramps a TV-static effect as its countdown expires;
    by 00:08 both statistics have collapsed and the phase check misses.
    Episode termination uses the first fight's outcome.
    """
    assert scorer.is_continue_screen(frame("continue_static_late_f01132.jpg")) is False


def test_match_end_phase_is_reported(scorer):
    assert scorer.classify_phase(frame("continue_f01055.jpg")) == "match_end"
    assert scorer.classify_phase(frame("combat_f00434.jpg")) == "combat"
    assert scorer.classify_phase(frame("dark_transition_f00199.jpg")) == "non_combat"


# --------------------------------------------------------------------------
# Health-bar calibration
# --------------------------------------------------------------------------

# Independently derived from the pixels, NOT from the scorer: colored-column
# runs in the bar band give P1 x 99..374 (275px wide) and P2 x 460..737
# (277px). A partial bar's health is its surviving run over that full width.
TRUE_HEALTH = {
    "full_health_f00244.jpg": (1.000, 1.000),
    "p1_85pct_f00000.jpg":    (0.851, None),
    "combat_f00434.jpg":      (0.366, 0.421),
    "combat_f00667.jpg":      (0.373, 0.291),
}


@pytest.mark.parametrize("name", sorted(TRUE_HEALTH))
def test_health_reads_match_pixel_geometry(scorer, name):
    want_p1, want_p2 = TRUE_HEALTH[name]
    got_p1, got_p2 = scorer.read(frame(name))
    assert got_p1 == pytest.approx(want_p1, abs=0.02), f"{name} p1"
    if want_p2 is not None:
        assert got_p2 == pytest.approx(want_p2, abs=0.02), f"{name} p2"


def test_previous_calibration_saturated_above_75_percent(scorer):
    """Regression: our first pass measured P1's box on a DAMAGED frame.

    Its box spans 207px against a 275px bar, so anything above ~75% health
    reads exactly 1.000 and damage taken there produces no delta at all. 387
    of the recording's 1168 frames sit in that band.
    """
    img = frame("p1_85pct_f00000.jpg")
    stale = Kof98TerminalScorer(
        {"health": {"p1_frac": [0.196, 0.438, 0.044, 0.068],
                    "p2_frac": [0.539, 0.862, 0.044, 0.068],
                    "timer_frac": [0.468, 0.527, 0.038, 0.108]}},
        client_size=CLIENT)
    assert stale.read(img)[0] == pytest.approx(1.0, abs=1e-6), (
        "if this stops saturating the fixture changed, not the bug")
    assert scorer.read(img)[0] == pytest.approx(0.851, abs=0.02)


def test_main_fractions_read_garbage_not_zero_on_this_capture(scorer):
    """Regression: the failure this capture actually produces is NOT a zero.

    main's y 0.086..0.114 lands on the character-name row here. On a frame
    whose two bars are both visibly FULL it returns 0.31 and 0.62 — a reading
    that looks like a live health signal and is not one, which is why the
    mismatch survived a smoke run.
    """
    img = frame("full_health_f00244.jpg")
    upstream = Kof98TerminalScorer(
        {"health": {"p1_frac": [0.208, 0.446, 0.086, 0.114],
                    "p2_frac": [0.547, 0.870, 0.086, 0.114],
                    "timer_frac": [0.468, 0.527, 0.038, 0.108]}},
        client_size=CLIENT)
    bad_p1, bad_p2 = upstream.read(img)
    assert 0.2 < bad_p1 < 0.45 and 0.5 < bad_p2 < 0.75
    good_p1, good_p2 = scorer.read(img)
    assert good_p1 == pytest.approx(1.0) and good_p2 == pytest.approx(1.0)


def test_combat_gate_holds_on_every_combat_fixture(scorer):
    for name in ("full_health_f00244.jpg", "p1_85pct_f00000.jpg",
                 "combat_f00434.jpg", "combat_f00667.jpg"):
        assert scorer.in_combat(frame(name)) is True, name
    for name in ("dark_transition_f00199.jpg", "white_flash_f01032.jpg",
                 "continue_f01055.jpg"):
        assert scorer.in_combat(frame(name)) is False, name


def test_delta_is_zero_across_a_non_combat_frame(scorer):
    """A transition must not manufacture reward out of a stale baseline."""
    scorer.delta(frame("full_health_f00244.jpg"))
    assert scorer.delta(frame("white_flash_f01032.jpg")) == 0.0
    # Baseline cleared, so the first combat frame after it scores nothing
    # rather than the whole accumulated difference.
    assert scorer.delta(frame("combat_f00434.jpg")) == 0.0


def test_damage_to_the_opponent_scores_positive(scorer):
    scorer.delta(frame("combat_f00434.jpg"))          # establish the baseline
    d = scorer.delta(frame("combat_f00667.jpg"))      # P2 loses ~0.13, P1 flat
    assert d > 0.10, "opponent health lost must score positive"
    assert scorer.total == pytest.approx(d)


# --------------------------------------------------------------------------
# The combat gate, added 2026-09-19
#
# Character-select portraits can look like timer and health pixels. Keep
# negative frames at both recorded capture sizes to verify the combat gate.
# --------------------------------------------------------------------------

BIG = (1280, 768)


def big_frame(name: str) -> np.ndarray:
    img = cv2.imread(str(FIXTURES / name))
    assert img is not None, f"fixture missing: {name}"
    assert img.shape[:2] == (BIG[1], BIG[0]), (
        f"{name} is {img.shape[:2]}, expected {(BIG[1], BIG[0])}")
    return img


@pytest.fixture()
def big_scorer(spec) -> Kof98TerminalScorer:
    return Kof98TerminalScorer(spec, client_size=BIG)


@pytest.mark.parametrize("name", [
    "char_select_768.jpg",
    "stage_select_768.jpg",
    "order_complete_768.jpg",
])
def test_menu_screens_are_never_combat_at_the_code_arm_size(big_scorer, name):
    assert big_scorer.in_combat(big_frame(name)) is False, name


@pytest.mark.parametrize("name", [
    "team_select_854_t02003.jpg",
    "winner_plate_854_t01532.jpg",
])
def test_post_match_screens_are_never_combat_at_the_gui_arm_size(scorer, name):
    assert scorer.in_combat(frame(name)) is False, name


def test_real_combat_still_reads_as_combat_at_the_gui_arm_size(scorer):
    assert scorer.in_combat(frame("combat_854_t01301.jpg")) is True


# --------------------------------------------------------------------------
# First-KO scoring, added 2026-09-19
#
# The episode now ends at the first KO and the score is read from that one
# frame, rather than being a running sum of per-frame health deltas. The old
# rule was additive in time and unbounded, so a policy that stayed in the fight
# longer accumulated a larger magnitude and "played more" could not be told
# from "played worse".
# --------------------------------------------------------------------------


def _live(scorer, p1=0.9, p2=0.9):
    """Latch the fight as begun without needing a real full-health frame."""
    scorer._seen_live = True


def test_the_scenario_asks_for_terminal_state_settlement(spec):
    """Without this key the host sums deltas and the new rule never runs.

    `DeferredScore.settle` only calls `snapshot` when the scorer's spec says
    `latest_valid_frame`; otherwise it accumulates `delta` exactly as before.
    A scorer that implemented `snapshot` but was never asked for it would look
    correct in unit tests and score the old way in every real run.
    """
    assert spec.get("settlement") == "latest_valid_frame"


def test_a_full_health_frame_is_not_a_ko(scorer):
    reading = scorer.snapshot(frame("full_health_f00244.jpg"))
    assert reading is None, "the first live frame only latches the fight"
    reading = scorer.snapshot(frame("full_health_f00244.jpg"))
    assert reading is not None
    assert reading["ko"] is False
    assert scorer.is_terminal(frame("full_health_f00244.jpg")) is False


def test_non_combat_frames_have_no_reading(scorer):
    """`_settle_latest` walks backward and skips these to reach the KO frame."""
    _live(scorer)
    for name in ("white_flash_f01032.jpg", "dark_transition_f00199.jpg",
                 "continue_f01055.jpg", "winner_pose_f01038.jpg"):
        assert scorer.snapshot(frame(name)) is None, name
        assert scorer.is_terminal(frame(name)) is False, name


def test_the_fight_must_be_seen_live_before_a_ko_can_end_it(scorer):
    """A misread during hand-over must not end the episode on tic one."""
    assert scorer._seen_live is False
    assert scorer.is_terminal(frame("p1_85pct_f00000.jpg")) is False
    assert scorer._seen_live is True


def test_score_endpoints_match_the_stated_rule(scorer):
    """0 is "dealt nothing and my bar is empty"; 1 is "KO at full health"."""
    _live(scorer)
    # Drive the arithmetic directly rather than hunting for fixtures that
    # happen to sit at the endpoints; the readings are the scorer's own units.
    scorer.in_combat = lambda _f: True
    scorer.read_timer = lambda _f: 30

    scorer.read = lambda _f: (0.0, scorer.full_bar)
    assert scorer.snapshot(None)["score"] == pytest.approx(0.0)
    assert scorer.is_terminal(None) is True

    scorer.read = lambda _f: (scorer.full_bar, 0.0)
    assert scorer.snapshot(None)["score"] == pytest.approx(1.0)
    assert scorer.is_terminal(None) is True

    # Two full bars decide nothing, so this is the undecided branch, and a
    # round nobody has touched scores zero rather than looking like a draw.
    scorer.read = lambda _f: (scorer.full_bar, scorer.full_bar)
    assert scorer.snapshot(None)["score"] == pytest.approx(0.0)
    assert scorer.is_terminal(None) is False


def test_duration_cannot_enter_the_score(scorer):
    """The property the old rule lacked: the same frame always scores the same.

    Under the previous sum-of-deltas rule the identical final frame produced a
    different number depending on how many frames preceded it. Here it cannot.
    """
    _live(scorer)
    first = scorer.snapshot(frame("p1_85pct_f00000.jpg"))["score"]
    for _ in range(20):
        scorer.snapshot(frame("combat_f00434.jpg"))
        scorer.snapshot(frame("combat_f00667.jpg"))
    assert scorer.snapshot(frame("p1_85pct_f00000.jpg"))["score"] == first
