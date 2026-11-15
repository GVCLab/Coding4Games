"""A single misread odometer frame must not become a permanent score.

`best_distance` is a high-water mark, and the comment on it reasons only about
readings that come out too LOW ("a drop is a respawn or a misread"). Nothing
guarded the other direction, and max() makes a high misread permanent.

Measured on 2026-08-25, from the two 2 h arms:

  * one lang episode scored 0.5024 -- 6x every other episode in the run -- and
    ONE act contributed 96% of it: a jump of ~769 m of an 860 m track between
    two reads, after which the car made no progress for 130 tics;
  * six of 27 episodes across both arms were inflated the same way;
  * the reported headline is a MAX over episodes, so the bug preferentially
    selected the episodes it had corrupted. "Best score" was the most
    contaminated statistic in the run.

The guard is physical: the race clock says how much time passed, and a car
cannot beat 1.6x gold-medal pace over any interval. These tests pin that it
rejects the impossible, keeps the merely fast, and never invents a number.
"""
from __future__ import annotations


from coding4game_gui.benchmark.windows.scorers.race import RaceScorer

# A02-Race, as the scenario declares it: 860 m, gold 17.27 s.
# Gold pace is 49.8 m/s, so the default ceiling is ~79.7 m/s.
SPEC = {
    "calibrated_client_size": [1280, 720],
    "finish_floor": 0.6,
    "regions": {n: {"rect": [0, 0, 10, 10]}
                for n in ("clock", "distance", "finish_time", "track", "finish_clock", "finish_banner")},
    "track": {"gold_seconds": 17.27, "silver_seconds": 19.47,
              "bronze_seconds": 24.28, "track_length_m": 860},
}

# A03-Race, 1521 m. Needed because the odometer cannot read past the end of
# the track: a fixture that drives 1269 m of an 860 m track is not a fast car,
# it is an impossible one, and the track-length ceiling (correctly) throws it
# out before the speed guard ever sees it. Two tests below used to do exactly
# that -- they were written for the A03 lap and left pointed at A02's spec --
# and the new guard is what surfaced it.
A03_SPEC = dict(SPEC, track={"gold_seconds": 19.84, "silver_seconds": 22.29,
                             "bronze_seconds": 28.61,
                             "track_length_m": 1521})


class _Scorer(RaceScorer):
    """The real logic, fed readings instead of pixels."""

    def __init__(self, samples, spec=None):
        self._samples = samples
        self._i = 0
        super().__init__(spec or SPEC, (1280, 720), ocr=lambda crop: [])

    def _text_in(self, frame, name):
        s = self._samples[self._i]
        text = {"clock": "" if s[0] is None else f"0:{s[0]:05.2f}",
                "distance": "" if s[1] is None else f"{s[1]}m"}.get(name, "")
        self._texts[name] = text
        return text

    def play(self):
        for _ in self._samples:
            self.read(object())
            self._i += 1
        return self


def test_the_769_metre_jump_is_rejected():
    """The episode that produced the headline. Clock advances 0.4 s while the
    odometer claims 769 m -- 1900 m/s."""
    s = _Scorer([(6.0, 28), (6.4, 797), (6.8, 31), (7.2, 34)]).play()
    assert s.best_distance == 34, s.best_distance
    assert s.rejected_jumps == 1
    assert s.total < 0.05, "a rejected frame must not survive in the score"


def test_a_genuinely_fast_car_is_not_punished():
    """70 m/s is under the ceiling and must pass, or the guard would delete
    real progress -- the failure mode that would be worse than the bug."""
    s = _Scorer([(1.0, 10), (2.0, 80), (3.0, 150), (4.0, 220)]).play()
    assert s.best_distance == 220
    assert s.rejected_jumps == 0


def test_a_rejected_reading_is_discarded_not_replaced():
    """It must never guess. After a rejection the high-water mark is exactly
    what the last BELIEVED frame said."""
    s = _Scorer([(5.0, 100), (5.2, 850)]).play()
    assert s.best_distance == 100
    assert s.last_reading.distance is None


def test_two_misreads_in_a_row_cannot_ratchet():
    """The ceiling is measured from the last ACCEPTED reading. Measuring from
    the previous frame instead would let a run of misreads walk upward, each
    one only a little beyond the last."""
    s = _Scorer([(5.0, 100), (5.1, 700), (5.2, 780), (5.3, 830)]).play()
    assert s.best_distance == 100
    assert s.rejected_jumps == 3


