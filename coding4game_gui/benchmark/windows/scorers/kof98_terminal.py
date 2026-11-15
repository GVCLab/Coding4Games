"""KOF98 first-fight termination and HUD scoring.

Detect a knockout from the calibrated health bars and combat transition.
A readable zero clock can end a timed-out fight. Settlement uses the last
readable combat frame. The CONTINUE? color signature labels the menu phase.
Historical human frames and current-host fixtures exercise both capture layouts.
"""
from __future__ import annotations

import time

import cv2
import numpy as np

from coding4game_gui.benchmark.windows.scorers import kof98_timer_font as font

P1_FRAC = (0.084, 0.347, 0.065, 0.092)
P2_FRAC = (0.445, 0.709, 0.065, 0.092)
TIMER_FRAC = (0.373, 0.426, 0.045, 0.099)

BLUE_MIN = 0.20
WHITE_BOT_MIN = 0.25
BOTTOM_FRAC = 0.78          # bottom strip holding the "CONTINUE?" plate
CONSECUTIVE_HITS = 2        # single-frame flicker must not end an episode

# Combat gate, added 2026-09-19. See in_combat for the measurement.
ABOVE_BRIGHT_MIN = 116.0
ABOVE_STRIP_FRAC = 0.035

# First-KO scoring, added 2026-09-19. See snapshot for the rule.
KO_EPSILON = 0.0145         # 4 columns of a 276-column bar; see is_terminal
LIVE_MIN = 0.50             # both bars this healthy means the fight has begun
FULL_BAR = 0.9964           # what a full bar actually reads through this ROI

# Drain confirmation, calibrated 2026-09-21. See _drained_out_of_combat.
# A finishing blow can land while the loser still shows real health, and the
# bar only empties AFTER the game leaves combat for the win banner -- so the
# arming step above never happens and the episode runs on into the next fight.
DRAIN_SECONDS = 1.2         # measured: false positives end by 0.96s, real
                            # knockouts last at least 1.50s
DRAIN_MIN_FRAMES = 2        # seconds alone would let one frame plus a stalled
                            # capture loop stand in for a sustained reading
DRAIN_LIVE_MAX = 60 / 276   # the loser must already have been losing: false
                            # positives all followed bars of >=105 columns,
                            # real knockouts followed bars of <=31

# Round clock, calibrated 2026-09-20. See read_timer for the derivation.
# Fractions are chosen so that int(frac * 854) and int(frac * 480) land on the
# exact pixel columns the calibration cut its glyphs from; tests/game/
# test_kof98_timer.py pins that, because a one-column drift silently degrades
# every match instead of failing.
TIMER_BAND_FRAC = (0.0, 0.0688)      # -> rows 0..32 at 480
TIMER_TENS_FRAC = (0.4708, 0.4848)   # -> cols 402..413 at 854
TIMER_ONES_FRAC = (0.4942, 0.5071)   # -> cols 422..432 at 854
TIMER_REJECT = 0.15         # mean abs difference above this is "not a digit"
TIMER_MIN_INK = 6.0         # a resized cell this empty is a blank, not a glyph
ROUND_SECONDS = 60          # measured, not assumed -- see read_timer


