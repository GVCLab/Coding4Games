"""NBA 2K25 task, startup, and point HUD scoring."""
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
import yaml

from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.games_config import load_game_config
from coding4game_gui.benchmark.windows.scorers.nba_2k25 import Nba2k25Scorer
from coding4game_gui.benchmark.windows.scorers.ocr import ScorerLost
from coding4game_gui.benchmark.windows.terminal import validate_terminal_detection


ROOT = Path(__file__).parents[2]


def configuration():
    spec = yaml.safe_load((ROOT / 'coding4game_gui/benchmark/scenarios/nba_2k25.yaml').read_bytes())
    return deepcopy(spec['scorer'])


def test_recovered_fixed_duration_point_contract():
    spec = load_game_spec('nba_2k25', windows_input='gamepad')
    assert spec['runtime']['tics_per_second'] == 10
    assert spec['runtime']['max_tics'] == 1000
    assert spec['scorer']['weights'] == dict(points_for=15, points_against=-5)
    assert spec['scorer']['settlement'] == 'final_frame'
    assert spec['scorer']['terminal_phases'] == []
    assert spec['lifecycle']['terminal_detection'] is False
    validate_terminal_detection(spec)
    assert 'Tyrese Maxey' in spec['gameplay']
    assert 'Paul George' not in spec['gameplay']
    assert 'You are directly PLAYING' not in spec['gameplay']


def test_direct_startup_keeps_recorded_gamepad_body_and_no_speedhack():
    cfg = load_game_config('nba_2k25', validate_files=False)
    assert Path(cfg.auto_launch.launch_path).name == 'NBA2K25.exe'
    assert cfg.auto_launch.settle_seconds == 60
    assert cfg.use_speedhack is False
    data = yaml.safe_load((ROOT / 'games/nba_2k25.yaml').read_text(encoding='utf-8'))
    startup = load_game_spec('nba_2k25')['startup']
    assert startup['menu_skip_replay'] == 'closed_loop'
    assert startup['menu_skip_open_loop_tail'] == 3
    assert len(data['menu_skip']) == 38
    assert data['menu_skip'][11]['expect'] == '进入快速比赛'
    assert any('KEY[2]' in step.get('buttons', []) for step in data['menu_skip'][:7])
    assert all(not token.startswith('KEY[')
               for step in data['menu_skip'][7:] for token in step.get('buttons', []))
    assert data['menu_skip'][-1]['buttons'] == ['START']


def items(own=0, opponent=0):
    return [
        dict(text='对方', conf=.99, box=[45, 1135, 72, 39]),
        dict(text=str(opponent).zfill(2), conf=.99, box=[243, 1132, 58, 42]),
        dict(text=str(own).zfill(2), conf=.99, box=[379, 1135, 54, 37]),
        dict(text='本方40', conf=.99, box=[556, 1131, 166, 45]),
    ]


def scorer_for(readings):
    spec = configuration()
    rect = spec.get('ocr', {}).get('rect') or [0, 0, 1920, 1200]
    def ocr(frame):
        return [dict(it, box=[it['box'][0] - rect[0], it['box'][1] - rect[1],
                             *it['box'][2:]]) for it in readings(frame)]
    return Nba2k25Scorer(spec, client_size=(1920, 1200), ocr=ocr)


@pytest.mark.parametrize('own,opponent,expected', [(0,0,0), (1,2,5), (2,1,25), (0,3,-15)])
def test_historical_weights_and_field_direction(own, opponent, expected):
    scorer = scorer_for(lambda _frame: items(own, opponent))
    reading = scorer.snapshot(np.zeros((1200,1920,3), dtype=np.uint8))
    assert reading['score'] == expected
    assert reading['readings'] == dict(points_for=own, points_against=opponent)


@pytest.mark.parametrize('remove', [0,1,2,3])
def test_missing_label_or_point_counter_is_not_zero_score(remove):
    readings = items(2,1)
    readings.pop(remove)
    scorer = scorer_for(lambda _frame: readings)
    assert scorer.snapshot(np.zeros((1200,1920,3), dtype=np.uint8)) is None


def test_full_frame_identity_sees_a_digit_change_outside_sparse_sample():
    calls = []
    def read(frame):
        calls.append(True)
        return items(int(frame[1,1,0]), 0)
    scorer = scorer_for(read)
    frame = np.zeros((1200,1920,3), dtype=np.uint8)
    assert scorer.snapshot(frame)['score'] == 0
    rect = scorer.spec.get('ocr', {}).get('rect') or [0,0,1920,1200]
    frame[rect[1] + 1,rect[0] + 1,0] = 1
    assert scorer.snapshot(frame)['score'] == 15
    assert len(calls) == 2


def test_episode_begins_only_from_readable_zero_zero():
    scorer = scorer_for(lambda _frame: items(1,0))
    with pytest.raises(ScorerLost, match='initial 0-0'):
        scorer.begin_episode(np.zeros((1200,1920,3), dtype=np.uint8))


def test_targeted_hud_lines_keep_both_point_counters_separate_from_clock():
    spec = configuration()
    frame = np.zeros((1200,1920,3), dtype=np.uint8)
    rules = list(spec['hud_labels'].values()) + list(spec['regions'].values())
    for tag, rule in enumerate(rules, 1):
        x, y, _w, _h = rule['rect']
        frame[y,x,0] = tag
    calls = []
    class Engine:
        def __call__(self, _frame):
            raise AssertionError('targeted scoring must not merge full-HUD counters')
        def recognize(self, crop, *, trim_light_text):
            calls.append(crop.shape[:2])
            text = {1:'对方',2:'本方',3:'04',4:'05'}[int(crop[0,0,0])]
            box = [14,13,49,33] if int(crop[0,0,0]) >= 3 else [0,0,crop.shape[1],crop.shape[0]]
            return [dict(text=text, conf=.99, box=box)]
    scorer = Nba2k25Scorer(spec, (1920,1200), ocr=Engine())
    assert scorer.snapshot(frame)['score'] == 35
    assert calls == [(55,100),(55,110),(55,85),(55,85)]


@pytest.mark.parametrize('text,box', [
    ('0', [63,16,22,28]),
    ('03', [24,13,58,39]),
    ('03', [35,13,48,33]),
    ('03', [2,13,49,33]),
    ('4O', [14,13,49,33]),
])
def test_scoring_animation_fragments_are_missing_evidence(text, box):
    # The first three boxes reproduce OCR from human frames 285 and 348.
    spec = configuration()
    frame = np.zeros((1200,1920,3), dtype=np.uint8)
    rules = list(spec['hud_labels'].values()) + list(spec['regions'].values())
    for tag, rule in enumerate(rules, 1):
        x, y, _w, _h = rule['rect']
        frame[y,x,0] = tag

    class Engine:
        def recognize(self, crop, *, trim_light_text):
            tag = int(crop[0,0,0])
            value = {1:'对方',2:'本方',3:text,4:'05'}[tag]
            bounds = box if tag == 3 else [14,13,49,33]
            return [dict(text=value, conf=.99, box=bounds)]

    scorer = Nba2k25Scorer(spec, (1920,1200), ocr=Engine())
    assert scorer.snapshot(frame) is None