def test_a_respawn_does_not_look_like_a_jump():
    """Distance drops and the clock may restart; neither is a misread, and the
    high-water mark already covers it."""
    s = _Scorer([(9.0, 300), (0.5, 5), (1.0, 40)]).play()
    assert s.best_distance == 300
    assert s.rejected_jumps == 0


def test_the_guard_still_applies_without_medal_times():
    """This used to assert the opposite -- no medals, no opinion -- because
    the ceiling was derived from gold pace. It is now an absolute physical
    bound, so a track that declares no medal times still cannot be driven at
    8400 m/s. Better: an unknown track is exactly where a fabricated number
    has nothing to contradict it."""
    spec = dict(SPEC, track={"track_length_m": 860})
    s = _Scorer([(1.0, 10), (1.1, 850)], spec=spec).play()
    assert s.best_distance == 10
    assert s.rejected_jumps == 1


def test_a_clockless_frame_still_catches_a_track_sized_jump():
    """Distance and clock come off the same HUD, but if only the clock fails
    the guard must not fall open completely."""
    s = _Scorer([(5.0, 30), (None, 900)]).play()
    assert s.best_distance == 30
    assert s.rejected_jumps == 1


# The A03-Race human lap of 2026-08-27, verbatim from its session.jsonl:
# 1467 m in 34.32 s. Average 42.7 m/s, peak 104 m/s -- the peak is 2.4x the
# average, which is why a ceiling derived from a lap's average pace is wrong
# in principle. The first version used gold-pace x 1.6 = 80 m/s and rejected
# four consecutive REAL readings, costing this lap its last 200 m.
A03_HUMAN_LAP = [
    (14.45, 40), (15.37, 74), (16.30, 116), (17.10, 163), (18.09, 222),
    (19.09, 295), (20.01, 367), (20.92, 422), (21.91, 476), (22.80, 521),
    (23.68, 537), (24.55, 627), (25.52, 688), (27.57, 822), (28.49, 894),
    (29.46, 978), (30.36, 1061), (31.41, 1163), (32.43, 1269),
]


def test_a_real_human_lap_is_not_rejected_anywhere():
    """The regression that motivated the rewrite. Every one of these readings
    is real driving, and several exceed 100 m/s."""
    s = _Scorer(A03_HUMAN_LAP, spec=A03_SPEC).play()
    assert s.rejected_jumps == 0, "the guard ate real driving"
    assert s.rejected_over_length == 0
    assert s.best_distance == 1269


def test_the_ceiling_is_an_absolute_bound_not_a_derived_one():
    """150 m/s is 540 km/h -- unreachable for this game's car -- and it does
    not move when a scenario declares different medals, because a lap average
    says nothing about a peak on a straight."""
    s = _Scorer([(1.0, 1)])
    assert s.max_speed_mps == 150.0
    slow = _Scorer([(1.0, 1)], spec=dict(SPEC, track={
        "gold_seconds": 90.0, "bronze_seconds": 120.0, "track_length_m": 500}))
    assert slow.max_speed_mps == 150.0


def test_one_wrong_rejection_does_not_cost_the_rest_of_the_lap():
    """The cascade. The anchor used to stay put, so the gap to it grew with
    every frame and everything after the first rejection was rejected too --
    which is exactly what ate the A03 lap's final 200 m.

    A run of rejections is also the shape that tells the causes apart: the
    misread this guard exists for was ONE frame with good readings either
    side, while a car faster than the ceiling produces an unbroken sequence.

    Every value stays inside the track, which is the only place the re-anchor
    can still be reached: past the end of the track the length ceiling rejects
    outright and never re-anchors. This fixture used to run to 3500 m of an
    860 m track, so it was testing the speed guard with readings the odometer
    could not physically produce.
    """
    s = _Scorer([(5.0, 100), (5.1, 400), (5.2, 700), (5.3, 1000),
                 (5.4, 1300), (5.5, 1600)], spec=A03_SPEC).play()
    assert s.rejected_jumps == 4, s.rejected_jumps
    # Re-anchored once, at the fourth reading. NOT "accepts everything after":
    # the fifth is another impossible jump measured from the new anchor and is
    # rejected again.
    assert s.best_distance == 1300, s.best_distance


