"""Black Myth: Wukong boss-health progress scorer.

The fixed encounter exposes a persistent neutral-white boss HP bar near the
bottom centre of the client image.  Score is the greatest confirmed fraction
of that bar depleted during the episode, scaled to 0..100.  Missing HUD is
treated as unknown rather than zero HP so hiding/occluding the bar cannot earn
reward.  An exact boss-defeated banner check or the separately calibrated
stable victory-results signature promotes an episode to the maximum score.

Frames follow the Windows benchmark's global BGR uint8 contract.
"""
from __future__ import annotations

import math

import numpy as np

from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer


DEFAULT_BAR_FRAC = (460 / 1280, 825 / 1280, 610 / 720, 620 / 720)


class BlackMythScorer:
    """Monotonic 0..100 progress from confirmed boss HP depletion."""

    def __init__(self, spec: dict, client_size, ocr=None):
        self.spec = spec or {}
        calibrated = self.spec.get("calibrated_client_size")
        if calibrated and tuple(calibrated) != tuple(client_size):
            raise ValueError(
                f"client size {tuple(client_size)} != calibrated "
                f"{tuple(calibrated)} — fix the game window or recalibrate"
            )

        cfg = self.spec.get("boss_health") or {}
        self.bar_frac = tuple(cfg.get("bar_frac", DEFAULT_BAR_FRAC))
        self.min_luma = float(cfg.get("min_luma", 150.0))
        self.neutral_tolerance = int(cfg.get("neutral_tolerance", 35))
        self.column_fill_ratio = float(cfg.get("column_fill_ratio", 0.45))
        self.smooth_columns = max(1, int(cfg.get("smooth_columns", 7)))
        if self.smooth_columns % 2 == 0:
            self.smooth_columns += 1
        self.max_prefix_gap = max(0, int(cfg.get("max_prefix_gap", 3)))
        self.min_visible_fill = float(cfg.get("min_visible_fill", 0.05))
        self.confirm_frames = max(1, int(cfg.get("confirm_frames", 2)))
        self.confirm_tolerance = float(cfg.get("confirm_tolerance", 0.04))
        self.scale = float(cfg.get("scale", 100.0))

        # Terminal screens use stable title ink rather than movable selection
        # rows or the absence of a HUD.  Keeping defeat and victory as separate
        # calibrated signatures prevents a hidden/occluded boss bar from being
        # mistaken for a kill.
        self._defeat_screen = dict(self.spec.get("terminal_menu") or {})
        self._victory_screen = dict(self.spec.get("victory_screen") or {})
        self._victory_splash_gate = dict(
            self.spec.get("victory_splash_gate") or {})
        self._victory_text = dict(self.spec.get("victory_text") or {})
        # Black Myth remains an image scorer for health and stable terminal
        # screens.  The shared OCR reader is used only for semantic assertions:
        # the exact encounter at startup and the short-lived victory banner.
        # Paddle's model remains lazy until one of those checks is requested.
        self._text_reader = OcrScorer(
            self.spec, client_size=client_size, ocr=ocr)
        self.reset()

    def reset(self) -> None:
        self._text_reader.reset()
        self._score = 0.0
        self._pending_hp: float | None = None
        self._pending_count = 0

    def _box(self, frame: np.ndarray) -> np.ndarray:
        return self._frac_box(frame, self.bar_frac).astype(np.int16)

    @staticmethod
    def _frac_box(frame: np.ndarray, frac) -> np.ndarray:
        h, w = frame.shape[:2]
        x0, x1, y0, y1 = frac
        return frame[
            int(y0 * h):int(y1 * h),
            int(x0 * w):int(x1 * w),
        ]

    def _neutral_ink_ratio(self, frame: np.ndarray, frac, *,
                           min_luma: float,
                           neutral_tolerance: int) -> float | None:
        seg = self._frac_box(frame, frac).astype(np.int16)
        if seg.size == 0:
            return None
        b, g, r = seg[:, :, 0], seg[:, :, 1], seg[:, :, 2]
        luma = 0.114 * b + 0.587 * g + 0.299 * r
        neutral = (
            seg.max(axis=2) - seg.min(axis=2)
            <= neutral_tolerance
        )
        return float((neutral & (luma >= min_luma)).mean())

    def _mean_luma(self, frame: np.ndarray, frac) -> float | None:
        seg = self._frac_box(frame, frac).astype(np.int16)
        if seg.size == 0:
            return None
        b, g, r = seg[:, :, 0], seg[:, :, 1], seg[:, :, 2]
        return float((0.114 * b + 0.587 * g + 0.299 * r).mean())

    def _matches_screen(self, frame: np.ndarray, config: dict) -> bool:
        regions = list(config.get("regions") or [])
        if not regions:
            return False
        min_luma = float(config.get("min_luma", 85.0))
        neutral_tolerance = int(config.get("neutral_tolerance", 50))
        for region in regions:
            ratio = self._neutral_ink_ratio(
                frame, region["frac"], min_luma=min_luma,
                neutral_tolerance=neutral_tolerance)
            if ratio is None:
                return False
            if not (float(region["min_ink_ratio"]) <= ratio
                    <= float(region["max_ink_ratio"])):
                return False
        for region in config.get("background_regions") or []:
            mean_luma = self._mean_luma(frame, region["frac"])
            if mean_luma is None:
                return False
            if ("min_mean_luma" in region
                    and mean_luma < float(region["min_mean_luma"])):
                return False
            if ("max_mean_luma" in region
                    and mean_luma > float(region["max_mean_luma"])):
                return False
        return True

    def is_defeat_menu(self, frame: np.ndarray) -> bool:
        """Recognize the stable post-defeat rematch-menu title structure."""
        return self._matches_screen(frame, self._defeat_screen)

    def is_victory_screen(self, frame: np.ndarray) -> bool:
        """Recognize either the immediate banner or stable results page."""
        return (self.is_victory_splash(frame)
                or self._matches_screen(frame, self._victory_screen))

    def check_text(self, frame: np.ndarray, pattern: str, rect=None) -> bool:
        """Expose bounded OCR for the shared frozen startup check."""
        return self._text_reader.check_text(frame, pattern, rect=rect)

    def is_victory_splash(self, frame: np.ndarray) -> bool:
        """Confirm the short-lived boss-defeated banner after an image gate."""
        pattern = self._victory_text.get("pattern")
        if not pattern:
            return False
        if not self._matches_screen(frame, self._victory_splash_gate):
            return False
        return self.check_text(
            frame, str(pattern), rect=self._victory_text.get("rect"))

    def read_boss_hp(self, frame: np.ndarray) -> float | None:
        """Return the visible boss HP fraction, or ``None`` when HUD is absent."""
        seg = self._box(frame)
        if seg.size == 0 or seg.shape[1] < self.smooth_columns:
            return None

        b, g, r = seg[:, :, 0], seg[:, :, 1], seg[:, :, 2]
        luma = 0.114 * b + 0.587 * g + 0.299 * r
        neutral = seg.max(axis=2) - seg.min(axis=2) <= self.neutral_tolerance
        white = neutral & (luma >= self.min_luma)
        active = white.mean(axis=0) >= self.column_fill_ratio

        if self.smooth_columns > 1:
            kernel = np.ones(self.smooth_columns, dtype=np.float32)
            required = math.ceil(self.smooth_columns * 0.6)
            pad = self.smooth_columns // 2
            padded = np.pad(active, (pad, pad), mode="edge")
            active = np.convolve(
                padded.astype(np.float32), kernel, mode="valid"
            ) >= required

        # The bar fills left-to-right.  Permit a few decorative/damaged pixels
        # inside the prefix, but stop when a real empty suffix begins.
        last_active = -1
        gap = 0
        for i, is_active in enumerate(active):
            if is_active:
                last_active = i
                gap = 0
            else:
                gap += 1
                if last_active >= 0 and gap > self.max_prefix_gap:
                    break

        if last_active < 0:
            return None
        fill = (last_active + 1) / float(len(active))
        if fill < self.min_visible_fill:
            return None
        return min(1.0, max(0.0, fill))

    def _confirm(self, hp: float | None) -> float | None:
        if hp is None:
            self._pending_hp = None
            self._pending_count = 0
            return None
        if (self._pending_hp is None
                or abs(hp - self._pending_hp) > self.confirm_tolerance):
            self._pending_hp = hp
            self._pending_count = 1
            return hp if self.confirm_frames == 1 else None
        self._pending_hp = (self._pending_hp * self._pending_count + hp) / (
            self._pending_count + 1
        )
        self._pending_count += 1
        if self._pending_count < self.confirm_frames:
            return None
        return self._pending_hp

    def delta(self, frame: np.ndarray) -> float:
        if self.is_victory_screen(frame):
            reward = max(0.0, self.scale - self._score)
            self._score = self.scale
            self._pending_hp = None
            self._pending_count = 0
            return reward
        hp = self._confirm(self.read_boss_hp(frame))
        if hp is None:
            return 0.0
        candidate = self.scale * (1.0 - hp)
        if candidate <= self._score:
            return 0.0
        reward = candidate - self._score
        self._score = candidate
        return reward

    def is_terminal(self, frame: np.ndarray) -> bool:
        # A missing health bar remains unknown. Only the requested victory text
        # or calibrated stable screens terminate the episode, so camera/HUD
        # occlusion cannot earn 100.
        return self.is_victory_screen(frame) or self.is_defeat_menu(frame)

    def classify_phase(self, frame: np.ndarray) -> str:
        if self.is_victory_screen(frame):
            return "victory"
        if self.is_defeat_menu(frame):
            return "defeat"
        return "combat" if self.read_boss_hp(frame) is not None else "non_combat"

    @property
    def total(self) -> float:
        return self._score
