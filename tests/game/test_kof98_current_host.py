"""Current-host startup captures must agree with the shipped HUD calibration."""
from pathlib import Path

import cv2
import pytest

from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.env import ResetController
from coding4game_gui.benchmark.windows.games_config import load_game_config
from coding4game_gui.benchmark.windows.scorers.kof98_terminal import Kof98TerminalScorer

FIXTURES = Path(__file__).parents[1] / "fixtures" / "kof98_current_host"


def _frame(name):
    frame = cv2.imread(str(FIXTURES / name))
    assert frame is not None
    return frame


def test_current_native_hud_has_full_health_and_sixty_seconds():
    spec = load_game_spec("kof98_terminal")
    scorer = Kof98TerminalScorer(spec["scorer"], (854, 480))
    frame = _frame("start_go_60.png")
    assert scorer.in_combat(frame)
    assert scorer.read(frame) == (1.0, 1.0)
    assert scorer.read_region(frame, "round_clock") == 60
    assert scorer.read_region(frame, "own_health_percent") == 100
    assert scorer.read_region(frame, "opponent_health_percent") == 100
    assert scorer._seen_live is False
    with pytest.raises(ValueError, match="unknown KOF98 HUD region"):
        scorer.read_region(frame, "absent")


def test_menu_and_go_overlay_cannot_satisfy_control_ready_handoff():
    spec = load_game_spec("kof98_terminal")
    controller = ResetController(spec)
    controller._scorer = Kof98TerminalScorer(spec["scorer"], (854, 480))
    for name in ("attract_order.png", "start_go_60.png"):
        assert controller.start_ok(_frame(name)) is False
    assert controller._scorer.read_region(_frame("attract_order.png"), "round_clock") is None


@pytest.mark.parametrize("name", ["ready_59.png", "ready_china_59.png"])
def test_measured_healthy_fifty_nine_second_hud_passes_the_shared_check(name):
    spec = load_game_spec("kof98_terminal")
    controller = ResetController(spec)
    controller._scorer = Kof98TerminalScorer(spec["scorer"], (854, 480))
    frame = _frame(name)
    assert controller._scorer.read_timer(frame) == 59
    assert controller.start_ok(frame)


def test_kof_launch_retains_normal_steam_entry_and_measured_order_stage_path():
    cfg = load_game_config("kof98", validate_files=False)
    assert Path(cfg.auto_launch.launch_path).name.lower() == "steam.exe"
    assert cfg.auto_launch.launch_args == ["-applaunch", "222420"]
    assert cfg.auto_launch.settle_seconds == 60.0
    inputs = [(index, step) for index, step in enumerate(cfg.menu_skip)
              if step.spec.buttons]
    assert [step.spec.buttons for _, step in inputs[-4:]] == [
        ["WEST"], ["SOUTH"], ["DPAD_DOWN"], ["SOUTH"]]
    # The old two-second wait sent the order inputs before ORDER SELECT.
    first_order_index = inputs[-4][0]
    assert cfg.menu_skip[first_order_index - 1].spec.duration_seconds == 10.5
    assert cfg.menu_skip[-1].spec.duration_seconds == 0.94


@pytest.mark.parametrize("name,clock,own,opponent,ending", [
    ("human_both_low_27.png", 27, 77 / 276, 46 / 277, None),
    ("human_win_25.png", 25, 77 / 276, 0.0, "ko"),
    ("human_loss_26.png", 26, 0.0, 144 / 277, "ko"),
])
def test_human_low_health_hud_remains_readable(name, clock, own, opponent, ending):
    scorer = Kof98TerminalScorer(load_game_spec("kof98_terminal")["scorer"], (854, 480))
    scorer.begin_episode(_frame("ready_china_59.png"))
    reading = scorer.snapshot(_frame(name))
    assert reading is not None
    assert reading["clock_remaining"] == clock
    assert reading["own_health"] == pytest.approx(own, abs=.004)
    assert reading["opponent_health"] == pytest.approx(opponent, abs=.004)
    assert reading["ending"] == ending
    if ending:
        assert reading["score"] == pytest.approx((1 + own - opponent) / 2, abs=.004)


@pytest.mark.parametrize("name", ["human_ko_overlay.png", "human_team_select.png"])
def test_human_missing_hud_is_not_a_score(name):
    scorer = Kof98TerminalScorer(load_game_spec("kof98_terminal")["scorer"], (854, 480))
    scorer.begin_episode(_frame("ready_china_59.png"))
    assert scorer.snapshot(_frame(name)) is None
