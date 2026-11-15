"""TrackMania on main's schema: pin what was measured on mr-why, 2026-09-23.

Every assertion here is a fact found by running the game (scripts/tm/), not a
restatement of the YAML. The ones that matter most are the negative ones: a
restart button the policy can reach, or a clock check aimed at the wrong
pixels, would not raise anything -- they would just produce wrong numbers.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
DRIVER = REPO / "games" / "trackmania.yaml"
SCENARIO = REPO / "coding4game_gui" / "benchmark" / "scenarios" / "trackmania.yaml"


@pytest.fixture(scope="module")
def driver():
    return yaml.safe_load(DRIVER.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def scenario():
    return yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))


def test_it_runs_on_its_own_platform(driver):
    assert driver["platform"] == "windows-kbm"


def test_trusted_menu_replay_uses_physical_keys(driver):
    from coding4game_gui.benchmark.windows.games_config import load_game_config
    from coding4game_gui.benchmark.windows.drivers.windows_kbm.input import SCANCODES
    cfg = load_game_config("trackmania", validate_files=False)
    for step in cfg.menu_skip:
        for button in step.spec.buttons:
            assert button.startswith("KEY[") and button[4:-1] in SCANCODES
        assert "KEY[DELETE]" not in step.spec.buttons
    assert "b" not in driver["actions"]


def test_the_driving_keys_reach_the_shared_raw_keyboard_surface():
    from coding4game_gui.benchmark.windows.raw_actions import resolve_raw_action
    from coding4game_gui.benchmark.windows.games_config import load_game_config
    cfg = load_game_config("trackmania", validate_files=False)
    action = resolve_raw_action(["KEY[UP]", "KEY[LEFT]"], cfg.actions,
                                input_mode="gamepad-kbm")
    assert set(action.state["buttons"]) == {"KEY[UP]", "KEY[LEFT]"}
    with pytest.raises(ValueError):
        resolve_raw_action(["KEY[DELETE]"], cfg.actions, input_mode="gamepad-kbm")


def test_the_window_is_the_size_the_scorer_was_calibrated_on(driver, scenario):
    assert list(driver["window"]["size"]) == list(
        scenario["scorer"]["calibrated_client_size"])


def test_the_start_check_and_the_freeze_probe_look_at_the_clock(driver, scenario):
    clock = scenario["scorer"]["regions"]["clock"]["rect"]
    assert scenario["startup"]["start_check"]["rect"] == clock
    assert driver["freeze"]["time_probe_rect"] == clock


def test_the_launch_is_direct_not_through_steam(driver):
    """Measured: through Steam 129 s to a window, directly 3 s."""
    assert driver["auto_launch"]["launch_path"].endswith("TmForever.exe")
    assert driver["auto_launch"]["launch_method"] == "desktop_shell"


def test_cold_start_does_not_toggle_display_during_the_native_intro(driver):
    """The 20-second wait failed before any menu input on the current host.

    native_settle_03 (2026-10-23) records the blank globe and later profile
    picker. Presentation and menu replay must wait for the measured settle.
    """
    assert driver["auto_launch"]["settle_seconds"] >= 60


def test_the_cold_path_survives_the_auto_login_prompt(driver):
    """Left alone, the picker auto-selects the profile and asks to connect
    online; the production path always met that prompt (tm_smoke5/6)."""
    clicks = [tuple(s["click"]) for s in driver["menu_skip"][:3]]
    assert clicks == [(790, 385), (640, 236), (790, 385)]
    assert all("buttons" not in s for s in driver["menu_skip"][:3]), (
        "ENTER on the prompt presses 连接 and opens account creation")


def test_the_restore_does_not_wipe_a_onedrive_folder(driver):
    """rmtree on OneDrive's ReadOnly folders raised WinError 5 (tm_smoke1)."""
    assert driver["save_state"]["wipe_before"] is False
    from coding4game_gui.benchmark.windows.save_state import SaveStateSpec  # noqa: F401


def test_a_clicked_cold_path_is_replayed_closed_loop(driver, scenario):
    """open_loop is run_pad_sequence, which never reads `click`: every click
    of the cold path was a no-op in tm_smoke3 and start_check failed twice."""
    if any("click" in step for step in driver["menu_skip"]):
        assert scenario["startup"]["menu_skip_replay"] == "closed_loop"


def test_the_freeze_never_presses_esc_on_a_menu(driver):
    """Pause-key freeze pressed ESC during menu replay (the menu's clock region
    is static, so unpause() read "not advancing"), which turned the profile
    picker into the connect-online prompt (tm_smoke3-5). Current-host freezing
    uses the shared process-suspension implementation and no pause key."""
    assert driver["freeze"]["method"] == "speedhack"
    assert "key" not in driver["freeze"]
    assert driver["use_speedhack"] is True
