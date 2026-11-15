"""Exercise Windows scenario contracts without private saves or game processes."""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.deferred_score import DeferredScore
from coding4game_gui.benchmark.windows.games_config import load_game_config, parse_game_config
from coding4game_gui.benchmark.windows.save_state import resolve_for_run, restore
from coding4game_gui.benchmark.windows.terminal import validate_terminal_detection


ROOT = Path(__file__).resolve().parents[2]


def test_missing_save_is_rejected_by_the_operational_parser(tmp_path):
    data = dict(name="test", process="test.exe", actions={"neutral": {}},
                save_state=dict(restore_to=str(tmp_path / "save"),
                                golden=str(tmp_path / "missing.zip")))
    assert parse_game_config(data, validate_files=False).save_state is not None
    with pytest.raises(FileNotFoundError):
        parse_game_config(data)
    assert not (tmp_path / "save").exists()


def test_account_bound_save_requires_a_local_destination_before_restore(tmp_path):
    path = tmp_path / "games" / "example.yaml"
    path.parent.mkdir()
    path.write_text(
        "name: Example\nprocess: example.exe\nactions: {neutral: {}}\n"
        "save_state: {restore_to: null}\n",
        encoding="utf-8",
    )
    unresolved = load_game_config(str(path), validate_files=False).save_state
    assert unresolved.restore_to is None
    with pytest.raises(ValueError, match=r"games\.local\.json"):
        load_game_config(str(path))
    with pytest.raises(ValueError, match=r"games\.local\.json"):
        resolve_for_run("example", unresolved, "golden")
    with pytest.raises(ValueError, match=r"games\.local\.json"):
        restore(unresolved, "example.exe")

    destination = tmp_path / "approved-account"
    (tmp_path / "games.local.json").write_text(json.dumps({
        "example": {"save_state": {"restore_to": str(destination)}},
    }), encoding="utf-8")
    assert load_game_config(str(path)).save_state.restore_to == destination


def test_keyboard_only_task_rejects_gamepad_before_startup(monkeypatch):
    from coding4game_gui.benchmark.windows import env as module

    controller = SimpleNamespace(
        cfg=SimpleNamespace(platform='windows-kbm'),
        ensure_env=lambda: pytest.fail('incompatible input mode started a game'))
    monkeypatch.setattr(module, 'ResetController', lambda *args, **kwargs: controller)
    with pytest.raises(ValueError, match='requires --windows-input gamepad-kbm'):
        module.WindowsGameEnv('aimlab')


def test_deferred_tic_spans_and_finalization_are_replayed_once():
    class Scorer:
        spec = {}
        def reset(self):
            self.spans = []
            self.finalizations = 0
        def note_tics(self, tics):
            self.spans.append(tics)
        def delta(self, frame):
            return 1.0
        def finalize(self):
            self.finalizations += 1
            return 4.0

    scorer = Scorer()
    pending = DeferredScore()
    for tic in (0, 7, 20):
        pending.add(np.zeros((8, 8, 3), np.uint8), tics=tic)
    assert pending.value is None
    assert pending.settle(scorer) == 7.0
    assert scorer.spans == [0, 7, 13]
    assert pending.settle(scorer) == 7.0
    assert scorer.finalizations == 1


def test_uncalibrated_riders_terminal_is_rejected_before_game_start():
    spec = load_game_spec("riders_republic")
    spec["lifecycle"]["terminal_detection"] = True
    with pytest.raises(ValueError, match="not calibrated"):
        validate_terminal_detection(spec)


def test_riders_fixed_duration_contract_requires_a_zero_score_start():
    spec = load_game_spec("riders_republic")
    assert spec["runtime"]["max_tics"] == 1000
    assert spec["runtime"]["tics_per_second"] == 10
    assert spec["startup"]["start_check"]["regions"] == {"score": 0}
    assert spec["scorer"]["terminal_phases"] == []
    assert validate_terminal_detection(spec) is False


def test_terminal_only_scenario_cannot_disable_detection():
    spec = load_game_spec("kingdom_rush_alliance_heart_of_the_forest")
    spec["lifecycle"]["terminal_detection"] = False
    with pytest.raises(ValueError, match="terminal-only"):
        validate_terminal_detection(spec)


def test_battleblock_templates_are_present():
    for name in ("tpl_gem0.png", "tpl_gem1.png", "tpl_gem2.png", "tpl_yarn.png"):
        assert (ROOT / "coding4game_gui/benchmark/windows/scorers/templates/battleblock" / name).is_file()
