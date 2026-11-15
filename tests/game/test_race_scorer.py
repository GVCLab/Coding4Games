"""Race scorer behaviour, driven by a fake OCR so it runs without a GPU.

The fake keys off crop SIZE, which means these tests also check that the
scenario's rects are wired to the regions the scorer expects -- a rect typo
would hand the clock reader the distance crop, which is exactly the class of
bug that made the first hand-estimated rects useless.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import yaml

from coding4game_gui.benchmark.windows.scorers.race import (
    RaceScorer, parse_metres, parse_time)

SPEC = (Path(__file__).resolve().parents[2] / "coding4game_gui" / "benchmark"
        / "scenarios" / "trackmania.yaml")


@pytest.fixture(scope="module")
def cfg() -> dict:
    return yaml.safe_load(SPEC.read_text(encoding="utf-8"))


def make_scorer(cfg, *, clock=None, distance=None, finish=None,
                track="A02-Race"):
    """Build a scorer whose OCR answers per region, identified by crop size."""
    regions = cfg["scorer"]["regions"]
    by_size = {}
    for name, text in (("clock", clock), ("distance", distance),
                       ("finish_time", finish), ("track", track)):
        x, y, w, h = regions[name]["rect"]
        by_size[(w, h)] = text

    def fake_ocr(crop):
        h, w = crop.shape[:2]
        text = by_size.get((w, h))
        return [{"text": text, "conf": 1.0, "box": [0, 0, w, h]}] if text else []

    return RaceScorer(cfg["scorer"], (1280, 720), ocr=fake_ocr)


BLANK = np.zeros((720, 1280, 3), np.uint8)


def read_confirmed(scorer, frames: int = 2):
    """Read enough times to satisfy the finish debounce.

    A finish must hold across consecutive reads -- one dropped clock read used
    to end an episode at 124m and score it as a completed lap.
    """
    reading = None
    for _ in range(frames):
        reading = scorer.read(BLANK)
    return reading


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize("text,expected", [
    ("新的时间 0.31.16", 31.16),
    ("1:02.45", 62.45),
    ("0:00.00", 0.0),
    ("tameless", None),
])
def test_parse_time(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("116 m", 116), ("2095m", 2095), ("0m", 0),
    ("2095", 2095),                       # the small 'm' is often dropped
    ("abc", None),
])
def test_parse_metres(text, expected):
    assert parse_metres(text) == expected


# ------------------------------------------------------------- finish logic
def test_finish_needs_the_clock_to_be_gone(cfg):
    """A paused race still draws its clock. Treating 'a time is visible' as a
    finish would score every pause as a completed lap."""
    s = make_scorer(cfg, clock="0:12.00", finish="新的时间 0:31.16")
    r = s.read(BLANK)
    assert not r.finished
    assert r.in_race


def test_one_frame_does_not_confirm_a_finish(cfg):
    """The debounce itself. Without it a single dropped clock read ends the
    episode -- observed at 124m, scored as a completed lap."""
    s = make_scorer(cfg, clock=None, finish="新的时间 0:31.16")
    assert not s.read(BLANK).finished
    assert s.read(BLANK).finished


def test_a_dropped_clock_read_alone_is_not_a_finish(cfg):
    """Distance still readable means we are still on the track, whatever the
    clock did -- the in-race personal-best row can parse as a time."""
    s = make_scorer(cfg, clock=None, distance="500m", finish="最佳时间 0:30.45")
    assert not read_confirmed(s, 4).finished


# -------------------------------------------------------------- medal scale
@pytest.mark.parametrize("medal,expected", [
    ("gold_seconds", 2.0),
    ("silver_seconds", None),      # between the two, checked by ordering
    ("bronze_seconds", 1.0),
])
def test_medal_times_land_on_their_marks(cfg, medal, expected):
    """Read the times from the spec: hardcoding them meant every test failed
    the moment the benchmark switched tracks."""
    seconds = cfg["scorer"]["track"][medal]
    s = make_scorer(cfg, clock=None, finish=f"新的时间 0:{seconds:05.2f}")
    got = s.score_for(read_confirmed(s))
    if expected is not None:
        assert got == pytest.approx(expected, abs=0.01)
    else:
        assert 1.0 < got < 2.0


def test_beating_gold_scores_above_two(cfg):
    faster = cfg["scorer"]["track"]["gold_seconds"] - 2.0
    s = make_scorer(cfg, clock=None, finish=f"新的时间 0:{faster:05.2f}")
    assert s.score_for(read_confirmed(s)) > 2.0


# ----------------------------------------------------------------- progress
def test_progress_scales_with_distance(cfg):
    length = cfg["scorer"]["track"]["track_length_m"]
    near = make_scorer(cfg, clock="0:20.00", distance=f"{int(length*0.9)}m")
    far = make_scorer(cfg, clock="0:20.00", distance=f"{int(length*0.1)}m")
    assert (near.score_for(near.read(BLANK))
            > far.score_for(far.read(BLANK)) > 0)


def test_progress_uses_the_high_water_mark(cfg):
    """RESPAWN sends the car back. Progress already earned is not lost, or the
    agent is punished for using a recovery the game offers it."""
    s = make_scorer(cfg, clock="0:20.00", distance="1500m")
    s.read(BLANK)
    assert s.best_distance == 1500
    s.regions = s.regions            # unchanged; swap the OCR instead
    back = make_scorer(cfg, clock="0:20.00", distance="300m")
    back.best_distance = 1500
    back.read(BLANK)
    assert back.best_distance == 1500


def test_standing_still_scores_zero(cfg):
    s = make_scorer(cfg, clock="0:05.00", distance="0m")
    assert s.score_for(s.read(BLANK)) == 0.0


def test_total_tracks_reads_not_only_deltas(cfg):
    """total() used to update only inside delta(), so a loop that called
    read() drove 125m and still reported a score of 0.000."""
    s = make_scorer(cfg, clock="0:10.00",
                    distance=f"{cfg['scorer']['track']['track_length_m'] // 2}m")
    s.read(BLANK)
    assert s.total > 0


# -------------------------------------------------------------------- guard
def test_wrong_track_is_detected(cfg):
    right = make_scorer(cfg, clock="0:05.00",
                        track=cfg["scorer"]["track"]["name"])
    right.read(BLANK)
    assert not right.wrong_track()

    wrong = make_scorer(cfg, clock="0:05.00", track="A99-Nowhere")
    wrong.read(BLANK)
    assert wrong.wrong_track()


def test_an_unreadable_track_name_is_not_reported_as_the_right_track(cfg):
    """Silence is not assent.

    The old shape returned False both for "this is A02" and for "the name did
    not read" — reporting no problem where there was no evidence, on the one
    check standing between the benchmark and scoring a stranger's lap against
    A02's medal times. Unreadable is the COMMON case here: at the start line
    the name is drawn over the stadium, "A02" falls on a white floodlight
    tower, and five candidate rects all read "Race" or "-Race ladeo".
    """
    blind = make_scorer(cfg, clock="0:05.00", track=None)
    blind.read(BLANK)
    assert blind.track_identity() == "unreadable"
    assert not blind.wrong_track()          # unreadable is not a WRONG track...
    assert blind.track_identity() != "ok"   # ...and it is not the right one


def test_the_readable_tail_alone_does_not_identify_a_track(cfg):
    """"Race" is the part OCR does recover from a start-line frame, and every
    track in this campaign ends in it — A01-Race, A03-Race, A05-Race. If that
    counted as identification the guard would wave through all of them."""
    tail = make_scorer(cfg, clock="0:05.00", track="Race")
    tail.read(BLANK)
    assert tail.track_identity() == "wrong"


def test_start_check_rejects_wrong_and_unreadable_track_identity(cfg):
    from coding4game_gui.benchmark.windows.env import ResetController
    from types import SimpleNamespace
    controller = object.__new__(ResetController)
    controller._start_check = {"track_identity": True}
    for identity, expected in [("ok", True), ("wrong", False), ("unreadable", False)]:
        controller._scorer = SimpleNamespace(
            read=lambda frame: None,
            track_identity=lambda reading, value=identity: value)
        assert controller.start_ok(BLANK) is expected


# ------------------------------------------------------------------- config
def test_distance_rect_is_wide_enough_for_four_digits(cfg):
    """The readout is right-aligned. A 1195px left edge truncated 2095m to
    '2' for the whole back half of a lap; the rect must stay wide."""
    x, y, w, h = cfg["scorer"]["regions"]["distance"]["rect"]
    assert w >= 130, f"distance rect {w}px wide will truncate four digits"


# ---------------------------------------------------------------------------
# The finish tier must have a gradient.
#
# `max(floor, 1 + (bronze - t)/(bronze - gold))` is flat for every t past
# bronze + (bronze - gold) * (1 - floor) — 32.12 s on A03. Every lap the
# benchmark has recorded on this track is slower than that (32.75, 36.16,
# 36.55, 57.36, 57.36), so all five finishes scored EXACTLY 0.6 and a run's
# "best score" was a constant. Found by a red-team pass, not by the tests:
# nothing here had ever asked what a SLOW finish is worth.
# ---------------------------------------------------------------------------
A03_LAPS = [32.75, 36.16, 36.55, 57.36]


def test_slower_finishes_are_ordered_by_lap_time(cfg):
    scores = [make_scorer(cfg, clock=None, finish=f"新的时间 0:{t:05.2f}")
              for t in A03_LAPS]
    got = [s.score_for(read_confirmed(s)) for s in scores]
    assert got == sorted(got, reverse=True), dict(zip(A03_LAPS, got))
    assert len(set(got)) == len(got), (
        f"all these laps score the same: {dict(zip(A03_LAPS, got))}")


def test_however_slow_a_finish_still_beats_every_unfinished_run(cfg):
    """The property the floor existed to guarantee, which the decay must not
    give up: the finish tier approaches the floor without reaching it, and an
    unfinished run caps at 0.9 x floor."""
    floor = cfg["scorer"]["finish_floor"]
    crawl = make_scorer(cfg, clock=None, finish="新的时间 9:59.99")
    assert crawl.score_for(read_confirmed(crawl)) > floor
    best_unfinished = 0.999 * floor * 0.9
    assert crawl.score_for(read_confirmed(crawl)) > best_unfinished


def test_the_medal_scale_joins_the_decay_without_a_step(cfg):
    """Both branches must give 1.0 at the bronze time, or the reward would
    jump at the join and a hair slower than bronze would score a hair less
    than a hair faster — except by a discontinuity."""
    bronze = cfg["scorer"]["track"]["bronze_seconds"]
    just_under = make_scorer(cfg, clock=None, finish=f"新的时间 0:{bronze - 0.01:05.2f}")
    just_over = make_scorer(cfg, clock=None, finish=f"新的时间 0:{bronze + 0.01:05.2f}")
    a = just_under.score_for(read_confirmed(just_under))
    b = just_over.score_for(read_confirmed(just_over))
    assert a > b
    assert abs(a - b) < 0.01, (a, b)


def test_a_physically_impossible_finish_time_is_not_a_finish(cfg):
    """A finish time IS the score — the medal scale runs backwards from
    bronze, so a clock misread low pays out enormously. A constructed false
    finish at 3.20 s scored 3.897 on a 0-1 benchmark, and the headline is a
    max over episodes. The floor is track_length / max_speed_mps."""
    length = cfg["scorer"]["track"]["track_length_m"]
    s = make_scorer(cfg, clock=None, finish="新的时间 0:03.20")
    r = read_confirmed(s)
    assert length / 150.0 > 3.20, "fixture no longer impossible; pick a lower time"
    assert not r.finished, "3.20 s for this track is not a fast lap, it is a misread"


# ---------------------------------------------------------------------------
# start_check's rect must actually scope the match.
#
# `env.start_ok` passes `start_check.rect`; this scorer accepted the argument
# and ignored it, matching any declared region's text. The scenario spends
# twenty lines on why that rect exists: the opponent-select dialog prints the
# track's medal times in exactly the format start_check looks for, so an
# unscoped search says "the race started" while the game is still on a menu.
# ---------------------------------------------------------------------------
def _dialog_scorer(cfg):
    """A frame where the OPPONENT DIALOG is up: no race clock, but the medal
    times are on screen. `menu_left` is the full-screen crop the scorer reads
    when no HUD is up, which is where the dialog's text lands."""
    regions = cfg["scorer"]["regions"]
    by_size = {}
    for name, text in (("menu_left", "金牌 0:17.27 银牌 0:19.47 铜牌 0:24.28"),
                       ("track", "A03-Race")):
        if name not in regions:
            continue
        x, y, w, h = regions[name]["rect"]
        by_size[(w, h)] = text

    def fake_ocr(crop):
        h, w = crop.shape[:2]
        text = by_size.get((w, h))
        return [{"text": text, "conf": 1.0, "box": [0, 0, w, h]}] if text else []

    return RaceScorer(cfg["scorer"], (1280, 720), ocr=fake_ocr)


