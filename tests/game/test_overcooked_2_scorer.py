"""Validate the registered Overcooked result and launch contract."""
from pathlib import Path

import numpy as np
import pytest
import yaml
import json

from coding4game_gui.benchmark.windows.scorers.overcooked_2 import Overcooked2Scorer
from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.terminal import validate_terminal_detection


def frame(tag=0):
    result = np.zeros((200, 320, 3), np.uint8)
    result[1, 1, 0] = tag
    return result


def test_current_scenario_does_not_silently_run_without_measurements(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[2]
    raw = yaml.safe_load((root / 'coding4game_gui/benchmark/scenarios/overcooked_2_2_1.yaml').read_text(encoding='utf-8'))
    assert raw['driver_game'] == 'overcooked_2'
    assert raw['runtime']['tics_per_second'] == 10
    assert raw['scorer']['three_star_threshold'] == 220
    raw['scorer']['three_star_threshold'] = None
    (tmp_path / 'overcooked_2_2_1.yaml').write_text(yaml.safe_dump(raw), encoding='utf-8')
    monkeypatch.setattr('coding4game_gui.benchmark.spec.WINDOWS_GAMES_DIR', tmp_path)
    loaded = load_game_spec('overcooked_2_2_1')
    with pytest.raises(ValueError, match='three_star_threshold'):
        validate_terminal_detection(loaded)


@pytest.mark.parametrize('missing_text', ['2-1', '继续', '订单失败 x 0', '246'])
def test_calibrated_rules_fail_closed_when_result_is_incomplete(missing_text):
    root = Path(__file__).resolve().parents[2]
    observed = json.loads((root / 'tests/fixtures/overcooked_2/result_246.ocr.json').read_text(encoding='utf-8'))
    observed = [item for item in observed if item['text'] != missing_text]
    reader = Overcooked2Scorer(load_game_spec('overcooked_2_2_1')['scorer'],
                               (1920, 1200), ocr=lambda _: observed)
    assert reader.snapshot(frame()) is None
