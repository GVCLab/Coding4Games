"""BattleBlock's score: a function of the milestone SET, not of the event stream.

WHY GEMS ARE THE PROGRESS MEASURE AND NOT A PROXY
-------------------------------------------------
The exit of a BattleBlock level only opens once enough gems have been collected.
So `gems / required` is not a proxy anyone invented for this benchmark -- it is
the game's own definition of how far through the level you are, read off the
game's own HUD. Yarn is a bonus that does not gate anything; the odometry that
used to be the score measured camera scroll, which is a proxy, and a farmable
one (it paid for jittering right and left until it was rewritten around a
signed high-water mark, and it is a diagnostic now, not a score).

WHY TIERS AND NOT A WEIGHTED SUM
--------------------------------
A weighted sum lets a cheap term buy the expensive one: enough yarn outscores a
gem, enough of any proxy outscores real progress. Tiers cannot be bought across
-- every T2 outranks every T1 no matter what happened inside T1 -- so the only
way up is the thing the tier is named after.

    T0  never entered the level     NOT SCORED (infrastructure failure)
    T1  in the level, short of the  [0, 1)   0.95 * gems_max / quota
        gem quota
    T2  quota met, exit open,       [1, 2)   flat 1.0 for now, see below
        not finished
    T3  finished                    [2, 3]   2 + 0.8*speed + 0.2*yarn

T2 IS FLAT AND THAT IS DELIBERATE. The honest gradient between "the exit opened"
and "you reached it" would be distance-to-exit, and this game has no measurable
one: in-level place recognition was disproven here (pHash, thumbnails, HSV, edge
grid and ORB all collapse to AUC 0.79-0.86 against hard negatives), and the
odometry crop is still calibrated against fixtures from the wrong reference
frame. Inventing a gradient we cannot measure would be worse than admitting a
flat tier.

WHY THE SET AND NOT THE SEQUENCE
--------------------------------
Every milestone pays AT MOST ONCE, so nothing can be farmed by repetition, and
`total` is monotone by construction. Dying turns out not to touch the score at
all -- measured, see `_maybe_restart` -- but the running max would have absorbed
it either way.

T0 EXISTS BECAUSE A FAILED COLD START MUST NOT LOOK LIKE A BAD PLAYER. An
episode that never reached the level should be excluded from the ledger, not
averaged in as a zero -- a zero says "played and scored nothing", which is a
claim about the policy. `entered_level` is what a runner should check.

TIME IS TICS, NOT WALL CLOCK. Wall clock would fine the lang arm for thinking:
the game is frozen while the policy deliberates, so seconds spent reasoning are
not seconds of play. The reference is a human's tic count on the same level.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from coding4game_gui.benchmark.windows.scorers import bbt_hud

# 1-1: three gems open the exit, one yarn exists. Both are per-level facts and
# both belong in the scenario YAML; these are the fallbacks and the only level
# calibrated so far.
DEFAULT_QUOTA = 3
DEFAULT_YARN_MAX = 1
# A human's tic count on 1-1, MEASURED IN THE UNITS THIS SCORER COUNTS.
#
# The previous value (481) came from a recording in the wrong reference frame
# whose tic units were never established, which made the speed term -- 0.8 of
# the whole finish tier -- a ratio of two different things.
#
# This one is measured from a playthrough captured through the bench renderer.
# The human sat idle for the first 29.8s (reading a handover message; frame
# delta 0.47 against 8-23 once play starts), reached the exit at t=81.6s, and
# the results screen appeared at t=86.9s. The span that counts is 51.8s:
# `completion_tics` accumulates only READABLE IN-LEVEL frames, so the 5.3s of
# curtain between exit and results is already excluded -- verified by replaying
# the recording (completion_tics 273 against 271 readable frames before the
# curtain at 3.32 fps).
#
# The 51.8s DOES include the four deaths and their respawn curtains, so when
# the env supplies game tics, tics that pass behind an IN-LEVEL curtain are
# carried into the next readable frame rather than dropped (`_carry_tics`).
# The exit curtain stays excluded: the results frame stamps completion before
# anything carried across it is collected.
#
# A human plays unfrozen, so tics = unfrozen seconds x tics_per_second (10):
#     51.8s -> 518 tics
#
# This is ONE run, by someone who knows the level and still died four times --
# a competent run, not a speedrun. Matching it scores full marks on speed,
# which is the intended bar.
DEFAULT_REFERENCE_TICS = 518

T1_SPAN = 0.95          # keeps a full T1 strictly below the T2 floor
T3_SPEED_WEIGHT = 0.8
T3_YARN_WEIGHT = 0.2

# Median window over the raw per-frame HUD reads. One misread frame must not be
# able to decide an episode; five frames at 10 Hz is half a second.
DEBOUNCE_WINDOW = 5
# How long after a count collapse we still accept "a curtain caused it".
RESTART_LOOKBACK = bbt_hud.RESTART_CURTAIN_LOOKBACK


class BbtProgressScorer:
    """Milestone scorer over the collectible HUD.

    Implements the duck-typed scorer protocol the Windows env expects:
    `reset`, `delta(frame) -> float`, `is_terminal(frame) -> bool`,
    `classify_phase(frame) -> str`, and a `total` property. The env SUMS the
    deltas, so a milestone score is expressed by paying the increment the first
    time a milestone is reached and zero every other frame.
    """

    def __init__(self, spec: Optional[dict] = None, client_size=None, ocr=None):
        self.spec = spec or {}
        cfg = self.spec.get("progress") or {}
        self.quota = max(1, int(cfg.get("gem_quota", DEFAULT_QUOTA)))
        self.yarn_max = max(0, int(cfg.get("yarn_max", DEFAULT_YARN_MAX)))
        self.reference_tics = max(
            1, int(cfg.get("reference_tics", DEFAULT_REFERENCE_TICS)))
        # A mis-sized frame raises inside bbt_hud rather than silently reading
        # the wrong pixels; the calibrated size lives there, so nothing to
        # check here beyond passing frames straight through.
        self.client_size = tuple(client_size) if client_size else None
        self.reset()

    # ------------------------------------------------------------------ state
    def reset(self) -> None:
        self.entered_level = False      # T0 gate: did we ever see the HUD?
        self.gems_max = 0
        self.yarn_max_seen = 0
        self.completed = False
        self.restarted = False
        self.tics = 0                   # frames scored inside the level
        self.completion_tics: Optional[int] = None
        self.quota_met_tics: Optional[int] = None
        self.first_gem_tics: dict[int, int] = {}
        self._raw: list[Optional[int]] = []
        self._curtains: list[bool] = []
        self._paid = 0.0
        self._next_tics = 1          # see note_tics
        self._tics_from_env = False  # False = tics are really DECISION counts
        self._carry_tics = 0         # env tics spent behind an in-level curtain
        # Diagnostics -- recorded, never scored.
        self.unreadable_frames = 0
        self.exit_open_at_gems: Optional[int] = None
        self.curtain_tics = 0

    # ------------------------------------------------------------ the score
    def _tier_score(self) -> float:
        if not self.entered_level:
            return 0.0
        if self.completed:
            speed = 0.0
            if self.completion_tics:
                speed = min(1.0, self.reference_tics / self.completion_tics)
            yarn = (self.yarn_max_seen / self.yarn_max) if self.yarn_max else 0.0
            return 2.0 + T3_SPEED_WEIGHT * speed + T3_YARN_WEIGHT * min(1.0, yarn)
        if self.gems_max >= self.quota:
            return 1.0
        return T1_SPAN * (self.gems_max / self.quota)

    @property
    def total(self) -> float:
        return self._paid

    # ------------------------------------------------------------- protocol
    def note_tics(self, n: int) -> None:
        """Tell the scorer how many GAME TICS the next scored frame covers.

        `delta` is called once per DECISION, and a decision spends however many
        tics it asked for -- so counting delta calls counts decisions, not
        time, and comparing that to a tic-denominated human reference compares
        two different units. Measured on a real episode: 120 decisions of 2-3
        tics each were being recorded as "120 tics".

        DeferredScore replays each recorded game-tic span before delta.
        Direct calls without a span retain the one-tic fallback and expose
        that fallback in the scorer details.

        Zero is a legal span (two frames stamped at the same tic) and counts as
        zero; only the per-call fallback assumes one tic per frame.
        """
        self._next_tics = max(0, int(n))
        self._tics_from_env = True

    def delta(self, frame: np.ndarray) -> float:
        # ONCE FINISHED, STOP LOOKING. Measured on a real playthrough: after
        # the results screen the game returns to the hub, the collectible HUD
        # reads 0 again, and a hub curtain sits right next to it -- which this
        # scorer dutifully classified as a RESTART, ~18 times, on an episode
        # that had already been won. Harmless there only because completion had
        # already been paid; mid-level the same pattern would end a good run.
        if self.completed:
            return 0.0

        if bbt_hud.is_results(frame):
            # The only completion signal there is. Stamp the tic count once:
            # re-entering this branch on later frames must not re-price it.
            if not self.completed:
                self.completed = True
                self.completion_tics = max(1, self.tics)
            return self._pay()

        curtain = bbt_hud.is_curtain(frame)
        self._curtains.append(curtain)
        reading = None if curtain else bbt_hud.read(frame)
        if reading is None:
            self.unreadable_frames += 1
            self._raw.append(None)
            if self.entered_level and self._tics_from_env:
                # Game time kept running behind a death/respawn curtain; the
                # human reference includes those, so they are not dropped.
                # Per-call counts have no time in them to carry.
                self._carry_tics += self._next_tics
                self.curtain_tics += self._next_tics
                self._next_tics = 1
            return self._pay()

        gems, yarn = reading
        self.entered_level = True
        self.tics += self._carry_tics + self._next_tics
        self._carry_tics = 0
        self._next_tics = 1
        self._raw.append(gems)

        # Debounce over the RAW stream, then take the running max. The median
        # kills single-frame jitter; the max is what makes every milestone
        # pay once and never un-pay, including across a death that might
        # zero the on-screen count.
        smoothed = bbt_hud.debounce(self._raw[-DEBOUNCE_WINDOW:],
                                    window=DEBOUNCE_WINDOW)
        current = next((v for v in reversed(smoothed) if v is not None), None)
        if current is not None:
            if current > self.gems_max:
                for g in range(self.gems_max + 1, current + 1):
                    self.first_gem_tics.setdefault(g, self.tics)
                self.gems_max = current
                if self.gems_max >= self.quota and self.quota_met_tics is None:
                    self.quota_met_tics = self.tics
                    # The declared quota, checked against the game. See
                    # `quota_disagreement`.
                    self.exit_open_at_gems = self.gems_max
            elif current == 0 and self.gems_max > 0:
                self._maybe_restart()

        self.yarn_max_seen = max(self.yarn_max_seen, yarn)
        return self._pay()

    def _pay(self) -> float:
        """Pay whatever the tier score gained. Never negative: a milestone that
        has been reached is reached, and the ledger may not take it back."""
        want = self._tier_score()
        gain = max(0.0, want - self._paid)
        self._paid += gain
        return gain

    def _maybe_restart(self) -> None:
        """A collapse to zero next to a curtain is an in-level RESTART.

        Restart ends the episode by ruling. Far from any curtain the same
        collapse is our own reader failing, and killing a good episode over a
        misread would be the expensive error, so the curtain has to be there.

        DEATH DOES NEITHER, AND THAT IS MEASURED. A human played 1-1 dying four
        times; the deaths were located by frame-delta spikes using a threshold
        built from in-level frames only (the two transitions inflate a plain
        standard deviation enough to hide everything else), and at every one of
        them the gem count was unchanged and no curtain appeared:

            t=33.07  gems [0,0,0,0] -> [0,1,1,0,1]   curtain 0
            t=48.61  gems [1,1,1,0] -> [1,1,1,1,1]   curtain 0
            t=49.20  gems [1,0,0,1] -> [1,1,1,1,1]   curtain 0
            t=74.10  gems [2,2,2,2] -> [2,2,2,2,2]   curtain 0

        So a death cannot reach this branch on this level. Another level with a
        drowning animation that draws the curtain would need re-checking.
        """
        idx = len(self._curtains) - 1
        window = self._curtains[max(0, idx - RESTART_LOOKBACK):idx + 1]
        if any(window):
            self.restarted = True
            print("[bbt-score] collectible count collapsed to 0 next to a "
                  "curtain -> RESTART, ending the episode. If the game was "
                  "showing a DEATH here, this is the known false positive: "
                  "death-vs-restart has not been measured.", flush=True)
        else:
            print("[bbt-score] count collapsed to 0 with no curtain nearby -> "
                  "treating it as a READER FAULT, not a restart; the episode "
                  "continues and the milestone set is unchanged.", flush=True)

    def is_terminal(self, frame: np.ndarray) -> bool:
        """Read the results screen before settlement updates transition flags."""
        return bool(self.completed or self.restarted
                    or bbt_hud.is_results(frame))

    def classify_phase(self, frame: np.ndarray) -> str:
        """Feeds the lifecycle's non-progress watchdog. `level` is the only
        phase declared as progress in the scenario, so curtains and the results
        screen correctly do not count as playing."""
        if bbt_hud.is_results(frame):
            return "results"
        if bbt_hud.is_curtain(frame):
            return "curtain"
        return "level" if self.entered_level else "unknown"

    # ----------------------------------------------------------- diagnostics
    @property
    def quota_disagreement(self) -> bool:
        """True when the game opened the exit at a different count than the
        YAML declares. The declared quota is a hand-entered constant, and a
        hand-entered constant with nothing watching it drifts; this is what
        watches it. A run where this is True should be voided, not scored."""
        return (self.exit_open_at_gems is not None
                and self.exit_open_at_gems != self.quota)

    def ledger(self) -> dict:
        """Everything the score was computed from, so a result can be argued
        with rather than taken on faith."""
        return {
            "entered_level": self.entered_level,
            "tier": (3 if self.completed
                     else 2 if self.gems_max >= self.quota
                     else 1 if self.entered_level else 0),
            "score": round(self._tier_score(), 4),
            "gems_max": self.gems_max,
            "quota": self.quota,
            "yarn_max_seen": self.yarn_max_seen,
            "completed": self.completed,
            "restarted": self.restarted,
            # `tics_in_level` and `completion_tics` are GAME TICS only when
            # `tic_source` says so; otherwise they are decision counts and the
            # speed term is not comparable to a tic-denominated reference.
            "tic_source": "env" if self._tics_from_env else "per-call",
            "tics_in_level": self.tics,
            "completion_tics": self.completion_tics,
            "quota_met_tics": self.quota_met_tics,
            "first_gem_tics": dict(self.first_gem_tics),
            "reference_tics": self.reference_tics,
            "curtain_tics": self.curtain_tics,
            "unreadable_frames": self.unreadable_frames,
            "quota_disagreement": self.quota_disagreement,
        }
