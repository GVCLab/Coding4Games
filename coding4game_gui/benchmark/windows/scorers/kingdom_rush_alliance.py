"""Kingdom Rush 5: Alliance OCR scorer.

The generic OCR scorer supplies text recognition, calibrated ROI scaling,
and per-frame OCR memoization.  This scorer adds the scenario-specific trust
rules for the lives and wave counters.  Formal victories receive the success
score; a valid gameplay episode that ends before victory receives a bounded
completion score based on the furthest observed wave and surviving lives.
"""
from __future__ import annotations

import re

import numpy as np

from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer, _box_center_in


class KingdomRushAllianceScorer(OcrScorer):
    """Score one formal victory or one bounded non-victory outcome.

    ``n/10`` is an observable *progress proxy*: it denotes a wave entered or
    released, not necessarily a wave fully cleared.  It is therefore sampled
    monotonically during play and paid only once at the terminal/episode
    boundary.  The scenario caps this proxy-based score below the victory
    range so it cannot substitute for the formal victory screen.
    """

    def __init__(self, spec: dict, client_size, ocr=None):
        super().__init__(spec, client_size, ocr=ocr)

        lives = (spec.get("regions") or {}).get("lives") or {}
        if not lives.get("rect"):
            raise ValueError(
                "kingdom_rush_alliance scorer requires regions.lives.rect")

        score = spec.get("victory_score") or {}
        required = {"base", "lives_bonus", "initial_lives"}
        missing = sorted(required - set(score))
        if missing:
            raise ValueError(
                "kingdom_rush_alliance scorer requires victory_score keys: "
                + ", ".join(missing))
        self.victory_base = float(score["base"])
        self.lives_bonus = float(score["lives_bonus"])
        self.initial_lives = int(score["initial_lives"])
        if self.initial_lives <= 0:
            raise ValueError("victory_score.initial_lives must be positive")

        failure = spec.get("failure_score") or {}
        failure_required = {
            "base", "wave_bonus", "lives_bonus", "max_score", "max_wave",
        }
        missing = sorted(failure_required - set(failure))
        if missing:
            raise ValueError(
                "kingdom_rush_alliance scorer requires failure_score keys: "
                + ", ".join(missing))
        self.failure_base = float(failure["base"])
        self.failure_wave_bonus = float(failure["wave_bonus"])
        self.failure_lives_bonus = float(failure["lives_bonus"])
        self.failure_max_score = float(failure["max_score"])
        self.failure_max_wave = int(failure["max_wave"])
        if self.failure_max_wave <= 0:
            raise ValueError("failure_score.max_wave must be positive")
        if self.failure_max_score < self.failure_base:
            raise ValueError(
                "failure_score.max_score must be >= failure_score.base")
        if self.failure_max_score >= self.victory_base:
            raise ValueError(
                "failure_score.max_score must stay below victory_score.base")

    def reset(self) -> None:
        # OcrScorer.__init__ calls reset(), before this subclass has parsed its
        # own configuration.  Keep reset independent of those later fields.
        super().reset()
        self._last_lives: int | None = None
        self._max_wave = 0
        self._victory_paid = False
        self._failure_paid = False
        # The environment may ask all three scorer methods about one ndarray.
        # State must advance once for that observation, regardless of order.
        self._observed_frame: np.ndarray | None = None
        self._observed_phase: str | None = None
        self._lives_ocr_key = None
        self._lives_ocr_items: list[dict] = []

    def _read_lives(self, frame: np.ndarray) -> int | None:
        region = self.spec["regions"]["lives"]
        rect = region.get("recognition_rect")
        if not rect:
            return super().read_region(frame, "lives")
        x, y, w, h = (int(round(v)) for v in self.scale_rect(rect))
        height, width = frame.shape[:2]
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
            raise ValueError("lives recognition rect is outside the client frame")
        crop = frame[y:y + h, x:x + w]
        # Inspect every HUD pixel: sampled full-frame keys can miss a digit.
        key = (frame.shape, crop.tobytes())
        if key != self._lives_ocr_key:
            items = self.ocr.recognize(
                crop, trim_light_text=bool(region.get("trim_light_text", False)))
            self._lives_ocr_items = [{
                **item, "box": [item["box"][0] + x, item["box"][1] + y,
                                item["box"][2], item["box"][3]],
            } for item in items]
            self._lives_ocr_key = key
        return self._region_value("lives", region, self._lives_ocr_items)

    def _observe(self, frame: np.ndarray) -> str | None:
        if frame is self._observed_frame:
            return self._observed_phase

        phase = super().classify_phase(frame)
        self._observed_frame = frame
        self._observed_phase = phase

        if phase == "play":
            value = self._read_lives(frame)
            maximum = getattr(self, "initial_lives", None)
            if maximum is not None and value is not None:
                trustworthy = 0 <= value <= maximum
                non_increasing = (
                    self._last_lives is None or value <= self._last_lives)
                if trustworthy and non_increasing:
                    self._last_lives = value

            wave = super().read_region(frame, "wave")
            max_wave = getattr(self, "failure_max_wave", None)
            if max_wave is not None and wave is not None:
                # Wave HUD reads are accepted only inside the declared range
                # and never move backwards.  A transient high OCR read cannot
                # poison a lower progress value later in the episode.
                if 0 <= wave <= max_wave and wave >= self._max_wave:
                    self._max_wave = wave
        return phase

    def check_text(self, frame: np.ndarray, pattern: str, rect=None) -> bool:
        """Accept the calibrated lives/gold anchor split into same-row OCR boxes."""
        if super().check_text(frame, pattern, rect=rect):
            return True
        if not rect:
            return False
        region = self.scale_rect(rect)
        items = sorted(
            (item for item in self._read(frame)
             if _box_center_in(item["box"], region)),
            key=lambda item: item["box"][0],
        )
        if len(items) < 2:
            return False
        # Never join different HUD rows or text outside the start assertion.
        top = max(item["box"][1] for item in items)
        bottom = min(item["box"][1] + item["box"][3] for item in items)
        if bottom <= top:
            return False
        text = " ".join(item["text"] for item in items)
        return bool(re.search(pattern, text, re.I))

    def classify_phase(self, frame: np.ndarray) -> str | None:
        return self._observe(frame)

    def delta(self, frame: np.ndarray) -> float:
        phase = self._observe(frame)
        if phase == "victory":
            return self._pay_victory()
        if phase == "defeat":
            return self._pay_failure()
        return 0.0

    def _pay_victory(self) -> float:
        if self._victory_paid or self._failure_paid:
            return 0.0
        self._victory_paid = True
        # Preserve the established behavior: a victory without a trustworthy
        # lives read is a successful terminal observation but cannot claim the
        # life-dependent score.
        if self._last_lives is None:
            return 0.0
        gained = (
            self.victory_base
            + self.lives_bonus * self._last_lives / self.initial_lives
        )
        self._total += gained
        return gained

    def _pay_failure(self) -> float:
        if self._victory_paid or self._failure_paid:
            return 0.0
        self._failure_paid = True
        progress = min(1.0, max(0.0, self._max_wave / self.failure_max_wave))
        lives = (0.0 if self._last_lives is None else
                 min(1.0, max(0.0, self._last_lives / self.initial_lives)))
        gained = min(
            self.failure_max_score,
            self.failure_base
            + progress * (self.failure_wave_bonus
                          + self.failure_lives_bonus * lives),
        )
        self._total += gained
        return gained

    def finalize(self) -> float:
        """Pay a partial score when a valid episode has no terminal screen.

        Windows rollout loops can end on a global tic budget or another
        contained boundary while the game is still in ``play``.  The final
        observation has already updated the trusted counters, so this method
        settles the same one-shot failure score without inventing a victory.
        """
        if self._victory_paid or self._failure_paid:
            return 0.0
        return self._pay_failure()

    def is_terminal(self, frame: np.ndarray) -> bool:
        terminal = set(self.spec.get("terminal_phases") or ["defeat"])
        return self._observe(frame) in terminal

    @property
    def last_lives(self) -> int | None:
        return self._last_lives

    @property
    def max_wave(self) -> int:
        return self._max_wave

    def read_region(self, frame: np.ndarray, name: str) -> int | None:
        if name == "lives":
            return self._read_lives(frame)
        return super().read_region(frame, name)