class Kof98TerminalScorer:
    """Read the KOF98 HUD and settle the first fight."""

    def __init__(self, spec: dict, client_size, ocr=None, clock=None):
        self.spec = spec or {}
        cal = self.spec.get("calibrated_client_size")
        if cal and tuple(cal) != tuple(client_size):
            print(f"[kof98_scorer] WARN client size {tuple(client_size)} != "
                  f"calibrated {tuple(cal)}; fractional coords should still hold")
        health = self.spec.get("health") or {}
        self.p1 = tuple(health.get("p1_frac", P1_FRAC))
        self.p2 = tuple(health.get("p2_frac", P2_FRAC))
        self.timer = tuple(health.get("timer_frac", TIMER_FRAC))
        self.timer_min = int(health.get("timer_min_yellow", 30))
        self.reset_jump = float(health.get("round_reset_jump", 0.20))
        self.scale = float(health.get("scale", 1.0))
        self.health_fill_color = health.get("fill_color")
        if self.health_fill_color not in (None, "green"):
            raise ValueError("KOF98 health.fill_color must be green when specified")
        # Injectable so the drain window can be tested without sleeping. It
        # measures REAL time on purpose: the threshold was calibrated in
        # seconds because a frame count means different things at different
        # capture rates, and mixing those up is what produced this defect.
        self._clock = clock or time.monotonic
        cfg = self.spec.get("terminal") or {}
        self.blue_min = float(cfg.get("blue_min", BLUE_MIN))
        self.white_bot_min = float(cfg.get("white_bot_min", WHITE_BOT_MIN))
        self.bottom_frac = float(cfg.get("bottom_frac", BOTTOM_FRAC))
        self.need_hits = int(cfg.get("consecutive_hits", CONSECUTIVE_HITS))
        combat = self.spec.get("combat") or {}
        self.above_bright_min = float(
            combat.get("above_bright_min", ABOVE_BRIGHT_MIN))
        self.above_strip_frac = float(
            combat.get("above_strip_frac", ABOVE_STRIP_FRAC))
        ko = self.spec.get("ko") or {}
        self.ko_epsilon = float(ko.get("epsilon", KO_EPSILON))
        self.live_min = float(ko.get("live_min", LIVE_MIN))
        self.full_bar = float(ko.get("full_bar", FULL_BAR))
        self.drain_seconds = float(ko.get("drain_seconds", DRAIN_SECONDS))
        self.drain_min_frames = int(ko.get("drain_min_frames", DRAIN_MIN_FRAMES))
        self.drain_live_max = float(ko.get("drain_live_max", DRAIN_LIVE_MAX))
        clock = self.spec.get("clock") or {}
        self.timer_band = tuple(clock.get("band_frac", TIMER_BAND_FRAC))
        self.timer_tens = tuple(clock.get("tens_frac", TIMER_TENS_FRAC))
        self.timer_ones = tuple(clock.get("ones_frac", TIMER_ONES_FRAC))
        self.timer_reject = float(clock.get("reject", TIMER_REJECT))
        self.timer_min_ink = float(clock.get("min_ink", TIMER_MIN_INK))
        self.round_seconds = int(clock.get("round_seconds", ROUND_SECONDS))
        self._digits = np.stack([np.asarray(font.bitmap(d), dtype=np.float32)
                                 for d in range(10)])
        self.reset()

    def reset(self) -> None:
        self._prev = None
        self._score = 0.0
        self._zero_clock_hits = 0
        self._pending_ko = False
        self._seen_live = False
        self._last_live_low = None
        self._drain_start = None
        self._drain_frames = 0

    # ---- combat gate ----
    def _above_bar_brightness(self, frame: np.ndarray) -> float:
        """Mean brightness of the strip directly above the two health bars.

        The strip reads differently on character-select portraits and the HUD.
        Portrait highlights can satisfy the yellow timer check by themselves.
        """
        h, w = frame.shape[:2]
        y1 = int(self.p1[2] * h)
        y0 = max(0, y1 - int(self.above_strip_frac * h))
        x0 = int(min(self.p1[0], self.p2[0]) * w)
        x1 = int(max(self.p1[1], self.p2[1]) * w)
        strip = frame[y0:y1, x0:x1]
        return float(strip.mean()) if strip.size else 0.0

    def in_combat(self, frame: np.ndarray) -> bool:
        """Require a yellow timer and a sufficiently bright strip above the bars.

        MEASURED 2026-09-19 on mr-why, AT BOTH FRAME SIZES, because the two
        arms do not share one: the GUI arm renders 854x480 and the code arm
        1280x768, and those have different aspect ratios, so the strip lands on
        different content and a band measured at one size cannot be assumed to
        hold at the other. Derived from `continue_capture` (851 frames of one
        match driven by a do-nothing policy, 854x480) and then checked against
        sets it was not derived from:

            854x480, three GUI episodes   accepted 122.2 .. 137.5
            (different characters/stages) rejected  96.1 .. 107.6
            1280x768, two frozen tests    accepted 119.8 .. 132.3
            (n=705 stock-combat frames)   rejected  35.8 .. 112.4

        The binding pair is 112.4 and 119.8, so 116 is the midpoint with about
        3% either side. That is not a generous margin, and it is the reason the
        fixtures below pin both sizes: if a stage or a character ever reads
        outside the band, a test says so rather than a run quietly mis-scoring.
        The single frames behind the bug read character-select 94.1,
        stage-select 93.6, ORDER COMPLETE 42.0.

        NO UPPER BOUND on purpose. The team-select menu is brighter than any
        combat frame (159.4), but it is already rejected by `_timer_yellow`,
        and a one-sided test cannot be tripped by an unusually bright stage.

        What this fixes is the SCORE, not just the phase label: `delta` only
        accumulates while `in_combat`, so before this the character-select
        mosaic was being read as health bars. Of the frames the stock test
        called combat in GUI episodes 0 and 1, 54% were that menu.
        """
        # A readable HUD remains valid when BOTH fighters have less than 30%
        # health. The stock health floor discarded real low-health wins.
        if self._timer_yellow(frame) <= self.timer_min:
            return False
        return self._above_bar_brightness(frame) >= self.above_bright_min

    def _bar(self, frame, frac) -> float:
        seg = self._box(frame, frac)
        if self.health_fill_color == "green":
            # Exclude the empty grid, whose tint varies with the stage.
            b, g, r = seg[:, :, 0], seg[:, :, 1], seg[:, :, 2]
            filled = (g > 90) & (g - r > 45) & (g - b > 45)
        else:
            brightest = seg.max(2)
            filled = (brightest - seg.min(2) > 45) & (brightest > 90)
        return float((filled.mean(0) > 0.4).mean())

    @staticmethod
    def _box(frame, frac):
        h, w = frame.shape[:2]
        x0, x1, y0, y1 = frac
        return frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)].astype(np.int16)

    def _timer_yellow(self, frame) -> int:
        seg = self._box(frame, self.timer)
        b, g, r = seg[:, :, 0], seg[:, :, 1], seg[:, :, 2]
        return int(((r > 170) & (g > 120) & (b < 110)).sum())

    def read(self, frame):
        return self._bar(frame, self.p1), self._bar(frame, self.p2)

    def delta(self, frame: np.ndarray) -> float:
        """Accumulate health losses between combat frames, skipping refills."""
        if not self.in_combat(frame):
            self._prev = None
            return 0.0
        own, opponent = self.read(frame)
        score = 0.0
        if self._prev is not None:
            prev_own, prev_opponent = self._prev
            if (own - prev_own < self.reset_jump
                    and opponent - prev_opponent < self.reset_jump):
                opponent_lost = max(0.0, prev_opponent - opponent)
                own_lost = max(0.0, prev_own - own)
                score = (opponent_lost - own_lost) * self.scale
                self._score += score
        self._prev = own, opponent
        return score

    @property
    def total(self) -> float:
        return self._score

    # ---- the round clock ----
    def _digit_cell(self, frame: np.ndarray, span) -> np.ndarray:
        h, w = frame.shape[:2]
        y0, y1 = (int(f * h) for f in self.timer_band)
        x0, x1 = (int(f * w) for f in span)
        cell = frame[y0:y1, x0:x1].astype(np.int16)
        b, g, r = cell[:, :, 0], cell[:, :, 1], cell[:, :, 2]
        mask = ((r > 170) & (g > 120) & (b < 110)).astype(np.float32)
        if not mask.size:
            return mask
        return cv2.resize(mask, (font.WIDTH, font.HEIGHT),
                          interpolation=cv2.INTER_AREA)

    def _read_digit(self, frame: np.ndarray, span):
        cell = self._digit_cell(frame, span)
        if cell.size == 0 or float(cell.sum()) < self.timer_min_ink:
            return None
        distances = np.abs(self._digits - cell[None]).mean(axis=(1, 2))
        best = int(distances.argmin())
        return None if distances[best] > self.timer_reject else best

    def read_timer(self, frame: np.ndarray):
        """Seconds left on the round clock, or None when it is not legible.

        The clock is the only thing this scenario can use as a duration: the
        scorer is handed a frame and nothing else, so a tic count would have to
        be plumbed through main's settlement. The number is on screen, it is
        the game's own authority on how far the round has run, and it resets by
        itself at a round boundary.

        CALIBRATED 2026-09-20 from kof_human_rec2 (1168 frames, 854x480, the
        production capture path). The two digit cells were located by temporal
        variance, not by colour: the countdown is the only thing in the top
        centre that changes, so a reddish stage behind it is constant and drops
        out. The glyphs were then labelled by the countdown itself -- the tens
        digit only moves when the ones digit wraps 0 -> 9 -- so nothing here was
        annotated by hand. See game/scorers/kof98_timer_font.py.

        ACCEPTANCE on that recording, stated before it was run: 853 frames
        decoded, 110 steps of exactly -1, 736 repeats, ZERO increases inside a
        stretch where the clock stayed on screen, and a maximum of exactly 60.
        That maximum is how we learned the round is 60 seconds rather than the
        99 we had assumed; every reset in the recording came back to 60.

        Reject threshold 0.15: the sweep 0.12 .. 0.30 returned identical
        results on the whole recording, and 0.10 began throwing away real
        digits (570 frames decoded instead of 853, maximum falling to 49).

        THE ONE ANOMALY, and it matters for the caller: frames 538..543 read
        22, 22, 22, 17, 8, 0, 0, 0 and then blank. That is the round ending --
        the clock spins down as part of the end-of-round animation. So a
        reading of 0 does NOT by itself mean the round timed out; `is_terminal`
        also requires both bars to be alive, which a KO frame never satisfies.
        """
        tens = self._read_digit(frame, self.timer_tens)
        ones = self._read_digit(frame, self.timer_ones)
        if tens is None or ones is None:
            return None
        return tens * 10 + ones

    def elapsed_fraction(self, frame: np.ndarray):
        """How much of the round has run, in [0, 1], or None if unreadable."""
        remaining = self.read_timer(frame)
        if remaining is None:
            return None
        used = self.round_seconds - min(remaining, self.round_seconds)
        return min(1.0, max(0.0, used / float(self.round_seconds)))

    def read_region(self, frame: np.ndarray, name: str):
        """Expose measured HUD scalars to the shared frozen startup check.

        A menu or unreadable combat HUD supplies no value. Reading readiness
        does not arm the terminal detector or settle a gameplay score.
        """
        names = {"own_health_percent", "opponent_health_percent", "round_clock"}
        if name not in names:
            raise ValueError(f"unknown KOF98 HUD region: {name!r}")
        if not self.in_combat(frame):
            return None
        if name == "round_clock":
            return self.read_timer(frame)
        own, opponent = self.read(frame)
        value = own if name == "own_health_percent" else opponent
        return round(100 * min(1.0, max(0.0, value / self.full_bar)))

    # ---- first KO: the episode boundary and the score ----
    def begin_episode(self, frame: np.ndarray) -> None:
        """Prime the healthy-round latch from the existing frozen initial HUD.

        No terminal decision or score is emitted. Without this observation,
        a first ACT ending on a KO could never establish the healthy latch.
        """
        self._ko_reading(frame)

    def _ko_reading(self, frame: np.ndarray):
        """(own, opponent, is_ko) on a live combat frame, else None.

        `_seen_live` is a one-way latch rather than a timer: an episode is only
        allowed to end on a KO after it has been observed with both bars
        healthy, so a misread during the hand-over cannot terminate it on tic
        one. It costs nothing when the match is normal.
        """
        if not self.in_combat(frame):
            return None
        p1, p2 = self.read(frame)
        if not self._seen_live:
            if min(p1, p2) > self.live_min:
                self._seen_live = True
            return None
        return p1, p2, min(p1, p2) <= self.ko_epsilon

    def is_terminal(self, frame: np.ndarray) -> bool:
        """End the episode when the first fight is decided.

        THE PREVIOUS RULE WAS `min(bar) <= 0.0` AND IT MISSED REAL KOs. A bar
        reading is the fraction of coloured COLUMNS in the bar box, and the p1
        box is 276 columns wide, so the reading is quantised to 1/276 =
        0.00362 and "zero" means not one surviving column. Measured 2026-09-20
        on kof_human_rec2, the production capture of a full human match, over
        every in-combat frame:

            columns  0   1   2   3   4   5   6   9  13
            frames  16   7   1   3   2   9   8   1   2

        There is no gap. One of that recording's four KOs never reads zero at
        all -- frames 998..1003 run 2, 3, 3, 3, 4, 4 columns and then combat
        ends -- so no threshold at zero can catch it, and a run of zeros can be
        followed by a stray column (frame 181 reads 1 column after seven frames
        of none). The same 1-column reading, 0.00362, is what let a KO through
        in the 2026-09-20 batch.

        WHAT DOES SEPARATE CLEANLY is the pair. In all four KOs the bar falls
        to a few columns and combat then ENDS; in ordinary play it never gets
        that low without the fight finishing. So a near-empty bar arms, and the
        end of combat confirms:

            bar == 0                  -> terminal at once, it is unambiguous
            bar <= ko_epsilon         -> arm; death is certain from here
            armed, then out of combat -> terminal

        Settlement walks backward and skips unreadable frames, so confirming on
        the first non-combat frame still settles on the last in-combat one --
        the near-empty bar itself.

        A ROUND ALSO ENDS ON THE CLOCK: KOF decides a timed-out round by
        comparing the bars, so neither reaches zero. Both bars alive with the
        clock at zero is the condition DIAMBRA's engine uses (`min(health) == 0
        or timer <= 0`). "Both alive" is load bearing -- the clock also spins
        to zero during the end-of-round animation AFTER a KO.

        NOTE ON RATE, because this test can only be as good as its sampling.
        main offers one frame per `act()` call, so a policy issuing one
        620-tic action is asked once per episode. The historical source branch installed a
        per-tic tap that closes that gap without editing main; without it, this
        rule still misses whatever falls between two actions.
        """
        reading = self._ko_reading(frame)
        if reading is None:
            # Out of combat. A bar that was near-empty a moment ago and a fight
            # that has now stopped is the KO we armed for.
            if self._pending_ko:
                self._pending_ko = False
                self._zero_clock_hits = 0
                return True
            return self._drained_out_of_combat(frame)
        p1, p2, _ = reading
        lowest = min(p1, p2)
        self._last_live_low = lowest
        self._drain_start = None
        self._drain_frames = 0
        if lowest <= 0.0:
            self._pending_ko = False
            self._zero_clock_hits = 0
            return True
        if lowest <= self.ko_epsilon:
            self._pending_ko = True
        if self.read_timer(frame) == 0:
            self._zero_clock_hits += 1
            return self._zero_clock_hits >= self.need_hits
        self._zero_clock_hits = 0
        return False

    def _drained_out_of_combat(self, frame: np.ndarray) -> bool:
        """The knockout that finishes AFTER the game leaves combat.

        Found 2026-09-21 in two batch-5 episodes that each scored a fight they
        did not fight. In one, the last in-combat frame read 19 columns for the
        losing side -- five times the arming threshold -- the finishing blow
        landed, the win banner replaced the fight, and the bar only reached
        zero on frames the combat gate throws away. Arming never happened, so
        the confirm step above could never fire, and the episode ran on into
        the next match and was scored there.

        Simply ending on "combat stopped" does not work. Measured over both
        human recordings, stretches of non-combat DURING a fight (super moves,
        hit freezes, flashes) run up to 23 frames while genuine fight-ends
        start at 21 -- no threshold separates them.

        What does separate is the bar itself, on those same discarded frames,
        under two conditions measured over both recordings:

            how long it reads empty   false positives end by 0.96s;
                                      real knockouts last 1.50s and longer
            who it was reading        false positives all followed a fighter
                                      with >=105 columns left; knockouts
                                      followed <=31

        Hence a window in SECONDS (not frames -- the recordings sample at 4-5
        Hz and the capture loop at 10) plus the requirement that the loser was
        already losing when the fight stopped. Both must hold; each alone has
        a hard negative in the data.
        """
        if not self._seen_live or self._last_live_low is None:
            return False
        if self._last_live_low > self.drain_live_max:
            self._drain_start = None
            self._drain_frames = 0
            return False
        try:
            p1, p2 = self.read(frame)
        except Exception:
            self._drain_start = None
            self._drain_frames = 0
            return False
        if min(p1, p2) > self.ko_epsilon:
            # The transition frames read garbage between the real ones (one
            # measured 40 columns mid-wipe), so the run has to restart rather
            # than accumulate across them.
            self._drain_start = None
            self._drain_frames = 0
            return False
        now = self._clock()
        if self._drain_start is None:
            self._drain_start = now
            self._drain_frames = 1
            return False
        self._drain_frames += 1
        if (now - self._drain_start >= self.drain_seconds
                and self._drain_frames >= self.drain_min_frames):
            self._drain_start = None
            self._drain_frames = 0
            self._pending_ko = False
            self._zero_clock_hits = 0
            return True
        return False

    def snapshot(self, frame: np.ndarray):
        """Terminal-state score for `settlement: latest_valid_frame`.

        `DeferredScore._settle_latest` walks BACKWARD from the last saved frame
        and takes the first one this returns a reading for, so the transition
        frames that may follow a KO are skipped and the KO frame itself is what
        gets scored -- and saved as the evidence PNG.

        THE RULE, in [0, 1], not accumulated. A round is DECIDED when a bar
        reaches zero or the clock reaches zero; otherwise it is UNDECIDED:

            decided     score = (1 + own - opponent) / 2
            undecided   score = (1 - opponent) * elapsed / 2

        The decided branch is one expression for all three endings, because
        deciding a round on the clock means comparing the bars, which is what
        that expression already does: a KO win has opponent 0 and scores
        0.5 + own/2, a KO loss has own 0 and scores (1 - opponent)/2, and a
        timed-out round lands between them by however the bars compare. Losing
        is therefore always below 0.5 and winning always above it. Both bars
        are divided by `full_bar` because a genuinely full bar reads 0.9964
        through this ROI, not 1.0; without that a perfect fight would score
        0.998 and never look like full marks.

        WHY THE UNDECIDED BRANCH DROPS `own`, measured rather than supposed.
        Under the single decided expression an unfought round reads 0.5: both
        bars are full, the difference is zero, and 0.5 beat almost every real
        outcome because the policy usually loses. A model found that. In the
        2026-09-19 claude-opus-5 run, episodes cut off before a KO averaged
        0.31 while episodes fought to a KO averaged 0.054, and the program that
        run submitted was a 1.5 KB crouch-block loop whose own docstring reads
        "The host settles score from the final valid frame as (1 + own_health -
        opponent_health) / 2, so preserving own health is worth as much as
        dealing damage." It scored 0.436 and 0.316 on two 900-tic rollouts it
        had capped itself, with the opponent's bar untouched at 1.0 both times,
        and 0.000 on all three frozen tests, which do not let a policy stop the
        clock. Crediting survival only on a decided round removes that: keeping
        your bar full while never attacking now scores 0, and `elapsed` makes a
        rollout cut short strictly worse than the same rollout played out.

        Speed is deliberately NOT folded in here. `episodes.jsonl` already
        records each episode's tic count, so if scores saturate at full marks
        the tie is broken from that without giving this number two meanings.
        """
        reading = self._ko_reading(frame)
        if reading is None:
            return None
        p1, p2, ko = reading
        own = min(1.0, max(0.0, p1 / self.full_bar))
        opp = min(1.0, max(0.0, p2 / self.full_bar))
        remaining = self.read_timer(frame)
        timed_out = (not ko) and remaining == 0
        if ko or timed_out:
            score = (1.0 + own - opp) / 2.0
            ending = "ko" if ko else "timeout"
            elapsed = None
        else:
            if remaining is None:
                # An undecided frame cannot be priced without the clock. Return
                # nothing so the backward walk keeps looking for a frame that
                # can be, rather than inventing a duration for this one.
                return None
            elapsed = min(1.0, max(0.0, (self.round_seconds
                                         - min(remaining, self.round_seconds))
                                   / float(self.round_seconds)))
            score = (1.0 - opp) * elapsed / 2.0
            ending = None
        return {"score": min(1.0, max(0.0, score)),
                "own_health": own, "opponent_health": opp,
                "raw_p1": p1, "raw_p2": p2, "ko": bool(ko),
                "ending": ending, "clock_remaining": remaining,
                "elapsed": elapsed}

    # ---- CONTINUE? screen ----
    def continue_signature(self, frame: np.ndarray):
        """(blue_ratio, white_bottom_ratio) — the two numbers the gate reads."""
        h = frame.shape[0]
        b = frame[:, :, 0].astype(np.int16)
        r = frame[:, :, 2].astype(np.int16)
        blue = float((((b - r) > 35) & (b > 90)).mean())
        bot = frame[int(self.bottom_frac * h):, :, :].astype(np.int16)
        white = float(((bot[:, :, 0] > 175) & (bot[:, :, 1] > 175)
                       & (bot[:, :, 2] > 175)).mean())
        return blue, white

    def is_continue_screen(self, frame: np.ndarray) -> bool:
        blue, white = self.continue_signature(frame)
        return blue > self.blue_min and white > self.white_bot_min

    def classify_phase(self, frame: np.ndarray) -> str:
        if self.is_continue_screen(frame):
            return "match_end"
        return "combat" if self.in_combat(frame) else "non_combat"
