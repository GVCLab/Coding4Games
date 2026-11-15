from pathlib import Path

import yaml

from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.games_config import load_game_config
from coding4game_gui.benchmark.windows.save_state import checkpoint_zip_path


ROOT = Path(__file__).resolve().parents[2]


def test_gyoubu_scenario_encodes_the_fixed_two_life_contract():
    spec = load_game_spec("sekiro_gyoubu")
    cfg = load_game_config("sekiro_gyoubu")

    assert spec["engine"] == "windows"
    assert spec["driver_game"] == "sekiro_gyoubu"
    assert spec["runtime"] == {
        "scenario": "sekiro_gyoubu",
        "tics_per_second": 10,
        "max_tics": 4100,
        "max_burst": 4,
        "max_calls_per_ep": 820,
        "windows_input": "gamepad",
        "max_action_tics": 1000,
    }
    assert spec["startup"] == {
        "golden_checkpoint": "account_compat_20261107",
        "preserve_player_save": True,
        "focus_alt_key": False,
        "menu_skip_replay": "closed_loop",
        "menu_skip_ocr": {
            "rect": [80, 140, 1180, 820],
            "input_size": [1180, 820],
            "min_conf": 0.5,
            "device": "gpu:0",
            "text_detection_model_name": "PP-OCRv6_small_det",
            "text_recognition_model_name": "PP-OCRv6_small_rec",
        },
        "start_check": {"kind": "scorer"},
    }
    assert spec["lifecycle"]["terminal_detection"] is True
    assert spec["scorer"]["terminal_phases"] == [
        "victory", "defeat", "resurrection"]
    assert "nonprogress_window_tics" not in spec["lifecycle"]
    assert "still_window_tics" not in spec["lifecycle"]
    assert "calibration_status" not in spec["startup"]
    assert "calibration_status" not in spec["scorer"]
    assert spec["scorer"]["total_lives"] == 2
    assert spec["scorer"]["kind"] == "image"
    assert spec["scorer"]["implementation"] == "sekiro_boss"
    assert cfg.scorer_kind == "image"
    assert cfg.scorer_implementation == "sekiro_boss"

    text = " ".join((spec["gameplay"] + spec["objective"] +
                     spec["episode_contract"]).split())
    for phrase in (
        "Gyoubu Masataka Oniwa",
        "both lives",
        "Demon Bell inactive",
        "A health-bar refill alone never confirms a completed life",
        "Resurrection is forbidden",
        "terminates the episode before another gameplay action",
    ):
        assert phrase in text


def test_gyoubu_driver_preserves_recorded_navigation_and_raw_actions():
    raw = yaml.safe_load((ROOT / "games/sekiro_gyoubu.yaml").read_text(
        encoding="utf-8"))
    cfg = load_game_config("sekiro_gyoubu")

    assert cfg.process == "sekiro.exe"
    # Current-host PrintWindow captures were black; visible recovery keeps
    # the existing foreground and occlusion checks.
    assert cfg.screenshot_backend == "printwindow_visible"
    assert cfg.auto_launch.settle_seconds == 60.0
    assert cfg.auto_launch.launch_path.replace("\\", "/").endswith(
        "/steamapps/common/Sekiro/sekiro.exe")
    assert len(raw["menu_skip"]) == 28
    assert raw["menu_skip"][13]["duration_seconds"] == 12.34
    assert raw["menu_skip"][13]["expect_text"] == "与鬼佛对坐"
    assert raw["menu_skip"][14]["buttons"] == ["X"]
    assert raw["menu_skip"][14]["expect_text"] == "购买道具"
    assert raw["menu_skip"][-2]["left_stick"] == [0.0, 1.0]
    assert raw["menu_skip"][-2]["duration_seconds"] == 4.54
    assert "trailing" in raw["menu_skip"][-1]["description"]
    assert raw["menu_skip"][-1]["duration_seconds"] == 2.11
    assert set(raw["actions"]) == {
        "neutral", "a", "b", "x", "y", "lb", "rb", "ls", "rs",
        "dpad_up", "dpad_down", "dpad_left", "dpad_right",
        "start", "back", "guide",
    }
    for action in raw["actions"].values():
        assert "duration_seconds" not in action
        assert "duration_tics" not in action


def test_gyoubu_has_an_independent_checkpoint_namespace():
    cfg = load_game_config("sekiro_gyoubu", validate_files=False)
    assert cfg.save_state.restore_to.parent.name == "Sekiro"
    spec = load_game_spec("sekiro_gyoubu")
    checkpoint = checkpoint_zip_path(spec["driver_game"], spec["startup"]["golden_checkpoint"])
    assert checkpoint.name == "sekiro_gyoubu_account_compat_20261107.zip"


def test_gyoubu_exposes_isolated_gamepad_and_kbm_guides():
    gamepad = load_game_spec("sekiro_gyoubu", windows_input="gamepad")
    kbm = load_game_spec("sekiro_gyoubu", windows_input="gamepad-kbm")

    assert "Controller guide" in gamepad["gameplay"]
    assert "Keyboard/mouse guide" not in gamepad["gameplay"]
    assert "Keyboard/mouse guide" in kbm["gameplay"]
    assert "Controller guide" not in kbm["gameplay"]
    for token in (
        "`KEY[W]`", "`KEY[SHIFT]`", "`KEY[SPACE]`", "`KEY[Q]`",
        "`CLICK[x,y]`", "`RCLICK[x,y]`", "`MCLICK[x,y]`",
        "`KEY[X]`", "`KEY[C]`", "`KEY[Z]`", "`KEY[CTRL]`",
        "`KEY[F]`", "`KEY[E]`", "`KEY[R]`", "`KEY[V]`",
        "`KEY[G]`", "`KEY[ESC]`",
    ):
        assert token in kbm["gameplay"]
    normalized = " ".join(kbm["gameplay"].split())
    assert "cannot hold guard" in normalized
    assert "Combat Art is unavailable through stepped KBM" in normalized
    assert "Absolute pointer movement is not mouse-relative camera input" in normalized
