"""OCR scorer for Riders Republic's fixed-duration trick activity."""
from __future__ import annotations

import re

import numpy as np

from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer
from coding4game_gui.benchmark.windows.scorers.ocr import _box_center_in


class RidersRepublicScorer(OcrScorer):
    """Track the highest trustworthy game-awarded trick total.

    The live HUD renders a single zero too weakly for PaddleOCR, while its
    nearby English/localized score label remains stable. Label-without-number
    therefore means zero. Once positive, ordinary missing or lower reads
    preserve the previous total. Crashes are intentionally non-terminal under
    this scenario.
    """

    def __init__(self, spec: dict, client_size, ocr=None):
        super().__init__(spec, client_size, ocr=ocr)
        region = (spec.get("regions") or {}).get("score") or {}
        label = region.get("label") or {}
        if not region.get("rect") or not label.get("rect"):
            raise ValueError(
                "riders_republic scorer requires score and label rectangles")
        self.score_rect = region["rect"]
        self.label_rect = label["rect"]
        self.label_pattern = re.compile(
            str(label.get("pattern") or r"(?:SCORE|得分)"), re.I)
        self.max_score = int(spec.get("max_score", 10_000_000))
        if self.max_score < 1:
            raise ValueError("riders_republic max_score must be positive")
        self._score_ocr_key = None
        self._score_ocr_items = None

    def reset(self) -> None:
        super().reset()
        self._score_ocr_key = None
        self._score_ocr_items = None

    def _read(self, frame: np.ndarray) -> list[dict]:
        # OCR only the compact upper-left HUD while retaining native box
        # coordinates. This excludes the lower-right speedometer by design.
        rects = [self.scale_rect(self.score_rect), self.scale_rect(self.label_rect)]
        x0 = max(0, int(min(rect[0] for rect in rects)))
        y0 = max(0, int(min(rect[1] for rect in rects)))
        x1 = min(frame.shape[1], int(max(rect[0] + rect[2] for rect in rects)))
        y1 = min(frame.shape[0], int(max(rect[1] + rect[3] for rect in rects)))
        roi = frame[y0:y1, x0:x1]
        key = (frame.shape, roi.tobytes())
        if key != self._score_ocr_key:
            masked = np.zeros_like(frame)
            masked[y0:y1, x0:x1] = roi
            self._score_ocr_items = self.ocr(masked)
            self._score_ocr_key = key
        return self._score_ocr_items

    def read_score(self, frame: np.ndarray) -> int | None:
        items = self._read(frame)
        label_rect = self.scale_rect(self.label_rect)
        label_seen = any(
            _box_center_in(item["box"], label_rect)
            and self.label_pattern.search(item["text"])
            for item in items
        )
        if not label_seen:
            return None

        score_rect = self.scale_rect(self.score_rect)
        candidates = []
        for item in items:
            if not _box_center_in(item["box"], score_rect):
                continue
            compact = re.sub(r"[\s,. ]", "", item["text"])
            if not re.fullmatch(r"\d{1,8}", compact):
                continue
            value = int(compact)
            if 0 <= value <= self.max_score:
                candidates.append((float(item.get("conf", 0.0)), value))
        if not candidates:
            return 0
        return max(candidates, key=lambda pair: pair[0])[1]

    def read_region(self, frame: np.ndarray, name: str) -> int | None:
        if name != "score":
            raise KeyError(f"region {name!r} not declared in Riders Republic scorer")
        return self.read_score(frame)

    def classify_phase(self, frame: np.ndarray) -> str | None:
        return "play" if self.read_score(frame) is not None else None

    def delta(self, frame: np.ndarray) -> float:
        value = self.read_score(frame)
        if value is None or value <= self._total:
            self._pending_jump.pop("score", None)
            return 0.0
        region = (self.spec.get("regions") or {}).get("score") or {}
        max_jump = region.get("max_jump")
        if max_jump and value - self._total > int(max_jump):
            pending = self._pending_jump.get("score")
            tolerance = max(3, int(value * 0.05))
            if pending is None or abs(value - pending) > tolerance:
                self._pending_jump["score"] = value
                return 0.0
        self._pending_jump.pop("score", None)
        gained = float(value) - self._total
        self._total = float(value)
        return gained

    def is_terminal(self, frame: np.ndarray) -> bool:
        return False