def test_a_sustained_impossible_ramp_still_ratchets_slowly(caplog):
    """The deliberate cost of re-anchoring, stated rather than hidden.

    A fabricated monotonic ramp and a genuinely fast car look identical to a
    streak rule, so re-anchoring lets a ramp through at one reading in
    `resync_after + 1`. That is accepted because the ceiling is now a physical
    impossibility rather than a guess: nothing real sustains 540 km/h, so a
    streak means the ceiling is MISCONFIGURED for this game, and recovering is
    worth more than the residue. If it ever fires on trackmania, the number to
    look at is max_speed_mps, and the log line says so.

    The residue is now bounded, though, which is the point of the test that
    follows: a ramp can only ratchet as far as the end of the track.
    """
    s = _Scorer([(1.0, 0)] + [(1.0 + 0.1 * i, 200 * i)
                              for i in range(1, 9)], spec=A03_SPEC).play()
    # Eight impossible readings offered, six rejected: it gets through at one
    # in four (the 4th and the 8th), which is what re-anchoring costs.
    assert s.rejected_jumps == 6, s.rejected_jumps
    assert s.best_distance == 1600, s.best_distance


# ---------------------------------------------------------------------------
# The root cause, found by re-reading the spiked episode's own replay video.
# Its frames show a smooth 85m -> 87 -> 88 -> 90 -> 93 -> 96 -> 97 -> 100 ...
# There was never an 800 in the pixels. What the crops DO show is loose OCR
# fragments beside the number -- '87m 1' and '0:05.4 6.45' are real captures
# from that episode -- and parse_metres used to delete every space before
# matching, which merges a fragment into the digits.
# ---------------------------------------------------------------------------
from coding4game_gui.benchmark.windows.scorers.race import parse_metres


def test_a_stray_fragment_beside_the_number_does_not_merge_into_it():
    """'7 97m' must be 97, not 797. Stripping spaces made it 797 -- a 700 m
    gain between two frames, which is the whole 0.5024 episode."""
    assert parse_metres("7 97m") == 97
    assert parse_metres("8 97m") == 97
    assert parse_metres("97m 8") == 97


def test_the_real_captures_from_that_episode_still_parse():
    """Taken verbatim from the replay: the fix must not cost the ordinary
    reading it was protecting."""
    assert parse_metres("87m 1") == 87
    assert parse_metres("85m") == 85
    assert parse_metres("100m") == 100


def test_a_space_before_the_unit_still_parses():
    """The only thing the strip was ever claimed to buy. METRES_RE already
    allows the whitespace, so it bought nothing."""
    assert parse_metres("116 m") == 116
    assert parse_metres("116m") == 116


def test_the_first_reading_of_an_episode_is_never_rejected():
    """Before anything is believed there is no speed to compute.

    _accepted_distance starts at 0 and _accepted_clock at None, so the first
    real reading used to be measured against "0 m, time unknown" and judged
    by the no-clock window -- rejecting any episode whose first readable
    odometer value exceeded ~475 m. Not exotic: the opening frames often have
    no readable HUD (20 of 57 racing acts in the A03 human recording had no
    distance), so the first value that DOES read can be well into the lap.

    Measured before the fix: an opening at 1200 m was rejected three times
    running and recovered only by re-anchoring, losing the progress between.
    """
    s = _Scorer([(10.0, 1200), (11.0, 1240), (12.0, 1280)],
                spec=A03_SPEC).play()
    assert s.rejected_jumps == 0, "the opening reading has nothing to beat"
    assert s.best_distance == 1280


def test_an_impossible_jump_is_still_caught_after_the_anchor_exists():
    """The negative control for the above: accepting the FIRST reading must
    not amount to accepting the second."""
    s = _Scorer([(10.0, 100), (10.2, 900)]).play()
    assert s.rejected_jumps == 1
    assert s.best_distance == 100


# ---------------------------------------------------------------------------
# THE SECOND CEILING: the track has an end.
#
# The speed guard deliberately has no opinion on the first believed reading of
# an episode -- there is nothing to measure a speed against, and the opening
# frames often have no readable HUD, so the first value that DOES read can be
# well into the lap. That is right, and it leaves the reading everything else
# is anchored to completely unchecked. On the A03 run of 2026-08-27 one
# episode was credited 1907 m of a lap whose true crossing reading was 1513.
#
# The track's length answers on frame one because it needs no history. It is
# also a fact rather than a guess, which is why -- unlike the speed ceiling --
# it does not re-anchor: measured on the two-lap human session of 2026-08-21,
# the odometer RESETS each lap (lap 1 ended at 839 m, lap 2 started from zero
# and ended at 835 m), so it is per-lap progress and there is nowhere past the
# line to have driven.
# ---------------------------------------------------------------------------
def test_a_reading_past_the_end_of_the_track_is_rejected():
    """1907 m of a 1521 m track. The number is from the run."""
    s = _Scorer([(30.0, 1400), (36.55, 1907)], spec=A03_SPEC).play()
    assert s.best_distance == 1400
    assert s.rejected_over_length == 1
    assert s.last_reading.distance is None


