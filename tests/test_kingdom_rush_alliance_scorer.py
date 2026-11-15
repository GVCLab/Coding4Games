"""Kingdom Rush 5 scorer rules with a fake OCR backend."""

import re
from copy import deepcopy

import numpy as np
import pytest

from coding4game_gui.benchmark.windows.scorers.kingdom_rush_alliance import (
    KingdomRushAllianceScorer,
)


WIDTH, HEIGHT = 200, 100
LIVES_RECT = [10, 10, 40, 20]
WAVE_RECT = [10, 40, 80, 20]
SPEC = {
    "calibrated_client_size": [WIDTH, HEIGHT],
    "phases": {
        "victory": {"any": ["胜利"]},
        "defeat": {"any": ["失败", "DEFEAT"]},
        "play": {"pattern": r"\d+\s*/\s*8"},
    },
    "terminal_phases": ["victory", "defeat"],
    "regions": {
        "lives": {"rect": LIVES_RECT, "kind": "number"},
        "wave": {
            "pattern": r"(\d+)\s*/\s*8",
            "rect": WAVE_RECT,
        },
    },
    "victory_score": {
        "base": 90,
        "lives_bonus": 10,
        "initial_lives": 20,
    },
    "failure_score": {
        "base": 1,
        "wave_bonus": 75,
        "lives_bonus": 13,
        "max_score": 89,
        "max_wave": 8,
    },
}


def _item(text, box=(120, 10, 40, 15), conf=0.99):
    return {"text": text, "conf": conf, "box": list(box)}


def _play(lives, wave=0, *, lives_box=(15, 12, 20, 12)):
    return [
        _item(str(lives), box=lives_box),
        _item(f"{wave}/8", box=(15, 42, 30, 12)),
    ]


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
    return KingdomRushAllianceScorer(
        SPEC, client_size=(WIDTH, HEIGHT), ocr=FakeOcr(by_tag))


def test_calibrated_lives_recognition_overrides_misread_full_frame_and_caches_pixels():
    spec = deepcopy(SPEC)
    spec["regions"]["lives"].update(
        recognition_rect=LIVES_RECT, trim_light_text=True)

    class RegionOcr(FakeOcr):
        def __init__(self):
            super().__init__({1: _play(5, 4)})
            self.recognized = 8
            self.calls = []

        def recognize(self, crop, *, trim_light_text):
            self.calls.append((crop.shape, trim_light_text))
            return [_item(str(self.recognized), box=(2, 2, 10, 10))]

    ocr = RegionOcr()
    scorer = KingdomRushAllianceScorer(spec, (WIDTH, HEIGHT), ocr=ocr)
    image = _frame(1)
    assert scorer.read_region(image, "lives") == 8
    assert scorer.delta(image) == 0
    assert scorer.last_lives == 8
    assert ocr.calls == [((20, 40, 3), True)]
    scorer.classify_phase(image.copy())
    assert len(ocr.calls) == 1
    # This digit pixel is skipped by the old 16-pixel full-frame cache sample.
    changed = image.copy()
    changed[15, 15] = 255
    ocr.recognized = 7
    scorer.delta(changed)
    assert scorer.last_lives == 7
    assert len(ocr.calls) == 2
    scorer.reset()
    scorer.delta(changed)
    assert len(ocr.calls) == 3


def test_missing_regional_lives_does_not_fall_back_to_wrong_full_frame():
    spec = deepcopy(SPEC)
    spec["regions"]["lives"]["recognition_rect"] = LIVES_RECT
    ocr = FakeOcr({1: _play(5), 2: [_item("胜利")]})
    ocr.recognize = lambda *_a, **_k: []
    scorer = KingdomRushAllianceScorer(spec, (WIDTH, HEIGHT), ocr=ocr)
    scorer.delta(_frame(1))
    assert scorer.last_lives is None
    assert scorer.delta(_frame(2)) == 0


@pytest.mark.parametrize(("lives", "expected"), [
    (20, 100.0),
    (10, 95.0),
    (1, 90.5),
])
def test_victory_scores_once_from_last_trustworthy_lives(lives, expected):
    scorer = _scorer({1: _play(lives), 2: [_item("胜利")]})

    assert scorer.delta(_frame(1)) == 0.0
    assert scorer.total == 0.0
    victory = _frame(2)
    assert scorer.delta(victory) == expected
    assert scorer.delta(victory) == 0.0
    assert scorer.total == expected


def test_defeat_scores_wave_and_lives_progress_once():
    scorer = _scorer({
        1: _play(20, 0),
        2: _play(19, 4),
        3: _play(18, 8),
        4: [_item("失败")],
    })

    for tag in (1, 2, 3):
        assert scorer.delta(_frame(tag)) == 0.0
    defeat_score = scorer.delta(_frame(4))
    assert defeat_score == pytest.approx(87.7)
    assert scorer.total == pytest.approx(87.7)
    assert scorer.delta(_frame(4)) == 0.0
    assert scorer.is_terminal(_frame(4))
    assert scorer.classify_phase(_frame(4)) == "defeat"


