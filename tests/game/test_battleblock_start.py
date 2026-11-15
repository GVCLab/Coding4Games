"""Real task labels must reject the campaign hub and first-run film."""
from pathlib import Path
from types import SimpleNamespace

import cv2
import pytest
import yaml

from coding4game_gui.benchmark.windows.env import ResetController
from coding4game_gui.benchmark.windows import games_config

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / 'tests/fixtures/battleblock_start'


def controller(monkeypatch):
    monkeypatch.setattr(games_config, 'load_game_config', lambda name: SimpleNamespace(
        process='BattleBlockTheater.exe', menu_skip=[], platform='windows-kbm'))
    spec = yaml.safe_load((ROOT / 'coding4game_gui/benchmark/scenarios/battleblock.yaml').read_text(encoding='utf-8'))
    return ResetController(spec)


@pytest.mark.parametrize('name', ['wrong_hub.png', 'title.png', 'intro.png'])
def test_only_the_configured_task_can_pass_start_check(monkeypatch, name):
    assert controller(monkeypatch).start_ok(cv2.imread(str(FIXTURES / name))) is False


def test_independent_native_start_with_another_player_color_passes(monkeypatch):
    assert controller(monkeypatch).start_ok(cv2.imread(str(FIXTURES / 'ready_alt.png')))


@pytest.mark.parametrize('name', ['ready_swing.png', 'ready_swing_brown.png', 'ready_swing_wide.png'])
def test_native_start_remains_valid_while_the_number_cards_swing(monkeypatch, name):
    assert controller(monkeypatch).start_ok(cv2.imread(str(FIXTURES / name)))


def test_next_level_is_not_the_registered_start(monkeypatch):
    image = ROOT / 'tests/fixtures/battleblock_20261025/next_level.png'
    assert not controller(monkeypatch).start_ok(cv2.imread(str(image)))


@pytest.mark.parametrize('digit', ['2', '3', '7'])
@pytest.mark.parametrize('rect', [(316, 160, 60, 75), (957, 160, 53, 75)])
def test_motion_tolerance_does_not_accept_a_different_number(monkeypatch, digit, rect):
    frame = cv2.imread(str(ROOT / 'games/assets/battleblock/start_1_1.png'))
    x, y, w, h = rect
    frame[y:y+h, x:x+w] = (190, 190, 190)
    cv2.putText(frame, digit, (x+3, y+h-8), cv2.FONT_HERSHEY_SIMPLEX,
                2, (60, 65, 90), 6, cv2.LINE_AA)
    assert not controller(monkeypatch).start_ok(frame)


@pytest.mark.parametrize('rect', [(305, 145, 78, 100), (948, 145, 70, 100)])
def test_missing_act_or_scene_number_cannot_pass(monkeypatch, rect):
    frame = cv2.imread(str(ROOT / 'games/assets/battleblock/start_1_1.png'))
    x, y, w, h = rect
    frame[y:y+h, x:x+w] = (200, 200, 200)
    assert controller(monkeypatch).start_ok(frame) is False
