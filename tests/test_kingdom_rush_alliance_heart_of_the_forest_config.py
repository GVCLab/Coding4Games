"""Static contract tests for the Stage 3 Kingdom Rush scenario."""

from pathlib import Path
import re

import yaml

from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.code.prompts import render_development_prompt
from coding4game_gui.benchmark.windows.raw_actions import select_windows_gameplay
from coding4game_gui.benchmark.windows.scorers.factory import ScorerFactory
from coding4game_gui.benchmark.windows.games_config import load_game_config


ROOT = Path(__file__).resolve().parents[1]
DRIVER_PATH = ROOT / "games" / "kingdom_rush_alliance_heart_of_the_forest.yaml"
SCENARIO_PATH = (
    ROOT / "coding4game_gui" / "benchmark" / "scenarios"
    / "kingdom_rush_alliance_heart_of_the_forest.yaml"
)
GOLDEN_PATH = (
    ROOT / "snapshots" / "kingdom_rush_alliance_heart_of_the_forest_golden.zip"
)

ACTION_NAMES = {
    "neutral", "a", "b", "x", "y", "lb", "rb", "ls", "rs",
    "dpad_up", "dpad_down", "dpad_left", "dpad_right", "start", "back",
    "guide",
}
GUIDE_TOKENS = [
    "neutral", "LS[x,y]", "RS[x,y]", "LT[v]", "RT[v]",
    "a", "b", "x", "y", "lb", "rb", "ls", "rs",
    "dpad_up", "dpad_down", "dpad_left", "dpad_right", "start", "back",
    "guide",
]
KBM_GUIDE_TOKENS = ["NEUTRAL", "KEY[<key>]", "MOVE[x,y]", "CLICK[x,y]"]


