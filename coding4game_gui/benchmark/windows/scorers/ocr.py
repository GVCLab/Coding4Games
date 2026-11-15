"""Shared OCR-based declarative scorer for real-game HUDs.

Perception primitive: text recognition (PaddleOCR), optionally restricted to
a calibrated HUD rectangle. All game
knowledge lives in the scenario yaml `scorer:` block as TEXT RULES —
keyword lists for phases (declaration order = priority), a regex for
text-anchored counters ("WAVE 3"), and a screen rect for position-anchored
bare numbers (the materials counter has no text label). No image assets.

The judgment layer supports monotonic counters, post-menu spendable rebase,
one-shot rewards for classified phases, and ScorerLost after consecutive
misses. OCR replaces HOW pixels become facts, not what the facts mean.

Frames are np HWC BGR uint8 (bench-wide contract); rects are [x, y, w, h]
in calibrated client-area pixels. OCR results are memoized per frame, so
delta()/is_terminal()/classify_phase() on the same frame cost one inference.
Validated against the template scorer over 110 frames of recorded episodes
(2026-07-09): phase agreement 110/110, combat wave reads 49/49."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import cv2
import numpy as np


class ScorerLost(RuntimeError):
    """Phase/readout both failed for MISS_LIMIT consecutive acts."""


MISS_LIMIT = 10
DEFAULT_MIN_CONF = 0.5
DEFAULT_INPUT_SIZE = (640, 360)   # OCR sees a downscaled frame


class PaddleOcrEngine:
    """Lazy PaddleOCR wrapper: frame (BGR, calibrated size) -> list of
    {text, conf, box[x,y,w,h] in FRAME coordinates}. One engine is shared
    per ResetController (model load ~seconds, inference ~0.2s on GPU)."""

    def __init__(self, input_size=DEFAULT_INPUT_SIZE,
                 min_conf: float = DEFAULT_MIN_CONF, device: str | None = None,
                 config: dict | None = None):
        self.input_size = tuple(input_size)
        self.min_conf = float(min_conf)
        self.device = device
        self.ocr_config = dict(config or {})
        self._ocr = None
        self._text_recognizer = None

    def _configure_windows_gpu_dlls(self) -> None:
        """Expose the CUDA DLLs installed inside this Python environment.

        Paddle's Windows wheel does not automatically search pip-installed
        ``nvidia/*/bin`` directories.  The resulting missing ``zlibwapi`` or
        ``cublas64_11`` errors occur before OCR returns any text.  Configure
        the process-local DLL search path immediately before importing Paddle;
        this is deliberately runtime-only and does not mutate global PATH.
        """
        if os.name != "nt" or not self.device or not self.device.startswith("gpu"):
            return
        prefix = Path(sys.prefix)
        candidates = [prefix / "Library" / "bin"]
        nvidia_root = prefix / "Lib" / "site-packages" / "nvidia"
        if nvidia_root.is_dir():
            candidates.extend(sorted(nvidia_root.glob("*/bin")))
        seen = set()
        existing = os.environ.get("PATH", "").split(os.pathsep)
        additions = []
        for directory in candidates:
            if not directory.is_dir():
                continue
            key = str(directory).lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                os.add_dll_directory(str(directory))
            except (AttributeError, OSError):
                pass
            if str(directory) not in existing:
                additions.append(str(directory))
        if additions:
            os.environ["PATH"] = os.pathsep.join(additions + existing)

    def _ensure(self):
        if self._ocr is None:
            self._configure_windows_gpu_dlls()
            from paddleocr import PaddleOCR
            ocr_cfg = getattr(self, "ocr_config", {})
            kw = dict(lang=ocr_cfg.get("lang", "en"),
                      use_doc_orientation_classify=False,
                      use_doc_unwarping=False,
                      use_textline_orientation=False)
            if self.device:
                kw["device"] = self.device
            for name in ("enable_mkldnn", "mkldnn_cache_capacity",
                         "cpu_threads", "enable_cinn",
                         "text_detection_model_name", "text_recognition_model_name"):
                if name in ocr_cfg:
                    kw[name] = ocr_cfg[name]
            self._ocr = PaddleOCR(**kw)
        return self._ocr

    def __call__(self, frame: np.ndarray) -> list[dict]:
        h, w = frame.shape[:2]
        iw, ih = self.input_size
        res = self._ensure().predict(cv2.resize(frame, (iw, ih)))
        out = []
        if res:
            r = res[0]
            sx, sy = w / iw, h / ih
            for text, conf, poly in zip(r["rec_texts"], r["rec_scores"],
                                        r["rec_polys"]):
                if conf < self.min_conf:
                    continue
                xs = [p[0] * sx for p in poly]
                ys = [p[1] * sy for p in poly]
                out.append({"text": str(text), "conf": float(conf),
                            "box": [int(min(xs)), int(min(ys)),
                                    int(max(xs) - min(xs)),
                                    int(max(ys) - min(ys))]})
        return out

    def recognize(self, frame: np.ndarray, *, trim_light_text=False) -> list[dict]:
        """Recognize one calibrated text line without full-frame detection.

        Optional light-text trimming removes blank HUD padding, preserving
        narrow glyphs such as a single remaining life. Coordinates remain
        relative to the supplied crop; empty foreground is missing evidence.
        """
        source = frame
        x = y = 0
        if source.size == 0:
            return []
        if trim_light_text:
            low = source.min(axis=2)
            high = source.max(axis=2).astype(np.int16)
            yy, xx = np.where((low > 200) & (high - low < 70))
            if not len(xx):
                return []
            x, y = max(0, int(xx.min()) - 3), max(0, int(yy.min()) - 3)
            source = source[y:int(yy.max()) + 4, x:int(xx.max()) + 4]
        if self._text_recognizer is None:
            self._configure_windows_gpu_dlls()
            from paddleocr import TextRecognition

            cfg = self.ocr_config
            kwargs = {name: cfg[name] for name in (
                "enable_mkldnn", "mkldnn_cache_capacity", "cpu_threads",
                "enable_cinn") if name in cfg}
            if self.device:
                kwargs["device"] = self.device
            self._text_recognizer = TextRecognition(
                model_name=cfg.get("text_recognition_model_name",
                                   "PP-OCRv6_medium_rec"), **kwargs)
        results = self._text_recognizer.predict(source)
        if not results:
            return []
        result = results[0]
        text, conf = str(result["rec_text"]), float(result["rec_score"])
        if not text or conf < self.min_conf:
            return []
        h, w = source.shape[:2]
        return [{"text": text, "conf": conf, "box": [x, y, w, h]}]


