"""Palworld benchmark config tests: no game, virtual device or model calls."""
from pathlib import Path
import yaml
import pytest
from coding4game_gui.benchmark.windows.games_config import load_game_config

ROOT = Path(__file__).resolve().parents[2]

def test_palworld_capture_enters_before_policy_handoff():
    cfg = load_game_config('palworld_capture', validate_files=False)
    assert cfg.save_state.golden.name == 'palworld_capture_entrance_singleplayer_20260922.zip'
    assert len(cfg.menu_skip) == 8
    assert cfg.menu_skip[4].spec.buttons == ['WEST']
    assert cfg.menu_skip[4].spec.expect_text == '要进入地下城'
    # Current-host world loading was observed for 65 seconds before the
    # entrance interaction; see the 2026-10-22 startup calibration evidence.
    assert cfg.menu_skip[3].spec.duration_seconds == pytest.approx(65.0)
    assert cfg.menu_skip[3].spec.expect_text == '进入地下城'
    assert cfg.menu_skip[3].min_scene_luma == 30.0
    assert cfg.menu_skip[-2].spec.click == (1107, 953)
    assert cfg.menu_skip[-2].spec.buttons == []
    assert cfg.menu_skip[-1].spec.duration_seconds == pytest.approx(7.0)
    assert cfg.menu_skip[-1].spec.expect_text == '企丸王'

    from coding4game_gui.benchmark.spec import load_game_spec
    spec = load_game_spec('palworld_capture_penking')
    assert spec['startup']['golden_checkpoint'] == 'entrance_singleplayer_20260922'
    from coding4game_gui.benchmark.windows.save_state import resolve_for_run
    if cfg.save_state.golden.is_file():
        resolved = resolve_for_run(spec['driver_game'], cfg.save_state, spec['startup']['golden_checkpoint'])
        assert resolved.golden.name == cfg.save_state.golden.name
    else:
        with pytest.raises(FileNotFoundError, match='checkpoint not found'):
            resolve_for_run(spec['driver_game'], cfg.save_state, spec['startup']['golden_checkpoint'])
    assert not spec['startup'].get('menu_skip_open_loop_tail')
    assert spec['startup']['menu_skip_ocr']['device'] == 'gpu:0'
    assert spec['startup']['menu_skip_ocr']['input_size'] == [1920, 1080]
    assert [step.spec.click for step in cfg.menu_skip[:3]] == [(1277, 825), (1277, 338), (1944, 1308)]
    assert cfg.menu_skip[0].spec.expect_text == r'WORLD\s*SELECT'
    assert spec['startup']['start_check'] == {
        'timeout_seconds': 0,
        'mean_luma': {'rect': [200, 75, 1500, 525], 'max': 100},
        'hsv_fraction': {
            'rect': [1900, 200, 500, 600],
            'ranges': [
                {'lower': [8, 35, 60], 'upper': [30, 160, 255]},
            ],
            'min': 0.12,
        },
    }
    assert 'both required blue penguin Pals' in spec['gameplay']
    assert 'large, boss-sized target' in spec['gameplay']
    assert 'much smaller' in spec['gameplay']
    gameplay = ' '.join(spec['gameplay'].split())
    assert 'passive controller hints, not an open radial menu' in gameplay
    assert spec['runtime']['max_tics'] == 1200
    assert spec['scorer']['capture_progress']['max_tics'] == 1200
    assert 'do not rely on blue color alone' not in gameplay
    assert 'rather than assuming the other target is absent' not in gameplay
    assert '1200-tic episode limit' in spec['objective']
    assert 'exactly 20 tics of `neutral`' not in gameplay
    assert 'two seconds' not in gameplay


def test_palworld_capture_start_check_rejects_exterior_first_frame():
    from coding4game_gui.benchmark.spec import load_game_spec
    from coding4game_gui.benchmark.windows.env import ResetController
    import numpy as np

    controller = object.__new__(ResetController)
    controller.spec = load_game_spec('palworld_capture_penking')
    controller._start_check = controller.spec['startup']['start_check']
    controller._scorer = None

    exterior = np.full((1440, 2560, 3), 162, dtype=np.uint8)
    dungeon = np.full((1440, 2560, 3), 61, dtype=np.uint8)
    # The warm stone wall identifies the room without a contextual boss HUD.
    dungeon[200:800, 1900:2400] = [70, 100, 140]
    assert controller.start_ok(exterior) is False
    assert controller.start_ok(dungeon) is True


def test_palworld_capture_start_check_rejects_dark_non_dungeon_frame():
    from coding4game_gui.benchmark.spec import load_game_spec
    from coding4game_gui.benchmark.windows.env import ResetController
    import numpy as np

    controller = object.__new__(ResetController)
    controller.spec = load_game_spec('palworld_capture_penking')
    controller._start_check = controller.spec['startup']['start_check']
    controller._scorer = None

    dark_exterior = np.full((1440, 2560, 3), 45, dtype=np.uint8)
    assert controller.start_ok(dark_exterior) is False

def test_palworld_capture_penalizes_only_extra_consumed_spheres():
    from coding4game_gui.benchmark.spec import load_game_spec
    spec = load_game_spec('palworld_capture_penking')
    scoring = spec['scorer']['capture_progress']
    assert scoring['waste_penalty_per_item'] == 2
    assert scoring['max_waste_penalty'] == 20
    verification = spec['lifecycle']['save_verification']
    assert verification['consumable_item_id'] == 'PalSphere_Ancient_1'
    assert set(verification['candidate_actions']) == {'RB', 'KEY[Q]'}
    assert 'deducts 2' in spec['objective']
    assert 'successful required captures are not penalized' in spec['objective']
    assert 'capped at 20 points' in spec['objective']

def test_palworld_only_declares_raw_physical_controls():
    data = yaml.safe_load((ROOT / 'games/palworld_capture.yaml').read_text(encoding='utf-8'))
    expected = {'neutral','a','b','x','y','lb','rb','ls','rs','dpad_up','dpad_down',
                'dpad_left','dpad_right','start','back','guide'}
    assert set(data['actions']) == expected
    for value in data['actions'].values():
        assert set(value) <= {'description','buttons'}
    assert not data.get('use_speedhack', False)


def test_menu_pad_press_does_not_switch_back_to_mouse(monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from PIL import Image
    from coding4game_gui.benchmark.windows import env as module
    from coding4game_gui.benchmark.windows import games_config
    monkeypatch.setattr(games_config, 'load_game_config', lambda name: load_game_config(name, validate_files=False))
    controller = module.ResetController({'driver_game': 'palworld_capture',
        'startup': {'menu_skip_park_cursor': True, 'focus_alt_key': False}})
    order = []
    monkeypatch.setattr(controller, '_force_foreground', lambda env: True)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    def apply(*args, **kwargs):
        order.append('press' if kwargs['buttons'] else 'release')
    fake = SimpleNamespace(render=lambda: Image.new('RGB', (100, 100),
        'white' if 'press' in order else 'black'), apply_state=apply)
    step = next(step for step in controller.cfg.menu_skip if step.spec.buttons)
    step = replace(step, spec=replace(step.spec, expect_text=None))
    controller._press_until_screen_changes(fake, step, retries=1)
    assert order == ['press', 'release']
