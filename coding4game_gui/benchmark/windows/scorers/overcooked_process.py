"""Versioned, evidence-only process scoring for Overcooked! 2 Story 2-1.

The ledger is deliberately independent of input events. A detector may add a
fact only when a native game image proves it; later states imply the required
earlier preparation states. Ambiguous images simply add no fact.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import queue
import threading

import cv2
import numpy as np


VERSION = "overcooked_2_2_1_composite_v1"
INGREDIENTS = {
    "fries": ("potato",),
    "fried_chicken": ("chicken",),
    "chicken_with_fries": ("chicken", "potato"),
}
STAGES = ("upper_raft", "chopped", "fried", "plated")
WEIGHTS = {"upper_raft": 4, "chopped": 4, "fried": 6, "plated": 6}


@dataclass(frozen=True)
class VisualFact:
    attempt_id: str
    order_type: str
    ingredient: str | None
    stage: str
    frame_id: int
    source: str
    rect: tuple[int, int, int, int]
    explanation: str


class ProcessLedger:
    """Keep one proven state per ingredient and stage of each visible order."""

    def __init__(self):
        self.facts: dict[tuple[str, str | None, str], VisualFact] = {}
        self.order_types: dict[str, str] = {}

    def add(self, fact: VisualFact) -> bool:
        if fact.order_type not in INGREDIENTS or fact.stage not in STAGES:
            raise ValueError("unknown Overcooked process fact")
        required = INGREDIENTS[fact.order_type]
        if fact.stage == "plated":
            if fact.ingredient is not None:
                raise ValueError("plated evidence must identify a complete plate")
        elif fact.ingredient not in required:
            raise ValueError("ingredient is not required by the visible order")
        existing = self.order_types.get(fact.attempt_id)
        if existing is not None and existing != fact.order_type:
            raise ValueError("order attempt changed recipe")
        self.order_types[fact.attempt_id] = fact.order_type
        key = (fact.attempt_id, fact.ingredient, fact.stage)
        if key in self.facts:
            return False
        self.facts[key] = fact
        return True

    def _attempt(self, attempt_id: str) -> tuple[int, list[dict]]:
        order_type = self.order_types[attempt_id]
        required = INGREDIENTS[order_type]
        total = 0
        evidence = []
        plate = self.facts.get((attempt_id, None, "plated"))
        for ingredient in required:
            highest = -1
            witness = None
            for index, stage in enumerate(STAGES[:3]):
                fact = self.facts.get((attempt_id, ingredient, stage))
                if fact is not None and index > highest:
                    highest, witness = index, fact
            if plate is not None:
                highest, witness = 2, plate
            for index in range(highest + 1):
                stage = STAGES[index]
                direct = self.facts.get((attempt_id, ingredient, stage))
                proof = direct or witness
                total += WEIGHTS[stage] / len(required)
                evidence.append(dict(
                    attempt_id=attempt_id, order_type=order_type,
                    ingredient=ingredient, stage=stage,
                    provenance="direct" if direct else "inferred_from_later_state",
                    witness_frame_id=proof.frame_id, source=proof.source,
                    rect=list(proof.rect), explanation=proof.explanation,
                ))
        if plate is not None:
            total += WEIGHTS["plated"]
            evidence.append(dict(
                attempt_id=attempt_id, order_type=order_type,
                ingredient=None, ingredients=list(required),
                stage="plated", provenance="direct",
                witness_frame_id=plate.frame_id, source=plate.source,
                rect=list(plate.rect), explanation=plate.explanation,
            ))
        return int(total), evidence

    def result(self, completed_orders: int | None = None) -> tuple[int, list[dict]]:
        if completed_orders is not None and completed_orders > 0:
            return 20, [dict(order_type=None, ingredient=None,
                             stage="completed_order", provenance="result_page",
                             completed_orders=completed_orders)]
        candidates = [self._attempt(key) for key in self.order_types]
        return max(candidates, key=lambda item: item[0], default=(0, []))


def breakdown(net_score: int | None, threshold: int, process_score: int,
              *, valid_result: bool) -> dict:
    if not 0 <= process_score <= 20:
        raise ValueError("process score is outside 0..20")
    result = dict(score_version=VERSION, process_score=process_score,
                  net_score=net_score if valid_result else None,
                  game_score=None, final_component=None, combined_score=None,
                  success=None)
    if valid_result:
        if net_score is None or threshold <= 0:
            raise ValueError("valid result requires a net score and threshold")
        result["game_score"] = min(100.0, 100.0 * net_score / threshold)
        result["final_component"] = 80.0 * min(1.0, max(0.0, net_score / threshold))
        result["combined_score"] = process_score + result["final_component"]
        result["success"] = net_score >= threshold
    return result


class ProcessObserver:
    """Conservative native-frame witness collector at existing capture points."""

    def __init__(self, detector=None):
        self.ledger = ProcessLedger()
        self.detector = detector or Story21Detector()
        self.frame_id = 0
        self.images: dict[int, bytes] = {}
        self.error: BaseException | None = None
        self._pending: queue.Queue = queue.Queue(maxsize=64)
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="story21-process-evidence", daemon=True)
        self._worker.start()

    def _run(self):
        while True:
            work = self._pending.get()
            try:
                if work is None:
                    return
                frame, frame_id, source = work
                if self.error is not None or self.ledger.result()[0] == 20:
                    continue
                for fact in self.detector.detect(frame, frame_id, source):
                    if self.ledger.add(fact):
                        ok, encoded = cv2.imencode(".png", frame,
                                                   [cv2.IMWRITE_PNG_COMPRESSION, 1])
                        if not ok:
                            raise RuntimeError("could not preserve process witness")
                        self.images[fact.frame_id] = encoded.tobytes()
            except BaseException as exc:
                self.error = exc
            finally:
                self._pending.task_done()

    def observe(self, frame: np.ndarray, source: str) -> None:
        if self.error is not None:
            raise RuntimeError("process evidence previously failed") from self.error
        if self._closed:
            raise RuntimeError("process evidence is closed")
        self.frame_id += 1
        try:
            self._pending.put_nowait((frame.copy(), self.frame_id, source))
        except queue.Full as exc:
            self.error = RuntimeError("process evidence queue overflowed")
            raise self.error from exc

    def result(self, completed_orders: int | None = None) -> tuple[int, list[dict]]:
        if not self._closed:
            self._pending.put(None)
            self._closed = True
            self._pending.join()
            self._worker.join()
        if self.error is not None:
            raise RuntimeError("process evidence failed") from self.error
        return self.ledger.result(completed_orders)

    def save(self, directory: Path, evidence: list[dict]) -> list[dict]:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        saved = {}
        for item in evidence:
            frame_id = item.get("witness_frame_id")
            if frame_id is None:
                continue
            if frame_id not in saved:
                raw = self.images.get(frame_id)
                if raw is None:
                    raise RuntimeError("missing native process witness")
                name = f"process_{frame_id:07d}.png"
                path = directory / name
                path.write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise RuntimeError("corrupt native process witness")
                saved[frame_id] = (name, digest)
            item["image"], item["sha256"] = saved[frame_id]
        return evidence


class Story21Detector:
    """Template and station checks calibrated against human play and old runs.

    Templates are small native crops whose exact sources are in assets/.../
    provenance.json. A card icon alone, a supplier icon, a partial progress bar,
    or an item in a chef's hands does not satisfy a preparation milestone.
    """

    def __init__(self):
        root = Path(__file__).with_name("assets") / "overcooked_story21"
        names = (
            "order_fries", "order_fried_chicken", "order_chicken_with_fries",
            "raw_chicken", "raw_potato_a", "raw_potato_b",
            "chopped_potato_board", "chopped_chicken_pieces",
            "chopped_potato_pile", "plated_fries_current",
            "fryer_chicken_icon", "fryer_potato_icon",
            "plated_chicken", "plated_fries", "plated_combo",
        )
        self.templates = {}
        self._visible_cards: set[str] = set()
        self._card_generation = {recipe: 0 for recipe in INGREDIENTS}
        for name in names:
            template = cv2.imread(str(root / f"{name}.png"), cv2.IMREAD_COLOR)
            if template is None:
                raise RuntimeError(f"missing Story 2-1 visual calibration: {name}")
            self.templates[name] = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)

    def _match(self, frame, name, rect, threshold):
        x1, y1, x2, y2 = rect
        template = self.templates[name]
        roi = frame[y1:y2, x1:x2]
        if roi.ndim == 3:
            roi = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        if roi.shape[0] < template.shape[0] or roi.shape[1] < template.shape[1]:
            return None
        result = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
        _, confidence, _, point = cv2.minMaxLoc(result)
        if confidence < threshold:
            return None
        return (x1 + point[0], y1 + point[1],
                template.shape[1], template.shape[0])

    def _cards(self, frame):
        return {recipe for recipe in INGREDIENTS
                if self._match(frame, f"order_{recipe}", (0, 60, 700, 151), .91)}

    @staticmethod
    def _recipe(ingredient, cards):
        single = "fries" if ingredient == "potato" else "fried_chicken"
        if single in cards:
            return single
        if "chicken_with_fries" in cards:
            return "chicken_with_fries"
        return None

    @staticmethod
    def _wood_below(frame, rect):
        x, y, w, h = rect
        y1, y2 = min(1199, y + h + 3), min(1200, y + h + 15)
        x1, x2 = max(0, x + w // 4), min(1920, x + 3 * w // 4)
        if y2 <= y1 or x2 <= x1:
            return False
        hsv = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
        warm = ((hsv[:, :, 0] >= 5) & (hsv[:, :, 0] <= 23)
                & (hsv[:, :, 1] >= 125) & (hsv[:, :, 2] >= 90))
        return float(warm.mean()) >= .55

    def _potato_on_upper(self, frame):
        for name in ("raw_potato_a", "raw_potato_b"):
            rect = self._match(frame, name, (500, 265, 1500, 475), .80)
            if rect is None:
                continue
            x, y, w, h = rect
            hsv = cv2.cvtColor(frame[y:y+h, x:x+w], cv2.COLOR_BGR2HSV)
            h0, s0, v0 = np.median(hsv.reshape(-1, 3), axis=0)
            if (4 <= h0 <= 22 and 80 <= s0 <= 175 and 130 <= v0 <= 220
                    and self._wood_below(frame, rect)):
                return rect
        return None

    @staticmethod
    def _pale_strips_visible(frame, rect):
        x, y, w, h = rect
        hsv = cv2.cvtColor(frame[y:y+h, x:x+w], cv2.COLOR_BGR2HSV)
        strips = ((hsv[:, :, 0] >= 10) & (hsv[:, :, 0] <= 35)
                  & (hsv[:, :, 1] >= 70) & (hsv[:, :, 1] <= 200)
                  & (hsv[:, :, 2] >= 130))
        return int(strips.sum()) >= 200

    def _ready_fryers(self, frame):
        hsv = cv2.cvtColor(frame[1000:1080, 400:1500], cv2.COLOR_BGR2HSV)
        green = cv2.inRange(hsv, (30, 80, 50), (90, 255, 255))
        count, _, stats, _ = cv2.connectedComponentsWithStats(green)
        for index in range(1, count):
            x, y, w, h, area = map(int, stats[index])
            x += 400
            y += 1000
            # Completed food displays a large green tick below its basket;
            # the in-progress bar is long and only about seven pixels high.
            if not (20 <= w <= 42 and 20 <= h <= 42 and area >= 350):
                continue
            for ingredient, template in (("chicken", "fryer_chicken_icon"),
                                         ("potato", "fryer_potato_icon")):
                icon = self._match(frame, template,
                                   (max(400, x - 30), 850,
                                    min(1500, x + 75), 975), .86)
                if icon is not None and abs(icon[0] - x) <= 32:
                    yield ingredient, (x, y, w, h)

    def detect(self, frame: np.ndarray, frame_id: int, source: str) -> list[VisualFact]:
        if frame.shape[:2] != (1200, 1920):
            raise ValueError("Story 2-1 process detector requires calibrated native frames")
        cards = self._cards(frame)
        for recipe in cards - self._visible_cards:
            self._card_generation[recipe] += 1
        self._visible_cards = cards
        if not cards:
            return []
        found = []

        def add(ingredient, stage, rect, explanation, order_type=None):
            recipe = order_type or self._recipe(ingredient, cards)
            if recipe is not None:
                found.append(VisualFact(
                    attempt_id=f"visible_{recipe}_{self._card_generation[recipe]}",
                    order_type=recipe,
                    ingredient=None if stage == "plated" else ingredient,
                    stage=stage, frame_id=frame_id, source=source,
                    rect=rect, explanation=explanation))

        chicken = self._match(frame, "raw_chicken", (500, 265, 1500, 475), .90)
        if chicken is not None and self._wood_below(frame, chicken):
            add("chicken", "upper_raft", chicken,
                "raw chicken rests on the upper wooden raft")
        potato = self._potato_on_upper(frame)
        if potato is not None:
            add("potato", "upper_raft", potato,
                "raw potato rests on the upper wooden raft")

        potato_chopped = self._match(frame, "chopped_potato_board",
                                      (400, 205, 1500, 370), .86)
        if potato_chopped is not None and self._pale_strips_visible(frame, potato_chopped):
            add("potato", "chopped", potato_chopped,
                "pale potato strips are visible on an upper chopping board")
        potato_pile = self._match(frame, "chopped_potato_pile",
                                  (350, 250, 1500, 1020), .86)
        if (potato_pile is not None
                and self._pale_strips_visible(frame, potato_pile)
                and self._wood_below(frame, potato_pile)):
            add("potato", "chopped", potato_pile,
                "pale chopped potato strips rest on the wooden work area")
        chicken_chopped = self._match(frame, "chopped_chicken_pieces",
                                       (350, 650, 1500, 1020), .94)
        if chicken_chopped is not None:
            add("chicken", "chopped", chicken_chopped,
                "pink chopped chicken pieces are visible away from the supply")

        for ingredient, rect in self._ready_fryers(frame):
            add(ingredient, "fried", rect,
                "filled fryer shows its green ready tick and matching ingredient icon")

        for ingredient, name in (("chicken", "plated_chicken"),
                                 ("potato", "plated_fries")):
            rect = self._match(frame, name, (350, 650, 1500, 1020), .91)
            if rect is None and ingredient == "potato":
                rect = self._match(frame, "plated_fries_current",
                                   (350, 650, 1500, 1020), .91)
            if rect is not None:
                add(ingredient, "plated", rect,
                    "cooked ingredient is visible on one physical clean plate")
        if "chicken_with_fries" in cards:
            rect = self._match(frame, "plated_combo", (350, 650, 1500, 1020), .95)
            if rect is not None:
                add(None, "plated", rect,
                    "fried chicken and fries share one physical plate",
                    order_type="chicken_with_fries")
        return found
