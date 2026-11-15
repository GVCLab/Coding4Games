"""Current native human frames must agree with their three-gem results oracle."""
from pathlib import Path

import cv2
import pytest

from coding4game_gui.benchmark.windows.scorers import bbt_hud
from coding4game_gui.benchmark.windows.scorers.bbt_progress import BbtProgressScorer

FIX = Path(__file__).parents[1] / "fixtures/battleblock_20261025"


@pytest.mark.parametrize("name,expected", [
    ("cloud_not_yarn.png", (1, 0)),
    ("results_a.png", None),
])
def test_native_counts_match_human_labels_without_false_yarn(name, expected):
    assert bbt_hud.read(cv2.imread(str(FIX / name))) == expected


def test_native_recorded_milestones_finish_once_without_restart_or_yarn_bonus():
    scorer = BbtProgressScorer(client_size=(1280, 720))
    for name in ["gem0.png", "gem1.png", "cloud_not_yarn.png", "gem2.png", "gem3_exitopen.png"]:
        frame = cv2.imread(str(FIX / name))
        assert not scorer.is_terminal(frame)
        for _ in range(5):
            scorer.note_tics(10)
            scorer.delta(frame)
    assert scorer.gems_max == 3
    assert scorer.yarn_max_seen == 0
    assert scorer.ledger()["tier"] == 2
    curtain = cv2.imread(str(FIX / "curtain.png"))
    assert not scorer.is_terminal(curtain)
    scorer.delta(curtain)
    results = cv2.imread(str(FIX / "results_a.png"))
    assert scorer.is_terminal(results)
    assert scorer.delta(results) > 0
    assert scorer.completed and not scorer.restarted
    paid = scorer.total
    assert scorer.delta(results) == 0
    assert scorer.delta(cv2.imread(str(FIX / "next_level.png"))) == 0
    assert scorer.total == paid
