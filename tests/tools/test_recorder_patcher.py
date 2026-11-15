from pathlib import Path
import shutil

import pytest
import yaml

from coding4game_gui.benchmark.windows.games_config import load_game_config
from coding4game_gui.benchmark.windows.menu_recorder import Step, YamlPatcher


SOURCE = Path(__file__).resolve().parents[2] / 'games' / 'brotato.yaml'


@pytest.fixture
def driver_file(tmp_path):
    return Path(shutil.copy2(SOURCE, tmp_path / SOURCE.name))


def test_recorded_menu_reloads_without_changing_other_driver_fields(driver_file):
    before = yaml.safe_load(driver_file.read_text(encoding='utf-8'))
    steps = [
        Step(pad=1, buttons=['A'], left_stick=(0.7, 0.35), left_trigger=1.0,
             duration_seconds=0.2, description='Recorded input'),
        Step(pad=2, duration_seconds=0.8, description='Recorded wait'),
        Step(pad=2, buttons=['B'], duration_seconds=0.2, description='Second pad'),
    ]
    patcher = YamlPatcher(driver_file)
    patcher.patch([Step(pad=1, buttons=['Y'], duration_seconds=0.2)])
    assert patcher.patch(steps)['steps_written'] == len(steps)
    parsed = load_game_config(str(driver_file)).menu_skip
    assert [(s.pad, s.spec.buttons, s.spec.duration_seconds) for s in parsed] == [
        (0, ['SOUTH'], 0.2), (1, [], 0.8), (1, ['EAST'], 0.2),
    ]
    assert parsed[0].spec.left_stick == (0.7, 0.35)
    assert (parsed[0].spec.left_trigger, parsed[0].spec.right_trigger) == (1.0, 0.0)
    after = yaml.safe_load(driver_file.read_text(encoding='utf-8'))
    del before['menu_skip'], after['menu_skip']
    assert after == before
    assert list(driver_file.parent.iterdir()) == [driver_file]


@pytest.mark.parametrize('invalid,field', [
    ({'pad': 0}, 'pad'),
    ({'buttons': ['UNKNOWN']}, 'buttons'),
    ({'duration_seconds': float('nan')}, 'duration_seconds'),
])
def test_invalid_recording_preserves_driver_file(driver_file, invalid, field):
    original = driver_file.read_bytes()
    step = Step(**({'pad': 1, 'duration_seconds': 0.2} | invalid))
    with pytest.raises(ValueError, match=field):
        YamlPatcher(driver_file).patch([step])
    assert driver_file.read_bytes() == original


def test_overwriting_recording_reports_removed_speed_overrides(driver_file):
    before = yaml.safe_load(driver_file.read_text(encoding='utf-8'))
    before['menu_skip'] = [{'buttons': ['A'], 'duration_seconds': 0.2, 'speed': 4.0}]
    driver_file.write_text(yaml.safe_dump(before), encoding='utf-8')
    result = YamlPatcher(driver_file).patch([Step(pad=1, buttons=['A'], duration_seconds=0.2)])
    assert result == {'speed_entries_dropped': 1, 'steps_written': 1}
