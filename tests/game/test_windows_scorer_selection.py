from pathlib import Path
import re
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from coding4game_gui.benchmark import spec as agentic
from coding4game_gui.benchmark.code.prompts import render_development_prompt
from coding4game_gui.benchmark.direct.player import build_direct_system
from coding4game_gui.catalog import FORMAL_SCENARIOS, input_mode
from coding4game_gui.benchmark.windows.env import ResetController
from coding4game_gui.benchmark.windows.scorers.bbt_progress import BbtProgressScorer
from coding4game_gui.benchmark.windows.scorers.black_myth import BlackMythScorer
from coding4game_gui.benchmark.windows.scorers.factory import ScorerFactory
from coding4game_gui.benchmark.windows.scorers.forza_horizon_5 import ForzaHorizon5Scorer
from coding4game_gui.benchmark.windows.scorers.kingdom_rush_alliance import KingdomRushAllianceScorer
from coding4game_gui.benchmark.windows.scorers.kof98_terminal import Kof98TerminalScorer
from coding4game_gui.benchmark.windows.scorers.nba_2k25 import Nba2k25Scorer
from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer
from coding4game_gui.benchmark.windows.scorers.overcooked_2 import Overcooked2Scorer
from coding4game_gui.benchmark.windows.scorers.race import RaceScorer
from coding4game_gui.benchmark.windows.scorers.riders_republic import RidersRepublicScorer
from coding4game_gui.benchmark.windows.scorers.save_verification import SaveVerificationScorer
from coding4game_gui.benchmark.windows.scorers.sekiro_boss import SekiroBossScorer
from coding4game_gui.benchmark.windows.games_config import load_game_config
from coding4game_gui.benchmark.windows.games_config import parse_game_config
from coding4game_gui.benchmark.windows.terminal import validate_terminal_detection
from coding4game_gui.paths import REPO_ROOT


RUNTIME_SCORER_CLASSES = {
    "aimlab": OcrScorer,
    "balatro": OcrScorer,
    "battleblock": BbtProgressScorer,
    "black_myth": BlackMythScorer,
    "brotato": OcrScorer,
    "forza_horizon_5": ForzaHorizon5Scorer,
    "kingdom_rush_alliance_heart_of_the_forest": KingdomRushAllianceScorer,
    "kof98_terminal": Kof98TerminalScorer,
    "nba_2k25": Nba2k25Scorer,
    "overcooked_2_2_1": Overcooked2Scorer,
    "palworld_capture_penking": SaveVerificationScorer,
    "riders_republic": RidersRepublicScorer,
    "sekiro_gyoubu": SekiroBossScorer,
    "slay_the_spire": OcrScorer,
    "trackmania": RaceScorer,
}
WINDOWS_SCENARIO_KEYS = {
    "title", "engine", "driver_game", "gameplay", "objective",
    "episode_contract", "runtime", "startup", "lifecycle", "scorer",
}
HAN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]')


def _windows_spec(driver_game: str, scorer: dict) -> dict:
    return {
        "title": "Scorer selection test",
        "engine": "windows",
        "driver_game": driver_game,
        "gameplay": {},
        "objective": {},
        "episode_contract": {},
        "runtime": {},
        "startup": {},
        "lifecycle": {},
        "scorer": scorer,
    }


def _write_scenario(path, driver_game: str, scorer: dict,
                    runtime: dict | None = None) -> None:
    spec = _windows_spec(driver_game, scorer)
    spec["runtime"] = runtime or {}
    path.write_text(
        yaml.safe_dump(spec, sort_keys=False),
        encoding="utf-8",
    )


def test_release_scenarios_and_drivers_match_formal_catalog(load_driver_metadata, monkeypatch):
    paths = sorted(agentic.WINDOWS_GAMES_DIR.glob("*.yaml"))
    assert {path.stem for path in paths} == set(FORMAL_SCENARIOS) == set(RUNTIME_SCORER_CLASSES)
    monkeypatch.setattr(ScorerFactory, "_ocr", lambda *_args: lambda _frame: [])
    drivers = set()
    fixed_duration = {"aimlab", "nba_2k25", "riders_republic"}
    for path in paths:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(raw) == WINDOWS_SCENARIO_KEYS, path.name
        assert raw["runtime"]["tics_per_second"] == 10, path.name
        mode = input_mode(path.stem)
        spec = agentic.load_game_spec(path.stem, windows_input=mode)
        development = render_development_prompt(
            spec, tic_budget=1200, token_limit=30000, wall_seconds=300)
        for game_label in ('召唤援军', '箭矢风暴'):
            development = development.replace(game_label, '')
        assert not HAN.search(development), path.name
        assert 'submission.finish' in development, path.name
        direct = build_direct_system(spec, spec['runtime']['max_burst'])
        assert spec['title'] in direct and '# Goal and scoring' in direct, path.name
        assert '# Available controls' in direct and 'ACT' in direct, path.name
        assert not any(marker in direct.lower() for marker in (
            'workspace', 'submission.save', 'submission.finish', 'policy.py',
            'code_contract', '```python', 'model-token budget', 'holdout',
        )), path.name
        driver = spec["driver_game"]
        drivers.add(driver)
        controller = ResetController(spec)
        cfg = controller.cfg
        assert cfg.auto_launch and cfg.auto_launch.launch_path, path.name
        executable = Path(cfg.auto_launch.launch_path)
        assert executable.is_absolute() and executable.suffix.lower() == ".exe", path.name
        assert cfg.menu_skip, path.name
        width, height = spec["scorer"]["calibrated_client_size"]
        with monkeypatch.context() as game_patch:
            if path.stem == 'palworld_capture_penking':
                game_patch.setattr(ScorerFactory, '_ocr', lambda *_: pytest.fail('Save scoring requested OCR'))
            scorer = controller.make_scorer(SimpleNamespace(window_width=width, window_height=height))
        assert type(scorer) is RUNTIME_SCORER_CLASSES[path.stem], path.name
        assert validate_terminal_detection(spec) is (path.stem not in fixed_duration), path.name
        assert "C4G_WINDOWS_INPUT:" not in spec["gameplay"], path.name
    assert drivers == {path.stem for path in (REPO_ROOT / "games").glob("*.yaml")}