def test_start_check_does_not_pass_on_the_opponent_dialog(cfg):
    """The medal times read as a race clock everywhere EXCEPT the clock rect."""
    sc = _dialog_scorer(cfg)
    check = cfg["startup"]["start_check"]
    assert not sc.check_text(BLANK, check["pattern"], rect=check["rect"]), (
        "a medal time on a menu must not be mistaken for the race clock")


def test_start_check_still_passes_on_a_real_race_clock(cfg):
    """The negative control: scoping must not break the thing it scopes."""
    sc = make_scorer(cfg, clock="0:03.41")
    check = cfg["startup"]["start_check"]
    assert sc.check_text(BLANK, check["pattern"], rect=check["rect"])


def test_an_unscoped_check_is_still_wide(cfg):
    """Documents the fallback rather than silently changing it: callers that
    pass no rect get the old any-region behaviour."""
    sc = make_scorer(cfg, clock="0:03.41")
    assert sc.check_text(BLANK, cfg["startup"]["start_check"]["pattern"])


# ---------------------------------------------------------------------------
# A clock that fails to OCR is not a menu.
# ---------------------------------------------------------------------------
def test_one_unreadable_clock_does_not_trigger_a_full_screen_read(cfg):
    """The scenario measures 6 unparseable clocks in 86 racing acts. Treating
    each as "no race" paid for a full-screen OCR pass (~1.3 s against ~1 s
    acts) and pushed in-race screen text into the stall watchdog's signature."""
    sizes = []
    regions = cfg["scorer"]["regions"]
    dist_wh = tuple(regions["distance"]["rect"][2:])
    clock_wh = tuple(regions["clock"]["rect"][2:])

    def fake_ocr(crop):
        h, w = crop.shape[:2]
        sizes.append((w, h))
        if (w, h) == dist_wh:
            return [{"text": "412m", "conf": 1.0, "box": [0, 0, w, h]}]
        return []                       # the clock, and everything else, fails

    sc = RaceScorer(cfg["scorer"], (1280, 720), ocr=fake_ocr)
    sc.read(BLANK)
    menu = {tuple(r["rect"][2:]) for name, r in regions.items()
            if name not in RaceScorer.SCORING_REGIONS}
    assert menu, "fixture needs at least one menu region to be meaningful"
    assert not (set(sizes) & menu), (
        f"read menu crops {set(sizes) & menu} while the odometer said 412m")
    assert clock_wh in set(sizes), "the clock must still be attempted"


# ---------------------------------------------------------------------------
# The agent is TOLD how it is scored, and it was told wrong.
#
# The objective said "reaching the gold time scores 1.0, the bronze time
# scores the finish floor". The scorer gives gold 2.0 and bronze 1.0, so both
# halves named the wrong number -- an agent reasoning about whether a faster
# lap was worth the risk was reasoning off a scale that does not exist. This
# is the same split-brain as scoring A03 against A02's medals: two files, one
# fact, nothing checking they agree.
# ---------------------------------------------------------------------------
def test_the_objective_states_the_score_the_scorer_actually_gives(cfg):
    text = " ".join(cfg["objective"].split())
    for medal, expected in (("gold_seconds", 2.0), ("bronze_seconds", 1.0)):
        s = make_scorer(cfg, clock=None,
                        finish=f"新的时间 0:{cfg['scorer']['track'][medal]:05.2f}")
        got = s.score_for(read_confirmed(s))
        assert got == pytest.approx(expected, abs=0.01), (medal, got)
        assert f"{expected:.1f}" in text, (
            f"the objective never names {expected:.1f}, which is what "
            f"{medal} is worth")