def test_the_track_length_guard_answers_on_the_very_first_reading():
    """The gap the speed guard cannot cover. Nothing has been believed yet,
    so `_implausible_jump` abstains -- and that is exactly when a bad anchor
    does the most damage, because everything after is measured from it."""
    s = _Scorer([(36.55, 1907), (37.0, 1910)], spec=A03_SPEC).play()
    assert s.best_distance == 0
    assert s.rejected_over_length == 2
    assert s.rejected_jumps == 0, "this is not a speed question"


def test_a_finish_line_reading_is_not_mistaken_for_an_overrun():
    """The declared length is a measurement and can be a little short: A03's
    was 1467 while real crossings read 1509-1521. The 1.1x headroom is what
    stops a guard about impossible readings from eating a completed lap."""
    s = _Scorer([(30.0, 1400), (32.75, 1521), (33.5, 1600)],
                spec=A03_SPEC).play()
    assert s.rejected_over_length == 0
    assert s.best_distance == 1600


def test_a_short_burst_of_overruns_is_rejected():
    """A misread or two past the end of the track is thrown away."""
    s = _Scorer([(20.0, 900)] + [(20.1 + 0.1 * i, 5000 + i)
                                 for i in range(4)], spec=A03_SPEC).play()
    assert s.best_distance == 900, "an overrun must not become the record"
    assert s.rejected_over_length == 4


def test_a_sustained_overrun_eventually_gives_up_rather_than_freezing():
    """The guard must be able to be WRONG about its own premise.

    This used to assert the opposite -- that an over-length reading can never
    ratchet through, because "the track's length is a fact, not a guess about
    the car". A red-team pass took that premise apart: the odometer resetting
    each lap shows the counter is per-RUN, not that it reports POSITION
    rather than PATH. The scenario's own gameplay text calls it "distance
    travelled", and an episode is 100 s against a 33-57 s lap, so a car that
    wanders or respawns has room to cover more than a track length without
    finishing.

    If that reading is right, a permanent guard is a disaster and a silent
    one: best_distance freezes, `far_enough` (0.9 x length) never becomes
    true, the frozen-clock finish path can never fire, and a completed lap
    scores below the floor. Measured on a constructed case: 12 rejections, a
    genuine finish afterwards never detected, 0.4260 for a finished lap.

    So it gives up, like the speed ceiling does. The cost is bounded ratchet;
    the cost of the alternative is an unbounded, invisible loss.
    """
    s = _Scorer([(20.0, 900)] + [(20.1 + 0.1 * i, 5000 + i)
                                 for i in range(12)], spec=A03_SPEC).play()
    assert s.rejected_over_length == 6, s.rejected_over_length
    assert s.best_distance > 900, "a sustained reading must not freeze it"


def test_an_overrun_does_not_arm_the_speed_guards_escape_hatch():
    """The two guards used to share one counter and not compose.

    Three speed rejections arm the re-anchor. An over-length frame in between
    neither incremented nor cleared that counter, so the armed state survived
    it and was spent on the next in-band reading with the speed ceiling
    disabled -- accepting 1670 m from a car that had honestly driven 100, and
    logging it as "re-anchoring after 3 consecutive rejections" that were four
    frames earlier.
    """
    s = _Scorer([(10.0, 100), (10.1, 800), (10.2, 900), (10.3, 1000),
                 (10.4, 5000), (10.5, 1670)], spec=A03_SPEC).play()
    assert s.best_distance == 100, (
        f"an over-length frame must disarm the re-anchor, got "
        f"{s.best_distance}")


def test_no_declared_length_means_no_opinion():
    """Same principle as the rest of the file: a guard that cannot measure
    must not reject. A scenario with no track length still scores."""
    spec = dict(SPEC, track={"gold_seconds": 17.27, "bronze_seconds": 24.28})
    s = _Scorer([(1.0, 10), (2.0, 60), (3.0, 110)], spec=spec).play()
    assert s.rejected_over_length == 0
    assert s.best_distance == 110
