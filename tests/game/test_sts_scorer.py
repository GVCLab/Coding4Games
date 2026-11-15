"""Slay the Spire floor scorer contract.

Perception is faked here; the production PaddleOCR readings are verified by
the recorded-session replay. These tests pin the judgment rule that the total
score equals the highest sequential floor reached.
"""
from __future__ import annotations

import copy

import numpy as np

from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer


WIDTH, HEIGHT = 1280, 960


def _item(text, box, conf=0.99):
    return {"text": text, "conf": conf, "box": list(box)}


RUN = [
    _item("the Ironclad", (70, 14, 77, 14)),
    _item("80/80", (197, 12, 58, 18)),
]
FLOOR_2 = _item("2", (614, 13, 13, 16))
FLOOR_3 = _item("3", (616, 14, 10, 15))


class FakeOcr:
    def __init__(self, by_tag):
        self.by_tag = by_tag

    def __call__(self, frame):
        return self.by_tag[int(frame[0, 0, 0])]


def _frame(tag):
    frame = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    frame[0, 0, 0] = tag
    return frame


def _scorer(by_tag):
    spec = copy.deepcopy(load_game_spec("slay_the_spire")["scorer"])
    return OcrScorer(spec, client_size=(WIDTH, HEIGHT), ocr=FakeOcr(by_tag))


def test_total_score_equals_current_floor_when_floor_one_is_not_ocr_readable():
    scorer = _scorer({
        1: RUN,
        2: RUN + [FLOOR_2],
        3: RUN + [FLOOR_3],
    })

    assert scorer.delta(_frame(1)) == 1
    assert scorer.total == 1
    assert scorer.delta(_frame(2)) == 1
    assert scorer.total == 2
    assert scorer.delta(_frame(3)) == 1
    assert scorer.total == 3


def test_repeated_floor_does_not_pay_twice():
    scorer = _scorer({1: RUN, 2: RUN + [FLOOR_2]})

    assert scorer.delta(_frame(1)) == 1
    assert scorer.delta(_frame(2)) == 1
    repeated = _frame(2)
    repeated[0, 1, 0] = 1  # defeat the scorer's sampled-frame memo key
    assert scorer.delta(repeated) == 0
    assert scorer.total == 2


def test_nonsequential_ocr_jump_is_rejected_even_when_repeated():
    bogus = _item("13", (612, 12, 18, 18))
    scorer = _scorer({
        1: RUN,
        2: RUN + [FLOOR_2],
        3: RUN + [bogus],
        4: RUN + [FLOOR_3],
    })

    assert scorer.delta(_frame(1)) == 1
    assert scorer.delta(_frame(2)) == 1
    assert scorer.delta(_frame(3)) == 0
    repeated = _frame(3)
    repeated[0, 1, 0] = 1
    assert scorer.delta(repeated) == 0
    assert scorer.delta(_frame(4)) == 1
    assert scorer.total == 3


def test_number_outside_floor_rect_cannot_change_score():
    damage = _item("11", (936, 526, 22, 20))
    scorer = _scorer({1: RUN, 2: RUN + [damage]})

    assert scorer.delta(_frame(1)) == 1
    assert scorer.delta(_frame(2)) == 0
    assert scorer.total == 1


def test_gameover_has_priority_over_the_persistent_run_hud():
    scorer = _scorer({
        1: RUN + [_item("YOU DIED", (500, 300, 160, 40))],
    })

    frame = _frame(1)
    assert scorer.classify_phase(frame) == "gameover"
    assert scorer.is_terminal(frame)
    assert scorer.delta(frame) == 0


def test_scenario_declares_only_floor_as_a_weighted_numeric_region():
    scorer = load_game_spec("slay_the_spire")["scorer"]

    assert "calibration_status" not in scorer
    assert scorer["score_phases"] == ["run"]
    assert scorer["phase_rewards"] == {"run": 1}
    assert scorer["weights"] == {"floor": 1}
    assert scorer["regions"]["floor"] == {
        "rect": [590, 0, 60, 45],
        "kind": "number",
        "max_increment": 1,
        "baseline": 1,
    }
    assert "player_hp" not in scorer["weights"]