def test_finalize_scores_incomplete_play_without_terminal_screen():
    scorer = _scorer({1: _play(20, 0), 2: _play(10, 4)})

    assert scorer.delta(_frame(1)) == 0.0
    assert scorer.delta(_frame(2)) == 0.0
    assert scorer.finalize() == pytest.approx(41.75)
    assert scorer.total == pytest.approx(41.75)
    assert scorer.finalize() == 0.0


def test_failure_without_trustworthy_progress_gets_base_score():
    scorer = _scorer({1: [_item("失败")]})

    assert scorer.delta(_frame(1)) == 1.0
    assert scorer.total == 1.0


def test_victory_without_a_trustworthy_lives_read_pays_zero():
    scorer = _scorer({1: [_item("胜利")], 2: [_item("胜利")]})

    assert scorer.delta(_frame(1)) == 0.0
    assert scorer.delta(_frame(2)) == 0.0
    assert scorer.total == 0.0


def test_terminal_and_phase_checks_before_delta_preserve_victory_payout():
    scorer = _scorer({1: _play(10), 2: [_item("胜利")]})

    play = _frame(1)
    assert scorer.classify_phase(play) == "play"
    assert not scorer.is_terminal(play)
    assert scorer.last_lives == 10

    victory = _frame(2)
    assert scorer.is_terminal(victory)
    assert scorer.classify_phase(victory) == "victory"
    assert scorer.delta(victory) == 95.0
    assert scorer.total == 95.0


def test_lives_may_decrease_but_never_increase_or_leave_valid_range():
    scorer = _scorer({
        1: _play(20),
        2: _play(10),
        3: _play(15),
        4: _play(21),
        5: [_item("胜利")],
    })

    for tag in (1, 2, 3, 4):
        assert scorer.delta(_frame(tag)) == 0.0
    assert scorer.last_lives == 10
    assert scorer.delta(_frame(5)) == 95.0


def test_lives_must_be_read_inside_the_calibrated_roi():
    scorer = _scorer({
        1: _play(20, lives_box=(100, 12, 20, 12)),
        2: [_item("胜利")],
    })

    assert scorer.delta(_frame(1)) == 0.0
    assert scorer.last_lives is None
    assert scorer.delta(_frame(2)) == 0.0


def test_reset_clears_lives_total_and_victory_paid_state():
    scorer = _scorer({
        1: _play(20),
        2: [_item("胜利")],
        3: _play(10),
        4: [_item("胜利")],
    })
    scorer.delta(_frame(1))
    assert scorer.delta(_frame(2)) == 100.0

    scorer.reset()
    assert scorer.total == 0.0
    assert scorer.last_lives is None
    assert scorer.max_wave == 0
    scorer.delta(_frame(3))
    assert scorer.delta(_frame(4)) == 95.0


def test_inherited_wave_region_reads_numerator_without_scoring_it():
    scorer = _scorer({1: _play(20, 0), 2: _play(20, 8)})

    assert scorer.read_region(_frame(1), "wave") == 0
    assert scorer.read_region(_frame(2), "wave") == 8
    assert scorer.total == 0.0


def test_wave_progress_is_monotonic_and_bounded():
    scorer = _scorer({
        1: _play(20, 0),
        2: _play(20, 4),
        3: _play(20, 2),
        4: _play(20, 9),
        5: [_item("失败")],
    })
    for tag in (1, 2, 3, 4):
        scorer.delta(_frame(tag))
    assert scorer.max_wave == 4
    assert scorer.delta(_frame(5)) == pytest.approx(45.0)


def test_measured_start_pattern_accepts_merged_lives_and_gold_text():
    scorer = _scorer({1: [_item("20400", box=(10, 5, 70, 20))]})
    pattern = r"20\s*400"

    assert re.search(pattern, "20400")
    assert scorer.check_text(_frame(1), pattern, rect=[0, 0, 100, 40])


def test_measured_start_pattern_accepts_adjacent_ocr_boxes_inside_roi():
    scorer = _scorer({
        1: [
            _item("20", box=(10, 5, 20, 20)),
            _item("400", box=(35, 5, 35, 20)),
            _item("0/8", box=(10, 45, 30, 15)),
        ],
    })

    assert scorer.check_text(
        _frame(1), r"20\s*400", rect=[0, 0, 100, 40]
    )


@pytest.mark.parametrize("gold_box", [(110, 5, 30, 15), (35, 28, 30, 10)])
def test_start_anchor_does_not_join_outside_region_or_across_rows(gold_box):
    scorer = _scorer({1: [
        _item("20", box=(10, 5, 20, 15)),
        _item("400", box=gold_box),
    ]})
    assert not scorer.check_text(_frame(1), r"20\s*400", rect=[0, 0, 100, 40])