def _norm(s: str) -> str:
    """Uppercase and strip spaces: game fonts make OCR glue/split words
    unpredictably ("LEVEL UP" vs "LEVELUP", "WAVE 1" vs "WAVE1")."""
    return re.sub(r"\s+", "", s.upper())


def _box_center_in(box, rect) -> bool:
    x, y, w, h = box
    rx, ry, rw, rh = rect
    cx, cy = x + w / 2, y + h / 2
    return rx <= cx <= rx + rw and ry <= cy <= ry + rh


class OcrScorer:
    """Declarative scorer: interprets a scenario yaml `scorer:` block whose
    rules are text, not templates. Phase rules (first hit in declaration
    order wins — put terminal/menu text anchors before combat):
        phases:
          death:   {any: ["RUN LO"]}          # substring of normalized text
          levelup: {any: ["LEVEL UP"]}
          combat:  {pattern: "WAVE\\s*\\d"}   # regex on the raw joined text
    Value regions:
        regions:
          wave:      {pattern: "WAVE\\s*(\\d+)"}       # group(1) -> int
          materials: {rect: [x,y,w,h], kind: number, spendable: true}
    `max_increment` is a hard upper bound on one accepted increase; readings
    above it are discarded even when repeated. `max_jump` is softer: readings
    above it may still be accepted after a second similar OCR read.
    A `number` region takes the bare integer whose OCR box center falls
    inside rect. A region is monotonic (decreases = misreads) unless
    `spendable: true`, legal only right after a levelup/shop crossing.
    `phase_rewards: {victory: 1}` pays once per named phase per episode,
    which supports binary-outcome games without numeric HUD counters."""

    def __init__(self, spec: dict, client_size, ocr=None):
        # SCALE THE RECTS, DO NOT SCALE THE IMAGE. This used to demand that
        # the frame arrive at exactly the calibrated size, which forced the
        # caller to upscale whatever the game actually rendered — and small
        # button labels then had to be read from interpolated pixels.
        # Measured on Balatro: a 1228x691 capture blown up to 1536x864 lost
        # the blind screen's "Select", which reads cleanly at native size, so
        # a text-targeted click could never resolve and the run stalled.
        # Rects are declared in the calibrated space and mapped down here, so
        # the OCR always sees the sharpest frame available.
        cal = tuple(spec.get("calibrated_client_size") or client_size)
        self.calibrated_size = cal
        self.client_size = tuple(client_size)
        self.rect_scale = (self.client_size[0] / cal[0],
                           self.client_size[1] / cal[1])
        if abs(self.rect_scale[0] / self.rect_scale[1] - 1.0) > 0.02:
            raise ValueError(
                f"client size {self.client_size} has a different aspect than "
                f"the calibrated {cal} — rects cannot be mapped")
        self.spec = spec
        ocr_cfg = spec.get("ocr") or {}
        self.ocr_rect = self.scale_rect(ocr_cfg.get("rect"))
        self.ocr = ocr if ocr is not None else PaddleOcrEngine(
            input_size=ocr_cfg.get("input_size", DEFAULT_INPUT_SIZE),
            min_conf=ocr_cfg.get("min_conf", DEFAULT_MIN_CONF),
            device=ocr_cfg.get("device"), config=ocr_cfg)
        self.weights = dict(spec.get("weights") or {})
        self.phase_rewards = {
            str(name): float(value)
            for name, value in (spec.get("phase_rewards") or {}).items()
        }
        self.miss_limit = int(spec.get("miss_limit", MISS_LIMIT))
        # Phase NAMES are per-game data, not constants. This scorer was
        # written for brotato and hardcoded its vocabulary: gains accrued
        # only in "combat", rebases only after "levelup"/"shop". A scenario
        # naming its phases anything else read no regions at all and
        # reported a flat zero — silently, because a scorer that never reads
        # also never misses, so the ScorerLost guard stayed quiet. Balatro
        # scores in "play" and lost two full episodes to exactly that.
        self.score_phases = set(spec.get("score_phases") or ["combat"])
        self.rebase_phases = set(
            spec.get("rebase_phases") or ["levelup", "shop"])
        self._cache_key: bytes | None = None
        self._cache_val: list[dict] | None = None
        self.reset()

    def scale_rect(self, rect):
        """A rect declared in the calibrated space, in FRAME coordinates."""
        if not rect:
            return rect
        sx, sy = self.rect_scale
        x, y, w, h = rect
        return (x * sx, y * sy, w * sx, h * sy)

    def to_calibrated(self, x: float, y: float):
        """A frame coordinate back in the calibrated space.

        Click coordinates are expressed in the calibrated space, so anything
        located on a frame has to be mapped back before it can be clicked.
        """
        sx, sy = self.rect_scale
        return (x / sx, y / sy)

    def reset(self) -> None:
        self._total = 0.0
        self._readings: dict[str, int] = {}
        self._misses = 0
        self._pending_rebase = False   # True after a levelup/shop crossing
        self._pending_jump: dict[str, int] = {}   # big jumps awaiting confirm
        self._paid_phase_rewards: set[str] = set()
        self._last_phase: str | None = None

    # -- perception (memoized per frame) --
    def _read(self, frame: np.ndarray) -> list[dict]:
        source = frame
        offset_x = offset_y = 0
        if self.ocr_rect:
            x, y, w, h = self.ocr_rect
            frame_h, frame_w = frame.shape[:2]
            x1 = max(0, min(frame_w, int(round(x))))
            y1 = max(0, min(frame_h, int(round(y))))
            x2 = max(x1, min(frame_w, int(round(x + w))))
            y2 = max(y1, min(frame_h, int(round(y + h))))
            if x2 <= x1 or y2 <= y1:
                raise ValueError("scorer OCR rect is outside the client frame")
            source = frame[y1:y2, x1:x2]
            offset_x, offset_y = x1, y1
        key = source[::16, ::16].tobytes()  # cheap sample-hash identity
        if key != self._cache_key:
            items = self.ocr(source)
            if offset_x or offset_y:
                items = [
                    {
                        **it,
                        "box": [it["box"][0] + offset_x,
                                it["box"][1] + offset_y,
                                it["box"][2], it["box"][3]],
                    }
                    for it in items
                ]
            self._cache_val = items
            self._cache_key = key
        return self._cache_val

    # -- facts from text --
    def _phase_visual_ready(self, frame: np.ndarray, rule: dict) -> bool:
        guard = rule.get("visual_ready")
        if not guard:
            return True
        x, y, w, h = self.scale_rect(guard["rect"])
        height, width = frame.shape[:2]
        x1, y1 = max(0, int(round(x))), max(0, int(round(y)))
        x2 = min(width, int(round(x + w)))
        y2 = min(height, int(round(y + h)))
        if x2 <= x1 or y2 <= y1:
            return False
        region = frame[y1:y2, x1:x2]
        threshold = int(guard.get("all_channels_min", 210))
        minimum = int(guard.get("min_pixels", 1))
        return int(np.count_nonzero(np.all(region >= threshold, axis=2))) >= minimum

    def _phase_regions_ready(self, items: list[dict], rule: dict) -> bool:
        regions = self.spec.get("regions") or {}
        return all(name in regions
                   and self._region_value(name, regions[name], items) is not None
                   for name in rule.get("required_regions") or [])

    def classify_phase(self, frame: np.ndarray) -> str | None:
        items = self._read(frame)
        for name, rule in self.spec["phases"].items():
            phase_items = items
            if rule.get("rect"):
                rect = self.scale_rect(rule["rect"])
                phase_items = [
                    it for it in items if _box_center_in(it["box"], rect)
                ]
            phase_raw = " ".join(it["text"] for it in phase_items)
            phase_blob = _norm(phase_raw)
            for kw in rule.get("any") or []:
                if (_norm(kw) in phase_blob
                        and self._phase_visual_ready(frame, rule)
                        and self._phase_regions_ready(items, rule)):
                    return name
            pat = rule.get("pattern")
            if (pat and (re.search(pat, phase_raw, re.I)
                         or re.search(pat, phase_blob, re.I))
                    and self._phase_visual_ready(frame, rule)
                    and self._phase_regions_ready(items, rule)):
                return name
        return None

    def _region_value(self, name: str, region: dict,
                      items: list[dict]) -> int | None:
        label = region.get("label")
        if label:
            # Check the current frame's local HUD label, never stale phase text
            # or a matching word elsewhere in an overlay. Join split OCR words.
            label_rect = self.scale_rect(label["rect"])
            local = sorted((it for it in items if _box_center_in(it["box"], label_rect)),
                           key=lambda it: (it["box"][1], it["box"][0]))
            text = " ".join(it["text"].strip() for it in local)
            if not (re.fullmatch(label["pattern"], text, re.I)
                    or re.fullmatch(label["pattern"], _norm(text), re.I)):
                return None
        pat = region.get("pattern")
        rect = self.scale_rect(region.get("rect"))
        if pat and rect:
            # Pattern applied WITHIN a rect, leftmost match wins. Needed for a
            # value the OCR may or may not merge with its neighbours: Balatro's
            # ante reads as separate "1" and "8" boxes at native resolution but
            # as one "1/8" box once a frame has been rescaled. A bare rect
            # rejects "1/8" (it only takes pure-digit boxes) and a bare pattern
            # would scan the whole screen, where "8/8" (hands) and "44/52"
            # (deck) collide. Leftmost is principled here: a numerator always
            # precedes its denominator.
            best = None
            match_left_max = region.get("match_left_max")
            for it in sorted(items, key=lambda d: d["box"][0]):
                if not _box_center_in(it["box"], rect):
                    continue
                # A missing fraction numerator must not promote its denominator.
                # Keep merged fractions whose box starts in the numerator area.
                if (match_left_max is not None
                        and it["box"][0] > match_left_max * self.rect_scale[0]):
                    continue
                m = (re.search(pat, it["text"], re.I)
                     or re.search(pat, _norm(it["text"]), re.I))
                if m:
                    best = int(m.group(1))
                    break
            if best is not None:
                return best
            fallback = region.get("fallback")
            return (self._region_value(name, fallback, items)
                    if isinstance(fallback, dict) else None)
        if pat:
            m = re.search(pat, " ".join(it["text"] for it in items), re.I) \
                or re.search(pat, _norm(" ".join(it["text"] for it in items)))
            return int(m.group(1)) if m else None
        if rect:
            for it in items:
                if _box_center_in(it["box"], rect) \
                        and re.fullmatch(r"\d{1,5}", it["text"].strip()):
                    return int(it["text"].strip())
        fallback = region.get("fallback")
        if isinstance(fallback, dict):
            return self._region_value(name, fallback, items)
        return None

    def snapshot(self, frame: np.ndarray) -> dict | None:
        """Read one complete labeled HUD without carrying state across frames."""
        phase = self.classify_phase(frame)
        if phase not in set(self.spec.get('snapshot_phases', self.score_phases)):
            return None
        items = self._read(frame)
        readings = {}
        total = 0.0
        for name, region in self.spec['regions'].items():
            value = self._region_value(name, region, items)
            if value is None or value < 0:
                return None
            readings[name] = value
            total += self.weights.get(name, 0.0) * max(0, value - region.get('baseline', 0))
        if not readings:
            return None
        return dict(score=total, phase=phase, readings=readings)

    # -- judgment (ported unchanged from the template scorer) --
    def delta(self, frame: np.ndarray) -> float:
        phase = self.classify_phase(frame)
        items = self._read(frame)
        if phase is not None:
            self._last_phase = phase
        # Balatro's score animation temporarily hides the poker-hand labels,
        # while the numeric round score remains readable. Reuse the last
        # explicitly recognized scoring phase for those transition frames.
        effective_phase = phase
        if effective_phase is None and self._last_phase in self.score_phases:
            effective_phase = self._last_phase
        gained = 0.0
        any_read = False
        if effective_phase in self.score_phases:
            regions = self.spec.get("regions") or {}
            # A phase-only binary scorer has no numeric HUD fields. Merely
            # recognizing its gameplay phase is a healthy read, not scorer
            # loss; the terminal phase reward carries the entire score.
            any_read = not regions
            for rname, region in regions.items():
                value = self._region_value(rname, region, items)
                if value is None:      # occluded HUD: keep the last reading
                    continue
                any_read = True
                prev = self._readings.get(rname)
                if prev is None:
                    base = region.get("baseline")
                    if base is None:
                        self._readings[rname] = value      # episode baseline
                        continue
                    # declared baseline: the FIRST reading itself scores
                    # (value - baseline) — e.g. wave with baseline 0 makes
                    # the score literally 100 x wave, counting wave 1, so
                    # the ledger matches the documented formula. Falls
                    # through to the gain branch, so a bogus huge first
                    # read is still held for max_jump confirmation.
                    prev = int(base)
                    if value <= prev:
                        self._readings[rname] = value
                        continue
                if value > prev:                            # wave jump / gain
                    # Sequential counters can impose a HARD per-read bound.
                    # Never clamp or adopt an over-limit reading: otherwise a
                    # persistent OCR error leaks reward or poisons the trusted
                    # baseline. Unlike max_jump below, repetition cannot make
                    # this reading acceptable.
                    max_increment = region.get("max_increment")
                    if max_increment is not None \
                            and value - prev > int(max_increment):
                        self._pending_jump.pop(rname, None)
                        continue
                    # BIG jumps need a second consecutive read to confirm:
                    # transient overlay text (combat damage numbers drifting
                    # through the HUD rect) can OCR as a huge bogus number,
                    # and the monotonic guard would adopt it FOREVER —
                    # observed live 2026-07-09: materials 16 -> "1810" from
                    # one damage-number frame, +1758 phantom score. Overlays
                    # change every frame, so they never read the same twice;
                    # a real jump persists on the HUD and confirms next act.
                    cap = region.get("max_jump")
                    if cap and value - prev > int(cap):
                        pend = self._pending_jump.get(rname)
                        tol = max(3, int(value * 0.05))
                        if pend is not None and abs(value - pend) <= tol:
                            gained += self.weights.get(rname, 0.0) \
                                * (value - prev)
                            self._readings[rname] = value
                            self._pending_jump.pop(rname, None)
                        else:
                            self._pending_jump[rname] = value
                    else:
                        gained += self.weights.get(rname, 0.0) * (value - prev)
                        self._readings[rname] = value
                        self._pending_jump.pop(rname, None)
                elif value < prev and self._pending_rebase \
                        and region.get("spendable"):
                    self._readings[rname] = value           # legal post-shop spend
                    self._pending_jump.pop(rname, None)
                # else: any other drop = misread — monotonic regions NEVER
                # adopt decreases
            if any_read:
                self._pending_rebase = False
        elif effective_phase in self.rebase_phases:
            self._pending_rebase = True
        if phase in self.phase_rewards and phase not in self._paid_phase_rewards:
            gained += self.phase_rewards[phase]
            self._paid_phase_rewards.add(phase)
        ok = (effective_phase is not None
              and (effective_phase not in self.score_phases or any_read))
        self._misses = 0 if ok else self._misses + 1
        if self._misses >= self.miss_limit:
            raise ScorerLost(
                f"phase/readout failed {self.miss_limit} consecutive acts "
                f"(HUD lost – window moved, overlay, or bad calibration?)")
        self._total += gained
        return gained

    def is_terminal(self, frame: np.ndarray) -> bool:
        terminal = self.spec.get("terminal_phases") or ["death"]
        return self.classify_phase(frame) in set(terminal)

    def locate_text(self, frame: np.ndarray, pattern: str):
        """Centre of the on-screen text matching `pattern`, or None.

        Backs text-targeted click actions. Reuses the per-frame memoised OCR
        the scorer already performs, so resolving a moving control costs no
        extra inference. Matching runs on both the raw and space-stripped
        text, like the other text checks here.
        """
        best = None
        for it in self._read(frame):
            if re.search(pattern, it["text"], re.I) \
                    or re.search(pattern, _norm(it["text"]), re.I):
                # Prefer the highest-confidence match: a moving control can
                # share its wording with static decoration elsewhere.
                if best is None or it["conf"] > best["conf"]:
                    best = it
        if best is None:
            return None
        x, y, w, h = best["box"]
        return (x + w / 2.0, y + h / 2.0)

    def read_region(self, frame: np.ndarray, name: str) -> int | None:
        """Read one declared region's current value off a frame (same read
        path scoring uses), with NO judgment/state side effects. Used by the
        startup start_check to assert initial values — e.g. wave must be 1,
        catching a leftover mid-run game that start-of-wave full HP lets
        through (observed 2026-07-09: an episode began on a wave-5 leftover)."""
        region = (self.spec.get("regions") or {}).get(name)
        if region is None:
            raise KeyError(f"region {name!r} not declared in scorer spec")
        return self._region_value(name, region, self._read(frame))

    def check_text(self, frame: np.ndarray, pattern: str,
                   rect=None, *, join_text_boxes: bool = False) -> bool:
        """Generic text presence check (used by startup start_check): does any
        recognized string match `pattern` (optionally with its box center
        inside `rect`)? Matching runs on both raw and space-stripped text."""
        items = self._read(frame)
        if rect:
            scaled = self.scale_rect(rect)
            items = [it for it in items if _box_center_in(it["box"], scaled)]
        for it in items:
            if re.search(pattern, it["text"], re.I) \
                    or re.search(pattern, _norm(it["text"])):
                return True
        if rect and not join_text_boxes:
            return False
        # Fall back to the JOINED text, the way classify_phase reads. OCR
        # splits a phrase across boxes as it pleases — "Choose your next
        # Blind" came back as 'Choose' + 'your' + 'next' + 'Blind' — so a
        # per-box check can never match a multi-word anchor. That made every
        # reset's arrival check fail against a screen that plainly said it,
        # and the recovery clicked on regardless. Rectangular checks can opt
        # into joining their local boxes; the default retains single-box checks.
        raw = " ".join(it["text"] for it in items)
        return bool(re.search(pattern, raw, re.I)
                    or re.search(pattern, _norm(raw), re.I))

    @property
    def total(self) -> float:
        return self._total