def _raw(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _guide_tokens(text: str, mode: str) -> list[str]:
    selected = select_windows_gameplay(
        text, mode, scenario="Kingdom Rush 5: Alliance — The Heart of the Forest"
    )
    if mode == "gamepad":
        heading = "Controller guide (game-default controller preset; context-sensitive):"
    else:
        heading = "Keyboard/mouse guide (game-default PC preset; context-sensitive):"
    assert selected.count(heading) == 1
    assert selected.count("Game-default preset:") == 1
    assert "no custom remapping is assumed" in " ".join(selected.split())
    guide = selected.split(heading, 1)[1]
    assert "| Input token | Gameplay meaning | Menu / contextual meaning |" in guide
    return re.findall(r"^\| `([^`]+)` \|", guide, flags=re.MULTILINE)


def test_driver_is_independent_and_uses_the_recorded_stage3_startup():
    data = _raw(DRIVER_PATH)
    cfg = load_game_config("kingdom_rush_alliance_heart_of_the_forest")

    assert data["name"] == "Kingdom Rush 5: Alliance — The Heart of the Forest"
    assert data["process"] == "Kingdom Rush Alliance.exe"
    assert data["screenshot_backend"] == "printwindow"
    assert data["auto_launch"]["launch_path"].endswith(
        "/Kingdom Rush Alliance/Kingdom Rush Alliance.exe"
    )
    assert data["save_state"] == {
        "restore_to": "%APPDATA%/kingdom_rush_alliance",
        "wipe_before": True,
    }
    assert len(data["menu_skip"]) == 20
    trailing = data["menu_skip"][-1]
    assert trailing["pad"] == 1
    assert trailing["duration_seconds"] == 12.34
    assert "buttons" not in trailing
    assert "trailing" in trailing["description"]
    assert set(data["actions"]) == ACTION_NAMES
    for name, action in data["actions"].items():
        assert "duration_seconds" not in action
        assert "duration_tics" not in action
        if name == "neutral":
            assert set(action) == {"description"}
    assert cfg.process == data["process"]
    assert cfg.scorer_kind == "ocr"
    assert cfg.scorer_implementation == "kingdom_rush_alliance"


def test_scenario_has_the_stage3_contract_and_no_generic_calibration_flag():
    data = _raw(SCENARIO_PATH)
    assert set(data) == {
        "title", "engine", "driver_game", "gameplay", "objective",
        "episode_contract", "runtime", "startup", "lifecycle", "scorer",
    }
    assert "calibration_status" not in data["scorer"]
    assert data["engine"] == "windows"
    assert data["driver_game"] == "kingdom_rush_alliance_heart_of_the_forest"
    assert data["runtime"] == {
        "scenario": "kingdom_rush_alliance_heart_of_the_forest",
        "tics_per_second": 10,
        "max_tics": None,
        "terminal_only_final_tests": True,
        "max_burst": 5,
        "max_calls_per_ep": None,
    }
    assert data["startup"] == {
        "golden_checkpoint": "golden",
        "start_check": {
            "pattern": r"20\s*500",
            "rect": [0, 0, 300, 75],
            "regions": {"wave": 0},
            "poll_tics": 10,
            "timeout_seconds": 80,
        },
        "menu_skip_replay": "closed_loop",
    }
    assert data["lifecycle"] == {
        "terminal_detection": True,
    }
    assert data["scorer"]["kind"] == "ocr"
    assert data["scorer"]["implementation"] == "kingdom_rush_alliance"
    assert data["scorer"]["calibrated_client_size"] == [2560, 1440]
    assert data["scorer"]["score_phases"] == ["victory"]
    assert data["scorer"]["terminal_phases"] == ["victory", "defeat"]
    assert data["scorer"]["regions"] == {
        "lives": {
            "rect": [20, 10, 110, 60], "kind": "number",
            "recognition_rect": [60, 28, 50, 35], "trim_light_text": True,
        },
        "wave": {
            "pattern": r"(\d+)\s*/\s*10",
            "rect": [20, 60, 130, 60],
        },
    }
    assert data["scorer"]["victory_score"] == {
        "base": 90,
        "lives_bonus": 10,
        "initial_lives": 20,
    }
    assert data["scorer"]["failure_score"] == {
        "base": 1,
        "wave_bonus": 75,
        "lives_bonus": 13,
        "max_score": 89,
        "max_wave": 10,
    }


def test_scorer_factory_and_prompt_expose_stage3_without_semantic_actions():
    spec = load_game_spec("kingdom_rush_alliance_heart_of_the_forest")
    scorer_spec = spec["scorer"]
    factory = ScorerFactory()
    factory._ocr_engine = lambda frame: []
    scorer = factory.create(
        spec=scorer_spec,
        client_size=tuple(scorer_spec["calibrated_client_size"]),
        kind=scorer_spec["kind"],
        implementation=scorer_spec["implementation"],
    )
    assert type(scorer).__name__ == "KingdomRushAllianceScorer"

    gameplay_raw = _raw(SCENARIO_PATH)["gameplay"]
    assert _guide_tokens(gameplay_raw, "gamepad") == GUIDE_TOKENS
    assert _guide_tokens(gameplay_raw, "gamepad-kbm") == KBM_GUIDE_TOKENS
    objective = " ".join(_raw(SCENARIO_PATH)["objective"].split())
    assert "Complete all 10 waves" in objective
    assert "90 + 10 * remaining_lives / 20" in objective
    assert "capped at 89" in objective
    assert "will be calibrated" not in objective
    assert "Infrastructure failure is invalid" in objective


    for input_mode, heading in (
        ("gamepad", "Controller guide (game-default controller preset; context-sensitive):"),
        ("gamepad-kbm", "Keyboard/mouse guide (game-default PC preset; context-sensitive):"),
    ):
        prompt = render_development_prompt(
            load_game_spec("kingdom_rush_alliance_heart_of_the_forest", windows_input=input_mode),
            tic_budget=100, token_limit=1000, wall_seconds=60,
        )
        normalized = " ".join(prompt.split())
        assert prompt.count(heading) == 1
        assert "Complete all 10 waves" in normalized
        assert "90 + 10 * remaining_lives / 20" in normalized
        assert "MOVE_HERO" not in normalized
        assert "USE_SKILL" not in normalized
        assert "calibrated_client_size" not in normalized
