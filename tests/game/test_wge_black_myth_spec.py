from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.games_config import load_game_config


def test_black_myth_benchmark_spec_maps_to_driver_and_boss_health_score():
    spec = load_game_spec("black_myth")
    cfg = load_game_config("black_myth")

    assert spec["engine"] == "windows"
    assert spec["driver_game"] == "black_myth"
    assert cfg.scorer_kind == "image"
    assert cfg.scorer_implementation == "black_myth"
    actions = {a["name"] for a in spec["action_catalog"]}
    assert actions == {
        "NEUTRAL", "A", "B", "X", "Y", "LB", "RB", "LS", "RS",
        "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
        "START", "BACK", "GUIDE",
    }
    assert spec["runtime"]["tics_per_second"] == 10
    assert spec["scorer"]["kind"] == "image"
    assert spec["scorer"]["implementation"] == "black_myth"
    assert spec["scorer"]["calibrated_client_size"] == [1280, 720]
    assert spec["scorer"]["boss_health"]["scale"] == 100
    terminal = spec["scorer"]["terminal_menu"]
    assert terminal["min_luma"] == 85
    assert len(terminal["regions"]) == 3
    assert spec["startup"]["golden_checkpoint"] == "golden"
    assert spec["startup"]["focus_alt_key"] is False
    assert spec["startup"]["menu_skip_replay"] == "open_loop"
    assert spec["startup"]["start_check"] == {
        "pattern": "黑熊精",
        "rect": [400, 580, 220, 100],
        "timeout_seconds": 120.0,
    }
    assert spec["scorer"]["victory_text"] == {
        "pattern": "已击破",
        "rect": [540, 300, 220, 140],
    }
    assert spec["runtime"]["max_tics"] == 10000


def test_black_myth_has_no_benchmark_blocking_calibration_marker():
    spec = load_game_spec("black_myth")

    assert "calibration_status" not in spec["scorer"]
    assert "calibration_status" not in spec["startup"]


def test_black_myth_waits_one_minute_before_menu_skip():
    cfg = load_game_config("black_myth")

    assert cfg.auto_launch is not None
    assert cfg.auto_launch.settle_seconds == 60.0


def test_black_myth_bypasses_the_interactive_top_level_bootstrap():
    cfg = load_game_config("black_myth")

    assert cfg.auto_launch is not None
    launch_path = cfg.auto_launch.launch_path.replace("\\", "/")
    assert launch_path.endswith(
        "/b1/Binaries/Win64/b1-Win64-Shipping.exe"
    )
