"""Score TrackMania from the calibrated race HUD and results panel.

The finish overlay corroborates a stopped clock before the replay hides the
HUD. The results panel can also settle a lap after the clock and distance
disappear. Finished laps use the track's medal times and outrank progress.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

# OCR can render a time separator as a dot or comma.
_SEP = r"[:：.．,，]"
TIME_RE = re.compile(rf"(\d+)\s*{_SEP}\s*(\d{{2}})\s*{_SEP}\s*(\d{{2}})")
METRES_RE = re.compile(r"(\d+)\s*m", re.IGNORECASE)
BARE_INT_RE = re.compile(r"(\d+)")


def parse_time(text: str) -> float | None:
    """'New time 0:31.16' -> 31.16. Returns None when no clock is present."""
    m = TIME_RE.search(text or "")
    if not m:
        return None
    minutes, seconds, hundredths = m.groups()
    return int(minutes) * 60 + int(seconds) + int(hundredths) / 100.0


def parse_metres(text: str) -> int | None:
    """'116m' -> 116. Falls back to a bare integer, because the trailing 'm'
    is small and OCR drops it more often than it drops the digits.

    DO NOT strip the spaces first. It used to, and that is what turned one
    stray OCR fragment into a 20x score: this reader emits loose fragments
    next to the number it wanted -- '87m 1' and '0:05.4 6.45' are both real
    captures from one episode -- and deleting the spaces MERGES them, so
    '7 97m' becomes '797m' and the car gains 700 metres between two frames.
    That is precisely the 769 m jump that carried 96% of a 0.5024 episode.

    Stripping was never needed: METRES_RE already allows whitespace before
    the 'm', so '116 m' matched with or without it. The line bought nothing
    and cost the run.
    """
    m = METRES_RE.search(text or "") or BARE_INT_RE.search(text or "")
    return int(m.group(1)) if m else None


@dataclass
class Reading:
    """What one frame says. Every field may be None -- OCR is fallible and a
    scorer that pretends otherwise turns a misread into a fabricated score."""

    clock: float | None = None          # running race time, seconds
    distance: int | None = None         # metres travelled
    finish_time: float | None = None    # set only on the results screen
    track: str | None = None            # for the wrong-track guard
    finished: bool = False

    @property
    def in_race(self) -> bool:
        return self.clock is not None and not self.finished


class RaceScorer:
    """Reads the HUD and turns a lap into a score.

    `spec` is the scenario's `scorer:` block, which carries the calibration
    (rects, client size) AND the track under `scorer.track` — medal times are
    reward parameters, and the game-spec boundary puts those in the scenario's
    scorer block rather than at the top level. `ocr` is any callable
    frame -> [{text, conf, box}], i.e. PaddleOcrEngine. The signature matches
    the other Windows scorers so `ResetController.make_scorer` builds them all
    the same way.
    """

    def __init__(self, spec: dict, client_size, ocr):
        self.spec = spec or {}
        self.track = self.spec.get("track") or {}
        self.client_size = tuple(client_size)
        cal = tuple(self.spec.get("calibrated_client_size") or client_size)
        self.rect_scale = (self.client_size[0] / cal[0],
                           self.client_size[1] / cal[1])
        self.regions = self.spec.get("regions") or {}
        if not {"finish_clock", "finish_banner"} <= self.regions.keys():
            raise ValueError("finish_clock and finish_banner must be configured together")
        self._finish_banner_pattern = re.compile(
            self.regions["finish_banner"].get("pattern", "FINISH"), re.IGNORECASE)
        self.finish_confirm_frames = int(
            self.spec.get("finish_confirm_frames", 2))
        if "finish_distance_ratio" in self.spec:
            raise ValueError("finish_distance_ratio is retired. Configure the calibrated finish overlay.")
        # THE CEILING ON PROGRESS, in metres per second of RACE CLOCK.
        #
        # best_distance is a high-water mark, and the comment on it reasons
        # only about readings that come out too LOW ("a drop is a respawn or a
        # misread"). Nothing defended the other direction, and max() makes a
        # single high misread permanent: on 2026-08-25 one act added 769 m of
        # an 860 m track and carried 96% of that episode's entire score, then
        # the car made no further progress for 130 tics. Six of 27 episodes
        # across the two 2 h arms were inflated this way, and because the
        # reported headline is a MAX over episodes, the bug preferentially
        # selected the episodes it had corrupted.
        #
        # An ABSOLUTE ceiling, not one derived from the medal times. The first
        # version used "gold pace x 1.6" and that is wrong in principle: a
        # lap's AVERAGE includes the standing start and every corner, while
        # the peak on a straight is far higher. Measured on the A03 human lap
        # of 2026-08-27 -- 1467 m in 34.32 s, so 42.7 m/s average, with
        # segments at 103, 97 and 104 m/s. The peak is 2.4x the average, and
        # the derived 80 m/s ceiling rejected four consecutive REAL readings
        # and cost the run its last 200 m.
        #
        # It would have done the same on A02 as soon as any agent got up to
        # speed; it had simply never been reached, because nothing had driven
        # past ~100 m of the track.
        #
        # 150 m/s is 540 km/h, which this game's stadium car cannot reach. The
        # bound does not need to be tight: the misread it exists to catch was
        # 769 m between two frames 0.4 s apart -- about 1900 m/s, three orders
        # of magnitude above anything real. A loose impossible bound catches
        # that with no false positives; a tight plausible one deletes real
        # driving.
        self.max_speed_mps = float(self.spec.get("max_speed_mps", 150.0))
        # Slack for one reading's worth of quantisation, so a legitimate
        # reading is never rejected merely for landing on a rounding boundary.
        self.jump_margin_m = float(self.spec.get("jump_margin_m", 25.0))
        # How many consecutive rejections mean the CEILING is wrong rather
        # than the reader. An OCR misread is one frame with good readings
        # either side (measured: the 769 m spike had 85/87/88/90 m around it);
        # a car faster than the ceiling produces an unbroken run.
        self.resync_after = int(self.spec.get("resync_after", 3))
        # THE OTHER CEILING, and the only one that needs no history.
        #
        # It exists because the speed guard cannot answer on frame one. Its
        # "no anchor, no opinion" rule (see _implausible_jump) is right — the
        # opening frames routinely have no readable HUD, so the first value
        # that DOES read can be well into the lap — but it leaves the first
        # believed reading completely unchecked, and that reading becomes the
        # anchor everything after is measured from. On the A03 run of
        # 2026-08-27 one episode was credited 1907 m of a lap whose crossing
        # frames read 1513.
        #
        # WHAT THIS IS NOT: a statement that the odometer cannot exceed the
        # track. It was written as one — "the odometer resets each lap, so
        # there is nowhere further to have driven" — and that does not follow.
        # The reset says the counter is per-RUN; it says nothing about whether
        # the counter integrates PATH or reports POSITION. The evidence that
        # looked decisive is not: four crossings reading 1509/1514/1514/1521
        # is equally well explained by which frame happened to be sampled at
        # ~3 m per frame. The scenario's own gameplay text calls it "distance
        # travelled", and an episode is 100 s against a 33-57 s lap, so a car
        # that wanders or respawns has room to cover well over a track length
        # without finishing.
        #
        # So the guard stays, because 1907 m on a lap that crossed at 1513 is
        # a misread either way — but it is RECOVERABLE. A sustained run of
        # over-length readings now gives up and accepts, exactly like the
        # speed ceiling, because the alternative is the failure this file
        # keeps relearning: the high-water mark freezes, `far_enough` never
        # becomes true, and a genuinely completed lap is scored below the
        # finish floor. A guard that cannot be wrong about a premise I cannot
        # prove must not be allowed to silently delete a lap.
        self.distance_ceiling_ratio = float(
            self.spec.get("distance_ceiling_ratio", 1.1))
        self.over_length_resync = int(
            self.spec.get("over_length_resync_after", 6))
        # How many consecutive readouts-gone frames before the menu crops are
        # worth paying for again. Small: the cost of waiting is that a menu
        # goes unread for a few frames of a cold path that runs for hundreds.
        self.menu_read_after_frames = int(
            self.spec.get("menu_read_after_frames", 3))
        self.ocr = ocr
        self.reset()

    # ---------------------------------------------------------------- state
    def reset(self) -> None:
        self.best_distance = 0
        self.last_reading = Reading()
        self._last_score = 0.0
        # Delta's OWN baseline. Separate from _last_score because other
        # callers (is_terminal on mid-act scan frames) move that one; see
        # delta().
        self._delta_baseline = 0.0
        self._finish_streak = 0
        self._stopped_streak = 0
        self._last_clock = None
        self._final_time = None
        self._finish_overlay_clock = None
        self._texts: dict[str, str] = {}
        self._cache_frame = None       # identity of the frame last read
        # The clock and distance of the last reading we BELIEVED, which is what
        # a plausibility ceiling has to be measured from -- measuring from a
        # rejected reading would let two consecutive misreads ratchet through.
        self._accepted_clock = None
        self._accepted_distance = 0
        # Has ANY distance been believed yet this episode? Before the first
        # one there is nothing to measure a speed against, and treating the
        # absent anchor as "0 m at time unknown" makes the opening reading
        # look like the car teleported. See _implausible_jump.
        self._anchored = False
        self._reject_streak = 0        # consecutive rejections; see _should_resync
        self.rejected_jumps = 0        # surfaced in describe(); never silent
        self.rejected_over_length = 0  # counted apart: a different question
        self._over_length_streak = 0   # see _over_track_length's escape hatch
        self._restart_streak = 0       # consecutive "a new run started" reads
        self._seen_race = False        # has a HUD ever been up this episode?
        self._no_hud_streak = 0        # consecutive reads with neither readout

    # ---------------------------------------------------------------- rects
    def scale_rect(self, rect):
        sx, sy = self.rect_scale
        x, y, w, h = rect
        return [int(round(x * sx)), int(round(y * sy)),
                int(round(w * sx)), int(round(h * sy))]

    def _crop_rect(self, frame: np.ndarray, rect) -> np.ndarray | None:
        x, y, w, h = self.scale_rect(rect)
        fh, fw = frame.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(fw, x + w), min(fh, y + h)
        if x1 <= x0 or y1 <= y0:
            return None
        return frame[y0:y1, x0:x1]

    def _crop(self, frame: np.ndarray, name: str) -> np.ndarray | None:
        region = self.regions.get(name)
        if not region:
            return None
        return self._crop_rect(frame, region["rect"])

    def _text_in(self, frame: np.ndarray, name: str) -> str:
        crop = self._crop(frame, name)
        if crop is None or crop.size == 0:
            text = ""
        else:
            text = " ".join(r["text"] for r in self.ocr(crop))
        self._texts[name] = text
        return text

    # ---------------------------------------------------------------- read
    # Regions read for the SCORE. Everything else declared in the scenario is
    # navigation-only; see _read_menu_regions.
    SCORING_REGIONS = ("clock", "distance", "track", "finish_time",
                       "finish_clock", "finish_banner")

    def _read_menu_regions(self, frame: np.ndarray, *, in_race: bool) -> None:
        """Read the non-scoring regions, but only while no race is on screen.

        The cold path walks pages this scorer otherwise cannot see at all, and
        `check_text` deliberately answers only about declared regions — so a
        menu_skip step's `expect:` had nothing to match, and a 26-step path
        could end four screens short reporting success. Declaring menu regions
        fixes that; reading them EVERY frame would not be free, because these
        crops are large and the act loop reads once per tic.

        The condition is the same structural signal the phase classifier uses:
        a readable clock means a race, and a race has no menu to anchor on. So
        this costs nothing during the part of an episode that is scored, and
        pays only in the menus, where nothing is scored yet.
        """
        if in_race:
            # Drop any stale menu text so check_text cannot answer about a
            # page that is no longer on screen.
            for name in self.regions:
                if name not in self.SCORING_REGIONS:
                    self._texts.pop(name, None)
            return
        for name in self.regions:
            if name not in self.SCORING_REGIONS:
                self._text_in(frame, name)

    def read(self, frame: np.ndarray) -> Reading:
        r = Reading()
        r.clock = parse_time(self._text_in(frame, "clock"))
        r.distance = parse_metres(self._text_in(frame, "distance"))
        r.track = (self._text_in(frame, "track") or "").strip() or None

        finish_text = self._text_in(frame, "finish_time")
        finish = parse_time(finish_text)
        # Three conditions, then a debounce, because a false finish silently
        # truncates an episode and scores it as complete.
        #
        #   clock gone     -- a paused race still draws its clock
        #   distance gone  -- the results screen drops both readouts, and this
        #                     is what separates a finish from one OCR miss on
        #                     the clock: the personal-best row sits right under
        #                     the finish-time rect and reads as a time
        #   a time present -- the results screen states it outright
        #
        # A single dropped frame used to end an episode at 124m and score it as
        # finished, so the condition must hold on consecutive reads.
        candidate = (finish is not None and r.clock is None
                     and r.distance is None and self._credible_finish(finish))
        self._finish_streak = self._finish_streak + 1 if candidate else 0
        if candidate and self._finish_streak >= self.finish_confirm_frames:
            r.finished = True
            r.finish_time = finish

        # A paused race also repeats the clock. Require a matching finish
        # overlay and enough distinct captures to confirm the stopped clock.
        stopped = r.clock is not None and r.clock == self._last_clock
        for name in ("finish_clock", "finish_banner"):
            self._texts.pop(name, None)
        credible = self._credible_finish(r.clock)
        overlay_matches = (credible and self._finish_overlay_matches(frame, r.clock))
        stopped_candidate = stopped and credible
        self._stopped_streak = self._stopped_streak + 1 if stopped_candidate else 0
        if (not r.finished
                and self._stopped_streak >= self.finish_confirm_frames
                and overlay_matches):
            r.finished = True
            r.finish_time = r.clock
        # A LAP THAT FINISHED STAYS FINISHED. This memory already existed so
        # that the frames after the HUD vanishes still know the time, but it
        # only applied when the crop went blank (`clock is None and distance
        # is None`), and a finish can also be undone by a frame that reads
        # BADLY rather than not at all.
        #
        # Measured on ep1 of the A03 run, its last two video frames:
        #
        #   #646  clock "0:57.36"  distance "1514m"   -> finished, streak 5
        #   #647  clock "0:57.36"  distance "15:4m"   -> parses as 4 m
        #
        # The odometer's OCR picked up a colon, `stopped` compared 4 against
        # 1514, the streak reset, and an episode that had been reporting
        # finished for five consecutive frames reported unfinished again. In
        # production this is invisible -- the env ends the episode on the
        # first terminal frame, so frame 647 is never read -- but every
        # offline pass over the same pixels got a different answer from the
        # run, and I twice explained that gap wrongly (sampling rate, then
        # lossy transcoding) before reading the frames.
        #
        # The only thing that legitimately un-finishes a lap is a NEW run
        # starting, which shows up as the race clock going back to near zero.
        # A respawn does not: it returns the car to a checkpoint and the clock
        # keeps running.
        if r.finished and r.finish_time is not None:
            self._final_time = r.finish_time
        elif self._final_time is not None:
            if self._race_restarted(r):
                # FORGET it, rather than merely skipping this frame. A new lap
                # climbs past the old finish time within seconds, so a rule
                # that only declined the frames below it would start reporting
                # the PREVIOUS lap's finish partway through this one.
                self._final_time = None
            else:
                r.finished = True
                r.finish_time = self._final_time

        if r.clock is not None:
            self._last_clock = r.clock

        if r.distance is not None:
            # Distance never decreases within a lap; a drop is a respawn or a
            # misread, and either way the high-water mark is the honest
            # progress figure. But the high-water mark is exactly why a reading
            # that comes out too HIGH is permanent, so it has to clear a
            # physical ceiling first -- see max_speed_mps.
            if (self._over_track_length(r)
                    and self._over_length_streak < self.over_length_resync):
                self._over_length_streak += 1
                self.rejected_over_length += 1
                # Reset the SPEED guard's streak too. Otherwise an armed
                # re-anchor survives any number of over-length frames and is
                # spent on whatever in-band reading comes next, with the speed
                # ceiling disabled — the two guards shared one counter and did
                # not compose. Measured: 100m, three rejections, one 5000m
                # frame, then 1670m accepted as a re-anchor "after 3
                # consecutive rejections" that were four frames earlier.
                self._reject_streak = 0
                print(f"[race] rejected distance {r.distance}m: beyond "
                      f"{self.distance_ceiling_ratio:.2f}x the "
                      f"{self.track.get('track_length_m')}m track",
                      flush=True)
                r.distance = None
            elif self._implausible_jump(r) and not self._should_resync():
                self.rejected_jumps += 1
                self._reject_streak += 1
                print(f"[race] rejected distance {r.distance}m at clock "
                      f"{r.clock}: more than {self.max_speed_mps:.0f} m/s "
                      f"since {self._accepted_distance}m at "
                      f"{self._accepted_clock}", flush=True)
                r.distance = None      # the frame said nothing believable
            else:
                if self._reject_streak >= self.resync_after:
                    # RE-ANCHOR. One wrong rejection used to be unrecoverable:
                    # the anchor stayed put, the gap to it grew with every
                    # frame, and so every later reading was rejected too. On
                    # the A03 lap that cascade ate the final 200 m.
                    #
                    # A run of rejections is also the shape that tells the two
                    # causes apart. The misread this guard exists for was ONE
                    # frame with correct readings either side; a car that is
                    # genuinely faster than the ceiling produces a sustained,
                    # monotonic sequence. So a streak means the ceiling was
                    # wrong, not the reader.
                    print(f"[race] re-anchoring after {self._reject_streak} "
                          f"consecutive rejections: {r.distance}m at clock "
                          f"{r.clock} (the ceiling, not the reader, was wrong)",
                          flush=True)
                self._reject_streak = 0
                self._anchored = True
                self._accepted_distance = max(self._accepted_distance,
                                              r.distance)
                if r.clock is not None:
                    self._accepted_clock = r.clock
                self.best_distance = max(self.best_distance, r.distance)
        self.last_reading = r
        # Either HUD reading identifies a race. Allow brief OCR dropouts
        # before the menu path runs a full-screen read.
        hud = r.clock is not None or r.distance is not None
        if hud:
            self._seen_race = True
            self._no_hud_streak = 0
        else:
            self._no_hud_streak += 1
        self._read_menu_regions(frame, in_race=hud or (
            self._seen_race
            and self._no_hud_streak < self.menu_read_after_frames))
        # Keep the running score current on every read. It used to update only
        # inside delta(), so a caller that read frames without asking for step
        # rewards saw total() stay at zero while describe() printed the real
        # number -- which is exactly how the first smoke run "scored" 0.000
        # after driving 125m.
        self._last_score = self.score_for(r)
        return r

    def _finish_overlay_matches(self, frame: np.ndarray, clock: float) -> bool:
        """Confirm the calibrated finish signal independently of HUD stillness."""
        if self._finish_overlay_clock == clock:
            return True
        # The banner is brief, whereas the stopped HUD remains for three
        # seconds. Preserve corroboration only while this exact clock value
        # persists, so ACT-sized observations can confirm the same finish.
        self._finish_overlay_clock = None
        overlay_time = parse_time(self._text_in(frame, "finish_clock"))
        if overlay_time != clock:
            return False
        banner = self._text_in(frame, "finish_banner")
        if self._finish_banner_pattern.search(banner) is None:
            return False
        self._finish_overlay_clock = clock
        return True

    def _credible_finish(self, t: float | None) -> bool:
        """Could the track have been completed in this many seconds?

        A physical floor, from the same two numbers the speed guard uses:
        `track_length_m / max_speed_mps`. On A03 that is 1521/150 = 10.1 s,
        against a gold time of 19.84 — a factor of two of headroom, so this
        cannot touch a real lap however fast.

        It exists because a finish time is not just a label, it is the SCORE:
        the medal scale runs backwards from bronze, so a clock misread low
        pays out enormously. Constructed on the current numbers, a spurious
        finish detected at 3.20 s scored 3.897 on a 0-1 benchmark, and the
        headline is a max over episodes. Rejecting the reading is better than
        clamping the payout, because a 3.20 s lap of a 1521 m track is not a
        fast lap that needs its score bounded — it is not a lap.
        """
        if t is None:
            return False
        length = float(self.track.get("track_length_m", 0) or 0)
        if length <= 0 or self.max_speed_mps <= 0:
            return True                  # nothing to measure against
        return t >= length / self.max_speed_mps

    def _race_restarted(self, reading: Reading) -> bool:
        """Is a new run on screen, rather than the aftermath of the last one?

        THE CLOCK ALONE IS NOT ENOUGH, and the first version of this made
        exactly the mistake it was written to fix. It asked only whether the
        clock had gone back, off a raw OCR read — and this game's clock is
        documented as dropping the minutes digit past a minute, so a lap
        finishing at 1:05.42 with one frame misread as 0:05.42 would clear the
        recorded finish PERMANENTLY (the clearing latches) and score a
        completed lap at 0.5357, below the floor. That is the same shape as
        the odometer misread this stickiness exists to survive, moved one
        field across, and worse: the old code recovered on the next blank
        frame, this would not.

        The asymmetry decides the design. A restart missed for a few frames
        costs a stale reading; a restart imagined once costs the whole lap.
        So a restart must be CORROBORATED before it is believed:

          * the clock went back, AND
          * the odometer went back with it — a new run starts at zero, and no
            single-field misread moves both, AND
          * it held for `finish_confirm_frames` consecutive reads, the same
            debounce the finish itself has to clear.
        """
        if self._final_time is None:
            return False
        clock_back = (reading.clock is not None
                      and reading.clock < self._final_time - 1.0)
        # A tenth of the distance already covered: a new run reads near zero,
        # while the frozen HUD after the line still reads the full lap.
        dist_back = (reading.distance is not None
                     and reading.distance < 0.1 * max(1, self.best_distance))
        if clock_back and dist_back:
            self._restart_streak += 1
        else:
            self._restart_streak = 0
        return self._restart_streak >= self.finish_confirm_frames

    def _over_track_length(self, reading: Reading) -> bool:
        """Is this further than the track goes? See distance_ceiling_ratio.

        Silent when the scenario declares no length, on the same principle as
        the rest of this file: a guard that cannot measure must not reject.
        """
        length = float(self.track.get("track_length_m", 0) or 0)
        if length <= 0 or reading.distance is None:
            return False
        return reading.distance > length * self.distance_ceiling_ratio

    def _should_resync(self) -> bool:
        """Has the guard been wrong often enough in a row to distrust itself?"""
        return self._reject_streak >= self.resync_after

    def _implausible_jump(self, reading: Reading) -> bool:
        """Could the car have covered this much since the last believed read?

        The race clock is the measure, not wall time: the game is frozen
        between acts, so wall time says nothing about how far the car drove.

        Deliberately ACCEPTS whenever the question cannot be answered — no
        track data, no clock on either side, or a clock that went backwards
        (a respawn or a new lap). A guard that rejects what it cannot measure
        would quietly delete real progress, which is a worse failure than the
        one it is here to stop: this reading is only ever discarded, never
        replaced with a guess.
        """
        if self.max_speed_mps <= 0:
            return False                       # no track data: no opinion
        if not self._anchored:
            # NO HISTORY, NO OPINION. _accepted_distance starts at 0 and
            # _accepted_clock at None, so the first real reading is compared
            # against "0 m, time unknown" and judged against the no-clock
            # window -- which rejects any episode whose first believed
            # odometer value is more than ~475 m. That is not exotic: the
            # opening frames of an episode routinely have no readable HUD
            # (20 of 57 racing acts in the A03 human recording had no
            # distance at all), so the first value that DOES read can be well
            # into the lap. Measured on a synthetic opening at 1200 m: three
            # consecutive rejections, recovery only via the re-anchor, and
            # the progress in between simply gone.
            #
            # This is the same confusion twice over -- "no anchor yet" was
            # being handled by the branch written for "this frame has no
            # clock". They are different states and only the second is a
            # judgement call.
            return False
        gain = reading.distance - self._accepted_distance
        if gain <= 0:
            return False                       # drops are handled by max()
        if reading.clock is None or self._accepted_clock is None:
            # No interval to measure. Bound by the longest a single act can
            # plausibly last instead, so a spike is still caught on a frame
            # whose clock did not read -- looser than the real check, and
            # deliberately so, since this branch cannot tell fast from wrong.
            window = float(self.spec.get("no_clock_seconds", 3.0))
        else:
            window = reading.clock - self._accepted_clock
            if window < 0:
                return False                   # clock restarted; not a jump
        return gain > self.max_speed_mps * window + self.jump_margin_m

    # --------------------------------------------------------------- score
    def _finish_score(self, t: float, gold: float, bronze: float,
                      floor: float) -> float:
        """What a completed lap of `t` seconds is worth.

        THE MEDAL SCALE ONLY REACHES SO FAR, and past its end this used to be
        a flat line. `max(floor, 1 + (bronze - t)/(bronze - gold))` clamps at
        the floor for every t beyond `bronze + (bronze - gold) * (1 - floor)`,
        which on A03 is 32.12 s. Every lap the benchmark has ever recorded on
        this track is slower than that: 32.75, 36.16, 36.55, 57.36, 57.36. So
        all five finishes scored EXACTLY 0.6 and the "best score" of a run was
        a constant, not a maximum — the reward could not tell a 33 s lap from
        a 57 s one, which is most of the dynamic range an agent actually has
        here. Fixing the scorer's arithmetic bugs did not fix this; it made it
        visible, because before the fix nothing reached the finish tier at all.

        Past bronze the score therefore DECAYS toward the floor instead of
        sitting on it, as the ratio of bronze pace to the pace achieved:

            floor + (1 - floor) * bronze / t

        which is 1.0 exactly at t = bronze (so it joins the medal scale with
        no step), falls monotonically, and approaches the floor without ever
        reaching it. That last property is what keeps the two-tier ordering
        intact: an unfinished run caps at 0.9 * floor, so ANY finish, however
        slow, still outranks EVERY incomplete run.

        On the A03 laps above this gives 0.949 / 0.917 / 0.913 / 0.800 /
        0.800 — an ordering by lap time, which is what the benchmark is
        supposed to measure.
        """
        if not bronze > gold > 0:      # misconfigured medals: finishing is 1.0
            return max(floor, 1.0)
        if t <= bronze:
            return 1.0 + (bronze - t) / (bronze - gold)
        return floor + (1.0 - floor) * (bronze / t)

    def score_for(self, reading: Reading) -> float:
        gold = float(self.track.get("gold_seconds", 0) or 0)
        bronze = float(self.track.get("bronze_seconds", 0) or 0)
        floor = float(self.spec.get("finish_floor", 0.6))
        length = float(self.track.get("track_length_m", 0) or 0)

        if reading.finished and reading.finish_time is not None:
            return self._finish_score(reading.finish_time, gold, bronze, floor)

        # WHAT THIS ACTUALLY MEASURES, stated plainly because the name lies.
        #
        # `frac` reads as "how far along the track you got". It is not. The
        # odometer counts PATH DRIVEN, confirmed by watching a replay on
        # 2026-09-01: a car dropped off the slope, drove around the open
        # ground behind the finish, and the reading kept climbing. So this
        # tier scores HOW MUCH THE CAR DROVE, and a car that wanders scores
        # like one that made progress.
        #
        # It is left in place rather than quietly re-derived, because there is
        # no position-along-track readout on this HUD to replace it with, and
        # a wrong number that is documented is safer than a different wrong
        # number that looks principled. The consequence is real and shows up
        # in results: the best lang episode of 2026-08-31 scored 0.5395, the
        # ceiling of this tier, having driven 1519 m of a 1521 m track WITHOUT
        # crossing the line. Read every unfinished score as "distance driven",
        # never as "distance completed", and do not compare it across tracks.
        if length > 0:
            frac = min(1.0, self.best_distance / length)
        else:
            frac = 0.0
        # Strictly below the finish floor: crawling to the line beats the best
        # possible unfinished run, which is the ordering the reward is for.
        return min(frac, 0.999) * floor * 0.9

    @property
    def total(self) -> float:
        return self._last_score

    # ------------------------------------------------- WindowsGameEnv contract
    # The env calls delta(), is_terminal() and classify_phase() on the SAME
    # frame in one act, and OcrScorer answers all three off one cached OCR
    # pass. Here it is not only a cost question: read() advances the finish
    # debounce and the distance high-water mark, so reading one frame three
    # times would confirm a finish in a third of the frames it is supposed to
    # take. Cache on frame IDENTITY (not a hash) — the env hands the same
    # array object to each call, and holding the reference also stops a
    # freed array's id from being reused by the next one.
    def _read_once(self, frame: np.ndarray) -> Reading:
        if self._cache_frame is frame and self._cache_frame is not None:
            return self.last_reading
        reading = self.read(frame)
        self._cache_frame = frame
        return reading

    def delta(self, frame: np.ndarray) -> float:
        """Account for progress since the previous settled scoring frame."""
        self._read_once(frame)              # read() keeps _last_score current
        gained = self._last_score - self._delta_baseline
        self._delta_baseline = self._last_score
        return gained

    def is_terminal(self, frame: np.ndarray) -> bool:
        """The lap is over. Structural, not keyword-based: a finish is a
        finish-time row WITH the race clock and the odometer gone."""
        return bool(self._read_once(frame).finished)

    def classify_phase(self, frame: np.ndarray) -> str | None:
        """Which screen this is, in the vocabulary the scenario's lifecycle
        block uses. None means "cannot tell" — a menu, a transition, a load —
        which is exactly what the non-progress watchdog is counting."""
        reading = self._read_once(frame)
        if reading.finished:
            return "finished"
        if reading.clock is not None:
            return "race"
        return None

    def read_region(self, frame: np.ndarray, name: str) -> int | None:
        """One region's current numeric value, with no scoring side effects —
        used by startup.start_check to assert the race actually began."""
        text = self._text_in(frame, name)
        return parse_metres(text) if name == "distance" else None

    def check_text(self, frame: np.ndarray, pattern: str, rect=None) -> bool:
        """Generic text-presence check for start_check.

        `rect` SCOPES the match, and this used to accept the argument and
        throw it away while `env.start_ok` dutifully passed
        `start_check.rect`. The scenario spends twenty lines explaining why
        that rect exists: the opponent-select dialog prints the track's medal
        times — gold 0:17.27 / silver 0:19.47 / bronze 0:24.28 — in exactly the
        format start_check matches on, so a screen-wide search declares the
        race started while the game is still waiting to be told which ghost
        to race. A smoke run duly reported PASS from that dialog.

        Unscoped it was worse than merely wide, because `_read_menu_regions`
        loads a full-screen crop into `_texts` precisely when no race clock is
        up — i.e. on the pre-race screens this check exists to reject. The
        result would be a cold start that stalls on the dialog, passes
        start_check, drives 1000 tics of menu, and is ledgered as a legitimate
        0.0, which is the exact failure `test_infra_failures_are_not_zeroes`
        exists to prevent. `OcrScorer.check_text` has always honoured `rect`;
        only this one did not.
        """
        rx = re.compile(pattern)
        if rect is not None:
            crop = self._crop_rect(frame, rect)
            if crop is None:
                return False
            text = " ".join(r["text"] for r in self.ocr(crop))
            return bool(rx.search(text))
        self._read_once(frame)
        return any(rx.search(t) for t in self._texts.values() if t)

    # ----------------------------------------------------------- diagnosis
    def track_identity(self, reading: Reading | None = None) -> str:
        """"ok" | "wrong" | "unreadable" — which is not a distinction to skip.

        The old shape of this returned a bool and answered False both when the
        track was right AND when the name could not be read at all, i.e. it
        reported "no problem" for "no evidence". That is the wrong default for
        the one check standing between the benchmark and scoring a stranger's
        lap against A02's medal times.

        Unreadable is the COMMON case here, not a corner: at the start line the
        name is drawn over the stadium, "A02" lands on a white floodlight tower
        and "-Race" on darker sky, so OCR returns "Race" or "-Race ladeo" and
        never the identifying part. Five candidate rects were measured; none
        read it. So a caller must decide what to do about silence, and cannot
        be allowed to mistake it for assent.
        """
        reading = reading or self.last_reading
        want = (self.track.get("name") or "").strip().lower().replace("-", "")
        got = (getattr(reading, "track", None) or "").strip().lower()
        got = got.replace("-", "")
        if not want:
            return "unreadable"          # nothing declared to compare against
        if not got:
            return "unreadable"
        # The readable tail alone is not identification: every track here ends
        # in "race", so "race" matching "a02race" would wave through A01, A03
        # and A05. Require the declared name to appear in what was read.
        return "ok" if want in got else "wrong"

    def wrong_track(self, reading: Reading | None = None) -> bool:
        """True only when the screen NAMED a different track. Silence is not
        agreement — see track_identity, which callers should prefer."""
        return self.track_identity(reading) == "wrong"

    def describe(self, reading: Reading | None = None) -> str:
        r = reading or self.last_reading
        if r.finished:
            return (f"finished {r.finish_time:.2f}s "
                    f"-> {self.score_for(r):.3f}")
        clock = f"{r.clock:.2f}s" if r.clock is not None else "?"
        return (f"racing t={clock} d={self.best_distance}m "
                f"-> {self.score_for(r):.3f}")
