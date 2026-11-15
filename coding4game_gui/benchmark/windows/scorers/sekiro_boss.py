"""Phase-aware image scorer for calibrated multi-life Sekiro boss fights.

Progress is the completed-life count plus the confirmed depletion of the
currently visible vitality bar, divided by the configured ``total_lives``.
Life transitions are accepted only from calibrated visual phase signatures;
a health-bar refill alone can never advance the phase because Sekiro permits
posture deathblows while substantial vitality remains.

All calibration lives in the scenario YAML. Missing or uncalibrated visual
evidence is unknown: it neither rewards nor terminates the episode.
"""
from __future__ import annotations

import math
from pathlib import Path

import cv2

import numpy as np


class SekiroBossScorer:
    """Monotonic 0..scale scorer for a calibrated Sekiro boss encounter."""

    def __init__(self, spec: dict, client_size, ocr=None):
        self.spec = spec or {}
        calibrated = self.spec.get("calibrated_client_size")
        if calibrated and tuple(calibrated) != tuple(client_size):
            raise ValueError(
                f"client size {tuple(client_size)} != calibrated "
                f"{tuple(calibrated)} - fix the game window or recalibrate"
            )

        self.total_lives = max(1, int(self.spec.get("total_lives", 1)))
        self.scale = float(self.spec.get("scale", 100.0))
        self._health = dict(self.spec.get("boss_health") or {})
        self._start_screen = dict(self.spec.get("start_screen") or {})
        self._hud_screen = dict(self.spec.get("hud_screen") or {})
        self._victory_screen = dict(self.spec.get("victory_screen") or {})
        self._defeat_screen = dict(self.spec.get("defeat_screen") or {})
        self._resurrection_screen = dict(
            self.spec.get("resurrection_screen") or {})
        self._terminal_phases = frozenset(
            str(phase) for phase in (
                self.spec.get("terminal_phases") or ("victory", "defeat")
            )
        )
        self._phase_signatures = list(self.spec.get("phase_signatures") or [])
        self._death_shapes = {}
        template = self._defeat_screen.get("shape_template")
        if template:
            # Trusted host calibration assets never enter the agent workspace.
            with np.load(Path(__file__).parent / template, allow_pickle=False) as data:
                self._death_shapes = {key: data[key].astype(np.float32)
                                      for key in data.files}
            for region in self._defeat_screen.get("shape_regions", []):
                shape = self._death_shapes[region["name"]]
                if shape.ndim != 2 or not np.isfinite(shape).all() or shape.std() <= 0:
                    raise ValueError("invalid calibrated death shape")
        self._victory_shapes = {}
        template = self._victory_screen.get("shape_template")
        if template:
            with np.load(Path(__file__).parent / template, allow_pickle=False) as data:
                self._victory_shapes = {key: data[key].astype(np.float32)
                                        for key in data.files}
            for region in self._victory_screen.get("shape_regions", []):
                shape = self._victory_shapes[region["name"]]
                if shape.ndim != 2 or not np.isfinite(shape).all() or shape.std() <= 0:
                    raise ValueError("invalid calibrated victory shape")
        self.reset()

    def reset(self) -> None:
        self._score = 0.0
        self._completed_lives = 0
        self._pending_hp: float | None = None
        self._pending_hp_count = 0
        self._phase_counts = [0] * len(self._phase_signatures)
        self._terminal_counts = {"victory": 0, "defeat": 0}
        self._confirmed_terminal: str | None = None
        # Keep the object alive while it is the cached frame. Comparing by
        # object identity avoids double-counting delta/is_terminal/classify on
        # one observation without allowing CPython to recycle a dead ndarray's
        # id and accidentally suppress the next real frame.
        self._last_observed_frame: np.ndarray | None = None

    @staticmethod
    def _frac_box(frame: np.ndarray, frac) -> np.ndarray:
        h, w = frame.shape[:2]
        x0, x1, y0, y1 = (float(v) for v in frac)
        return frame[
            max(0, int(y0 * h)):min(h, int(math.ceil(y1 * h))),
            max(0, int(x0 * w)):min(w, int(math.ceil(x1 * w))),
        ]

    @staticmethod
    def _luma(seg: np.ndarray) -> np.ndarray:
        px = seg.astype(np.float32)
        return 0.114 * px[:, :, 0] + 0.587 * px[:, :, 1] + 0.299 * px[:, :, 2]

    def _region_matches(self, frame: np.ndarray, region: dict,
                        defaults: dict) -> bool:
        frac = region.get("frac")
        if not frac:
            return False
        seg = self._frac_box(frame, frac)
        if seg.size == 0:
            return False
        luma = self._luma(seg)

        min_mean = region.get("min_mean_luma")
        max_mean = region.get("max_mean_luma")
        mean = float(luma.mean())
        if min_mean is not None and mean < float(min_mean):
            return False
        if max_mean is not None and mean > float(max_mean):
            return False

        if "bgr_min" in region or "bgr_max" in region:
            lo = np.asarray(region.get("bgr_min", [0, 0, 0]), dtype=np.int16)
            hi = np.asarray(region.get("bgr_max", [255, 255, 255]),
                            dtype=np.int16)
            px = seg.astype(np.int16)
            ink = ((px >= lo) & (px <= hi)).all(axis=2)
        else:
            tolerance = int(region.get(
                "neutral_tolerance", defaults.get("neutral_tolerance", 50)))
            min_luma = float(region.get(
                "min_luma", defaults.get("min_luma", 85.0)))
            px = seg.astype(np.int16)
            spread = px.max(axis=2) - px.min(axis=2)
            ink = (spread <= tolerance) & (luma >= min_luma)

        ratio = float(ink.mean())
        if ("min_ink_ratio" in region
                and ratio < float(region["min_ink_ratio"])):
            return False
        if ("max_ink_ratio" in region
                and ratio > float(region["max_ink_ratio"])):
            return False
        return True

    def _matches_signature(self, frame: np.ndarray, config: dict) -> bool:
        regions = list(config.get("regions") or [])
        return bool(regions) and all(
            self._region_matches(frame, region, config) for region in regions
        )

    def victory_shape_scores(self, frame: np.ndarray) -> dict[str, float]:
        """Match both victory labels after removing local background luminance."""
        scores = {}
        for region in self._victory_screen.get("shape_regions", []):
            name = region["name"]
            target = self._victory_shapes[name]
            crop = self._frac_box(frame, region["frac"])
            if not crop.size:
                scores[name] = 0.0
                continue
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float32)
            gray = cv2.resize(gray, (target.shape[1], target.shape[0]),
                              interpolation=cv2.INTER_AREA)
            feature = np.maximum(0, gray - cv2.GaussianBlur(gray, (0, 0), 3.0))
            scores[name] = (
                float(cv2.matchTemplate(feature, target, cv2.TM_CCOEFF_NORMED)[0, 0])
                if feature.std() > float(region.get("min_contrast", 1.0)) else 0.0)
        return scores

    def _matches_victory(self, frame: np.ndarray) -> bool:
        if not self._victory_shapes:
            return self._matches_signature(frame, self._victory_screen)
        regions = self._victory_screen.get("shape_regions", [])
        scores = self.victory_shape_scores(frame)
        return bool(regions) and all(
            scores[region["name"]] >= float(region["min_correlation"])
            for region in regions)

    def death_shape_scores(self, frame: np.ndarray) -> dict[str, float]:
        """Brightness-normalized red-shape correlations for host diagnostics."""
        scores = {}
        for region in self._defeat_screen.get("shape_regions", []):
            name = region["name"]
            template = self._death_shapes[name]
            crop = self._frac_box(frame, region["frac"]).astype(np.float32)
            if not crop.size:
                scores[name] = 0.0
                continue
            # Red excess suppresses neutral scenery. Zero-mean, unit-norm
            # correlation removes brightness/contrast scale during the fade;
            # a small contrast floor rejects blank frames and quantization noise.
            red = np.maximum(0, crop[:, :, 2] -
                             np.maximum(crop[:, :, 0], crop[:, :, 1]))
            red = cv2.resize(red, (template.shape[1], template.shape[0]),
                             interpolation=cv2.INTER_AREA)
            if float(red.std()) <= float(region.get("min_contrast", 1.0)):
                scores[name] = 0.0
                continue
            scores[name] = float(cv2.matchTemplate(
                red, template, cv2.TM_CCOEFF_NORMED)[0, 0])
        return scores

    def _matches_resurrection(self, frame: np.ndarray) -> bool:
        if not self._matches_signature(frame, self._resurrection_screen):
            return False
        required = self._resurrection_screen.get("require_death_shape")
        if required:
            region = next(r for r in self._defeat_screen["shape_regions"]
                          if r["name"] == required)
            return self.death_shape_scores(frame)[required] >= region["min_correlation"]
        return True

    def _matches_defeat(self, frame: np.ndarray) -> bool:
        regions = self._defeat_screen.get("shape_regions")
        if not regions:
            return self._matches_signature(frame, self._defeat_screen)
        scores = self.death_shape_scores(frame)
        return all(scores[region["name"]] >= float(region["min_correlation"])
                   for region in regions)

    def read_boss_hp(self, frame: np.ndarray) -> float | None:
        """Read the left-to-right vitality fill fraction, or unknown."""
        frac = self._health.get("bar_frac")
        if not frac:
            return None
        seg = self._frac_box(frame, frac)
        smooth = max(1, int(self._health.get("smooth_columns", 5)))
        if smooth % 2 == 0:
            smooth += 1
        if seg.size == 0 or seg.shape[1] < smooth:
            return None

        lo = np.asarray(self._health.get("bgr_min", [0, 0, 0]),
                        dtype=np.int16)
        hi = np.asarray(self._health.get("bgr_max", [255, 255, 255]),
                        dtype=np.int16)
        px = seg.astype(np.int16)
        mask = ((px >= lo) & (px <= hi)).all(axis=2)
        active = mask.mean(axis=0) >= float(
            self._health.get("column_fill_ratio", 0.4))

        if smooth > 1:
            required = math.ceil(smooth * float(
                self._health.get("smooth_required_ratio", 0.6)))
            pad = smooth // 2
            active = np.convolve(
                np.pad(active, (pad, pad), mode="edge").astype(np.float32),
                np.ones(smooth, dtype=np.float32), mode="valid") >= required

        max_gap = max(0, int(self._health.get("max_prefix_gap", 3)))
        last_active = -1
        gap = 0
        for index, is_active in enumerate(active):
            if is_active:
                last_active = index
                gap = 0
            else:
                gap += 1
                if last_active >= 0 and gap > max_gap:
                    break
        if last_active < 0:
            return None
        fill = (last_active + 1) / float(len(active))
        if fill < float(self._health.get("min_visible_fill", 0.03)):
            return None
        full_threshold = self._health.get("full_fill_threshold")
        if (full_threshold is not None
                and fill >= float(full_threshold)):
            return 1.0
        return min(1.0, max(0.0, fill))

    def _confirmed_hp(self, frame: np.ndarray) -> float | None:
        # Color-only vitality measurements can overlap cinematic/menu pixels.
        # A calibrated HUD signature gates reward without changing historical
        # Sekiro configs that predate ``hud_screen``.
        if self._hud_screen and not self._matches_signature(
                frame, self._hud_screen):
            self._pending_hp = None
            self._pending_hp_count = 0
            return None
        hp = self.read_boss_hp(frame)
        if hp is None:
            self._pending_hp = None
            self._pending_hp_count = 0
            return None
        tolerance = float(self._health.get("confirm_tolerance", 0.04))
        required = max(1, int(self._health.get("confirm_frames", 2)))
        if (self._pending_hp is None
                or abs(hp - self._pending_hp) > tolerance):
            self._pending_hp = hp
            self._pending_hp_count = 1
            return hp if required == 1 else None
        self._pending_hp = (
            self._pending_hp * self._pending_hp_count + hp
        ) / (self._pending_hp_count + 1)
        self._pending_hp_count += 1
        return self._pending_hp if self._pending_hp_count >= required else None

    def _observe(self, frame: np.ndarray) -> None:
        if frame is self._last_observed_frame:
            return
        self._last_observed_frame = frame

        # A confirmed phase signature is the only non-victory path that can
        # advance completed_lives. Health refill is deliberately ignored.
        for index, item in enumerate(self._phase_signatures):
            matched = self._matches_signature(
                frame, dict(item.get("signature") or {}))
            self._phase_counts[index] = (
                self._phase_counts[index] + 1 if matched else 0)
            required = max(1, int(item.get("confirm_frames", 2)))
            if matched and self._phase_counts[index] >= required:
                completed = int(item.get("completed_lives", 0))
                if completed > self._completed_lives:
                    self._completed_lives = min(
                        self.total_lives - 1, completed)
                    self._pending_hp = None
                    self._pending_hp_count = 0

        resurrection = self._matches_resurrection(frame)
        for name, config in (
                ("victory", self._victory_screen),
                ("defeat", self._defeat_screen)):
            matched = (self._matches_defeat(frame) if name == "defeat"
                       else self._matches_victory(frame))
            # Keep the resurrection choice distinct from the final-death overlay.
            # is_terminal handles resurrection through registered terminal_phases.
            if name == "defeat" and resurrection:
                matched = False
            self._terminal_counts[name] = (
                self._terminal_counts[name] + 1 if matched else 0)
            required = max(1, int(config.get("confirm_frames", 2)))
            if matched and self._terminal_counts[name] >= required:
                self._confirmed_terminal = name

    def check_start(self, frame: np.ndarray) -> bool:
        """Verify the calibrated, controllable fight frame for tic zero."""
        if not self._matches_signature(frame, self._start_screen):
            return False
        if self._start_screen.get("require_boss_health", True):
            return self.read_boss_hp(frame) is not None
        return True

    def delta(self, frame: np.ndarray) -> float:
        self._observe(frame)
        if self._confirmed_terminal == "victory":
            reward = max(0.0, self.scale - self._score)
            self._score = self.scale
            return reward

        hp = self._confirmed_hp(frame)
        if hp is None:
            return 0.0
        candidate = self.scale * (
            self._completed_lives + (1.0 - hp)
        ) / self.total_lives
        if candidate <= self._score:
            return 0.0
        reward = candidate - self._score
        self._score = candidate
        return reward

    def is_terminal(self, frame: np.ndarray) -> bool:
        self._observe(frame)
        if ("resurrection" in self._terminal_phases
                and self._matches_resurrection(frame)):
            return True
        return self._confirmed_terminal in self._terminal_phases

    def classify_phase(self, frame: np.ndarray) -> str:
        self._observe(frame)
        if self._confirmed_terminal:
            return self._confirmed_terminal
        if self._matches_resurrection(frame):
            return "resurrection"
        has_hud = (not self._hud_screen
                   or self._matches_signature(frame, self._hud_screen))
        return (f"combat_life_{self._completed_lives + 1}"
                if has_hud and self.read_boss_hp(frame) is not None
                else "non_combat")

    @property
    def total(self) -> float:
        return self._score

    @property
    def completed_lives(self) -> int:
        return self._completed_lives

