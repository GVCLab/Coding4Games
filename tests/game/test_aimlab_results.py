"""Guard result semantics observed in the 2026-10-21 human Gridshot recording."""
from pathlib import Path

import numpy as np
import pytest
import yaml

from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer
from coding4game_gui.benchmark.windows.games_config import load_game_config


def item(text, box):
    return {"text": text, "conf": 0.99, "box": list(box)}


TITLE = item("Gridshot 3x3", (160, 35, 126, 20))
KILL_LABEL = item("Targets Killed", (1405, 277, 106, 17))
HIT_LABEL = item("Hits/Shots", (1217, 277, 82, 17))


def test_loaded_menu_search_guard_accepts_human_ocr_zero_as_letter_o():
    cfg = load_game_config("aimlab", validate_files=False)
    pattern = cfg.menu_skip[0].spec.expect_text
    assert pattern
    for text, expected in [("Search results: O", True),
                           ("Search results: 18", True),
                           ("Play Last task Gridshot 3x3", False)]:
        scorer = OcrScorer({"phases": {}, "regions": {}}, (200, 100),
                           ocr=lambda _, text=text: [item(text, (10, 10, 170, 20))])
        assert scorer.check_text(np.zeros((100, 200, 3), dtype=np.uint8), pattern) is expected


@pytest.mark.parametrize("row,expected", [
    ("Search results: 18 GRIDSHOT 3X3 whoisaqua", True),
    ("Search results: 1 GG GRIDSHOT 3X3 aimlabofficialtasks", False),
    ("Search results: 2 GG GRIDSHOT 3X3 aimlabofficialtasks GRIDSHOT 3X3 whoisaqua", False),
    ("Search results: 1 GRIDSHOT 3X3 someoneelse", False),
])
def test_menu_wait_requires_the_recorded_task_in_the_first_result(row, expected):
    cfg = load_game_config("aimlab", validate_files=False)
    pattern = cfg.menu_skip[3].spec.expect_text
    assert pattern
    scorer = OcrScorer({"phases": {}, "regions": {}}, (200, 100),
                       ocr=lambda _: [item(row, (10, 10, 170, 20))])
    assert scorer.check_text(np.zeros((100, 200, 3), dtype=np.uint8), pattern) is expected


def observe(items):
    source = Path(__file__).resolve().parents[2] / (
        "coding4game_gui/benchmark/scenarios/aimlab.yaml")
    spec = yaml.safe_load(source.read_text(encoding="utf-8"))["scorer"]
    x, y, _, _ = spec["ocr"]["rect"]
    local_items = [{**it, "box": [it["box"][0] - x, it["box"][1] - y,
                                 *it["box"][2:]]} for it in items]
    scorer = OcrScorer(spec, (2048, 1152), ocr=lambda _: local_items)
    return scorer, np.zeros((1152, 2048, 3), dtype=np.uint8)


def test_human_kills_are_final_while_large_score_still_animates_and_graph_is_empty():
    # frames/000264.png: the large score is still 0; the task's final count is 8.
    scorer, frame = observe([TITLE, item("0", (511, 107, 41, 47)),
                             item("8", (1404, 259, 16, 19)), KILL_LABEL])
    assert scorer.is_terminal(frame)
    assert scorer.snapshot(frame) == {
        "score": 8.0, "phase": "results", "readings": {"killed_targets": 8}}


@pytest.mark.parametrize("kills,hits,expected", [("0", "0/30", 0),
                                                   (None, "8/8", 8),
                                                   (None, "0/0", 0)])
def test_labeled_zero_and_hit_counter_fallback(kills, hits, expected):
    items = [TITLE, KILL_LABEL, HIT_LABEL, item(hits, (1215, 256, 50, 23))]
    if kills is not None:
        items.append(item(kills, (1404, 259, 16, 19)))
    scorer, frame = observe(items)
    assert scorer.is_terminal(frame)
    assert scorer.snapshot(frame)["score"] == expected


@pytest.mark.parametrize("items", [
    [TITLE, item("8", (512, 109, 33, 44))],  # Large score alone is insufficient.
    [TITLE, item("8", (1404, 259, 16, 19))],  # Unlabeled overlay number.
    [TITLE, KILL_LABEL, item("--", (1404, 259, 16, 19))],
    [TITLE, KILL_LABEL, item("8/8", (1215, 256, 37, 23))],  # Missing fallback label.
    [KILL_LABEL, item("8", (1404, 259, 16, 19))],  # Missing task identity.
    [item("POINTS", (639, 45, 63, 18)), item("8", (638, 66, 25, 29)),
     item("00:00", (914, 67, 95, 25))],  # Human frame 000234 is still gameplay.
    [],  # Black loading transition.
])
def test_incomplete_results_and_non_results_have_no_score(items):
    scorer, frame = observe(items)
    assert not scorer.is_terminal(frame)
    assert scorer.snapshot(frame) is None