def test_older_windows_spec_defaults_to_ten_tics_per_second(
        monkeypatch, tmp_path):
    _write_scenario(tmp_path / "legacy.yaml", "black_myth", {})
    monkeypatch.setattr(agentic, "WINDOWS_GAMES_DIR", tmp_path)

    spec = agentic.load_game_spec("legacy")

    assert spec["runtime"]["tics_per_second"] == 10


@pytest.mark.parametrize("value", [0, -1, 10.0, True, "20"])
def test_windows_spec_rejects_invalid_tic_rate(
        monkeypatch, tmp_path, value):
    _write_scenario(
        tmp_path / "invalid_rate.yaml", "black_myth", {},
        runtime={"tics_per_second": value})
    monkeypatch.setattr(agentic, "WINDOWS_GAMES_DIR", tmp_path)

    with pytest.raises(ValueError, match="tics_per_second.*positive integer"):
        agentic.load_game_spec("invalid_rate")


def test_scenario_scorer_kind_overrides_driver_default(monkeypatch, tmp_path):
    _write_scenario(tmp_path / "scorer_override.yaml", "black_myth",
                    {"kind": "image", "implementation": "kof98_terminal"})
    monkeypatch.setattr(agentic, "WINDOWS_GAMES_DIR", tmp_path)

    spec = agentic.load_game_spec("scorer_override")

    cfg = load_game_config("black_myth")
    assert (cfg.scorer_kind, cfg.scorer_implementation) == (
        "image", "black_myth")
    assert spec["scorer"]["kind"] == "image"
    assert spec["scorer"]["implementation"] == "kof98_terminal"


def test_benchmark_rejects_missing_driver_default_even_with_override(
        monkeypatch, tmp_path):
    # A scenario override cannot replace the per-game declaration: every
    # benchmarked driver must remain self-describing in games/<game>.yaml.
    from coding4game_gui.benchmark.windows import games_config

    driver = parse_game_config({
        "name": "Demo",
        "process": "Demo.exe",
        "actions": {"neutral": {"description": "No input"}},
    })
    monkeypatch.setattr(games_config, "load_game_config", lambda *_args, **_kwargs: driver)
    _write_scenario(tmp_path / "missing_scorer.yaml", "demo",
                    {"kind": "ocr", "implementation": "ocr"})
    monkeypatch.setattr(agentic, "WINDOWS_GAMES_DIR", tmp_path)

    with pytest.raises(ValueError, match="has no complete .* scorer"):
        agentic.load_game_spec("missing_scorer")


def test_driver_config_rejects_unknown_scorer_kind():
    with pytest.raises(ValueError, match="unknown scorer.kind"):
        parse_game_config({
            "name": "Demo",
            "process": "Demo.exe",
            "scorer": {"kind": "ocrr", "implementation": "black_myth"},
            "actions": {"confirm": {"buttons": ["A"]}},
        })


def test_driver_config_rejects_kind_implementation_mismatch():
    with pytest.raises(ValueError, match="requires kind 'image'"):
        parse_game_config({
            "name": "Demo",
            "process": "Demo.exe",
            "scorer": {"kind": "ocr", "implementation": "black_myth"},
            "actions": {"confirm": {"buttons": ["A"]}},
        })


def test_runtime_dispatch_rejects_unknown_implementation_without_ocr_fallback(
        monkeypatch):
    from coding4game_gui.benchmark.windows import games_config

    cfg = SimpleNamespace(
        process="scorer-selection-test-does-not-exist.exe",
        scorer_kind="ocr",
        scorer_implementation="ocr",
        actions={},
    )
    monkeypatch.setattr(games_config, "load_game_config", lambda _name: cfg)
    controller = ResetController({
        "driver_game": "demo",
        "scorer": {"kind": "image", "implementation": "boss_healt"},
    })

    with pytest.raises(ValueError, match="unknown scorer.implementation"):
        controller.make_scorer(SimpleNamespace(
            window_width=1280, window_height=720))


def test_save_verification_can_end_on_registered_ocr_failure():
    frame = np.zeros((1440, 2560, 3), dtype=np.uint8)

    def fake_ocr(_frame):
        return [{"text": "DEFEAT", "conf": 1.0,
                 "box": [1044, 414, 470, 120]}]

    scorer = SaveVerificationScorer(
        {
            "calibrated_client_size": [2560, 1440],
            "failure_detection": {
                "phases": {
                    "defeat": {
                        "any": ["DEFEAT"],
                        "rect": [760, 250, 1040, 500],
                    },
                },
                "terminal_phases": ["defeat"],
            },
        },
        client_size=(2560, 1440),
        ocr=fake_ocr,
    )

    assert scorer.is_terminal(frame) is True
    assert scorer.classify_phase(frame) == "defeat"
    assert scorer.delta(frame) == 0.0
    assert scorer.total == 0.0
