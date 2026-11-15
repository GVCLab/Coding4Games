"""Forza Horizon 5 scorer for its HUD distance-to-target readout.

The scorer reuses :class:`OcrScorer`'s production PaddleOCR, coordinate
scaling, text checks, and frame cache.  Unlike the generic counter scorer, a
    distance is allowed to move in both directions: the score always represents
    ``baseline_distance_m - latest_distance_m``. Forza removes the destination
    HUD when the car enters its arrival radius rather than displaying a stable
    zero. A configured visible success distance terminates immediately;
    confirmed near-target disappearance remains the fallback.
"""
from __future__ import annotations

import re

import numpy as np

from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer
from coding4game_gui.benchmark.windows.scorers.ocr import _box_center_in


DEFAULT_PATTERN = r"(?<![\d.,])(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>KM|M)\b"


class ForzaHorizon5Scorer(OcrScorer):
    """Read remaining metres from one calibrated HUD rectangle."""

    def __init__(self, spec: dict, client_size, ocr=None):
        super().__init__(spec, client_size, ocr=ocr)
        cfg = spec.get("distance") or {}
        if not cfg.get("rect"):
            raise ValueError("forza_horizon_5 scorer requires distance.rect")
        if cfg.get("baseline_m") is None:
            raise ValueError(
                "forza_horizon_5 scorer requires distance.baseline_m")
        self.distance_rect = cfg["rect"]
        self.distance_text_rect = cfg.get("text_rect")
        self.baseline_m = float(cfg["baseline_m"])
        self.max_distance_m = float(cfg.get("max_m", 100_000.0))
        self.distance_pattern = re.compile(
            str(cfg.get("pattern") or DEFAULT_PATTERN), re.I)

        arrival = spec.get("arrival") or {}
        threshold = arrival.get("threshold_m")
        self.arrival_threshold_m = (
            None if threshold is None else float(threshold))
        success_threshold = arrival.get("success_threshold_m")
        self.arrival_success_threshold_m = (
            None if success_threshold is None else float(success_threshold))
        self.arrival_confirm_missing = int(
            arrival.get("confirm_missing_observations", 2))
        if self.arrival_threshold_m is not None:
            if not 0.0 < self.arrival_threshold_m < self.baseline_m:
                raise ValueError(
                    "arrival.threshold_m must be between zero and baseline_m")
            if self.arrival_confirm_missing < 1:
                raise ValueError(
                    "arrival.confirm_missing_observations must be positive")
        if self.arrival_success_threshold_m is not None:
            if not 0.0 <= self.arrival_success_threshold_m < self.baseline_m:
                raise ValueError(
                    "arrival.success_threshold_m must be non-negative and "
                    "below baseline_m")
            if (self.arrival_threshold_m is not None and
                    self.arrival_success_threshold_m > self.arrival_threshold_m):
                raise ValueError(
                    "arrival.success_threshold_m must not exceed threshold_m")

    def reset(self) -> None:
        # OcrScorer.__init__ calls reset(), so this must not depend on fields
        # initialized after super().__init__ returns.
        super().reset()
        self._last_distance_m: float | None = None
        self._arrival_armed = False
        self._arrival_missing = 0
        self._arrived = False
        # delta(), is_terminal(), and classify_phase() are deliberately called
        # on the same ndarray by the environment.  Hold the exact object so one
        # observation advances the arrival state at most once, while a later
        # equal-looking frame still counts as a distinct confirmation.
        self._arrival_frame: np.ndarray | None = None
        self._arrival_frame_distance: float | None = None
        self._distance_ocr_key = None
        self._distance_ocr_items = None

    def _read(self, frame: np.ndarray) -> list[dict]:
        text_rect = getattr(self, "distance_text_rect", None)
        if not text_rect:
            return super()._read(frame)
        x, y, w, h = (int(round(v)) for v in self.scale_rect(text_rect))
        height, width = frame.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(width, x + w), min(height, y + h)
        if x1 <= x0 or y1 <= y0:
            return []
        roi = frame[y0:y1, x0:x1]
        # Hash every text pixel: a sampled full-frame hash can miss a digit
        # change. Preserve native coordinates and leave evidence untouched.
        key = (frame.shape, roi.tobytes())
        if key != self._distance_ocr_key:
            masked = np.zeros_like(frame)
            masked[y0:y1, x0:x1] = roi
            self._distance_ocr_items = self.ocr(masked)
            self._distance_ocr_key = key
        return self._distance_ocr_items

    def _distance_from_items(self, items: list[dict]) -> float | None:
        rect = self.scale_rect(self.distance_rect)
        candidates = []
        for item in items:
            if not _box_center_in(item["box"], rect):
                continue
            match = self.distance_pattern.search(item["text"])
            if match is None:
                # Paddle may insert or remove whitespace unpredictably.
                compact = re.sub(r"\s+", "", item["text"].upper())
                match = self.distance_pattern.search(compact)
            if match is None:
                continue
            raw_value = match.group("value").replace(",", ".")
            value = float(raw_value)
            if match.group("unit").upper() == "KM":
                value *= 1000.0
            elif value > 1000:
                # The metre HUD includes 1000 M at the unit boundary. Only
                # remove a merged map-pin "1" when the complete metre value
                # exceeds that boundary and its suffix is a valid HUD value.
                if not raw_value.startswith("1"):
                    continue
                value = float(raw_value[1:])
                if not 0 < value <= 1000:
                    continue
            if 0.0 <= value <= self.max_distance_m:
                candidates.append((float(item.get("conf", 0.0)), value))
        if not candidates:
            return None
        return max(candidates, key=lambda pair: pair[0])[1]

    def read_distance_m(self, frame: np.ndarray) -> float | None:
        """Read the current HUD value without changing scorer state."""
        return self._distance_from_items(self._read(frame))

    def read_region(self, frame: np.ndarray, name: str) -> float | None:
        if name != "distance":
            raise KeyError(f"region {name!r} not declared in distance scorer")
        return self.read_distance_m(frame)

    def _observe_distance(self, frame: np.ndarray) -> float | None:
        if frame is self._arrival_frame:
            return self._arrival_frame_distance

        distance_m = self.read_distance_m(frame)
        self._arrival_frame = frame
        self._arrival_frame_distance = distance_m
        if self._arrived:
            return distance_m

        threshold = getattr(self, "arrival_threshold_m", None)
        if distance_m is not None:
            self._last_distance_m = distance_m
            self._arrival_missing = 0
            success_threshold = getattr(
                self, "arrival_success_threshold_m", None)
            if (success_threshold is not None and
                    distance_m <= success_threshold):
                self._arrived = True
            else:
                self._arrival_armed = (
                    threshold is not None and distance_m <= threshold)
        elif self._arrival_armed:
            self._arrival_missing += 1
            if self._arrival_missing >= self.arrival_confirm_missing:
                self._arrived = True
        else:
            self._arrival_missing = 0
        return distance_m

    def classify_phase(self, frame: np.ndarray) -> str | None:
        distance_m = self._observe_distance(frame)
        if self._arrived:
            return "arrival"
        return "driving" if distance_m is not None else None

    def delta(self, frame: np.ndarray) -> float:
        distance_m = self._observe_distance(frame)
        if self._arrived:
            target_total = self.baseline_m
        elif distance_m is None:
            # A hidden/occluded HUD must not create reward or erase the last
            # trustworthy score. If no reading has ever succeeded, total is 0.
            return 0.0
        else:
            target_total = self.baseline_m - distance_m
        gained = target_total - self._total
        self._total = target_total
        return gained

    def is_terminal(self, frame: np.ndarray) -> bool:
        self._observe_distance(frame)
        return self._arrived

    @property
    def last_distance_m(self) -> float | None:
        return self._last_distance_m
