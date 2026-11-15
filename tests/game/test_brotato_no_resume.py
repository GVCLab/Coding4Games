from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from coding4game_gui.benchmark.windows import game_settings
from coding4game_gui.benchmark.windows.startup import prepare_launch_state


REPO = Path(__file__).resolve().parents[2]
SCENARIO = (REPO / "coding4game_gui" / "benchmark" / "scenarios"
            / "brotato.yaml")
ACTIVE_ACCOUNT_ID = 1509794311
ACTIVE_STEAM_ID = "76561199470060039"


def _brotato_account(tmp_path: Path, monkeypatch) -> Path:
    appdata = tmp_path / "AppData" / "Roaming"
    account = appdata / "Brotato" / ACTIVE_STEAM_ID
    account.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(
        game_settings, "_active_steam_account_id",
        lambda: ACTIVE_ACCOUNT_ID,
    )
    return account


def test_brotato_cleanup_removes_only_resumable_run(tmp_path, monkeypatch):
    account = _brotato_account(tmp_path, monkeypatch)
    resume_files = [
        account / "run_v3_0.json",
        account / "run_v3_0_01.json.bak",
        account / "run_v3_0_05.json.bak",
    ]
    for path in resume_files:
        path.write_text("resumable", encoding="utf-8")
    permanent = account / "save_v3_0.json"
    permanent_backup = account / "save_v3_0_01.json.bak"
    settings = account / "settings.json"
    for path in (permanent, permanent_backup, settings):
        path.write_text("keep", encoding="utf-8")

    what = game_settings.clear_brotato_saved_run()

    assert what is not None
    assert all(not path.exists() for path in resume_files)
    assert permanent.read_text(encoding="utf-8") == "keep"
    assert permanent_backup.read_text(encoding="utf-8") == "keep"
    assert settings.read_text(encoding="utf-8") == "keep"


def test_brotato_cleanup_does_not_touch_another_steam_id(
        tmp_path, monkeypatch):
    account = _brotato_account(tmp_path, monkeypatch)
    foreign = account.parent / "76561198970876301"
    foreign.mkdir()
    foreign_run = foreign / "run_v3_0.json"
    foreign_run.write_text("foreign", encoding="utf-8")

    assert game_settings.clear_brotato_saved_run() is None
    assert foreign_run.read_text(encoding="utf-8") == "foreign"


def test_brotato_cleanup_blocks_when_active_steam_user_is_unknown(
        tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(
        game_settings, "_active_steam_account_id", lambda: None)

    with pytest.raises(RuntimeError, match="active Steam account"):
        game_settings.clear_brotato_saved_run()


def test_no_golden_start_runs_cleanup_without_window_setup(monkeypatch):
    killed = []
    cleared = []
    monkeypatch.setattr(
        "coding4game_gui.benchmark.windows.startup.ss.kill_process",
        lambda process: killed.append(process),
    )
    monkeypatch.setattr(
        game_settings, "clear_saved_run",
        lambda game, exe_path=None: cleared.append((game, exe_path)),
    )
    cfg = SimpleNamespace(
        process="Brotato.exe",
        save_state=None,
        auto_launch=SimpleNamespace(launch_path="D:/App/STEAM/steam.exe"),
    )

    result = prepare_launch_state({
        "driver_game": "brotato",
        "startup": {},
        "scorer": {},
    }, cfg)

    assert result == {"mode": "no_save_cleanup"}
    assert killed == ["Brotato.exe"]
    assert cleared == [("brotato", "D:/App/STEAM/steam.exe")]


def test_brotato_scenario_uses_no_save_cleanup_contract():
    scenario = yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))

    assert "golden_checkpoint" not in scenario["startup"]
    assert "clears any resumable run" in scenario["episode_contract"]
    assert "brotato" in game_settings.SAVE_CLEARERS


def test_brotato_uses_closed_loop_menu_replay(monkeypatch):
    from coding4game_gui.benchmark.spec import load_game_spec
    from coding4game_gui.benchmark.windows import auto_nav
    from coding4game_gui.benchmark.windows.env import ResetController

    controller = ResetController(load_game_spec("brotato"))
    assert controller._menu_skip_replay == "closed_loop"

    monkeypatch.setattr(controller, "_force_foreground", lambda _env: True)
    monkeypatch.setattr(
        auto_nav,
        "run_pad_sequence",
        lambda *_args, **_kwargs: pytest.fail(
            "Brotato must not use one-shot open-loop menu replay"),
    )
    pressed = []
    waited = []
    monkeypatch.setattr(
        controller,
        "_press_until_screen_changes",
        lambda _env, step: pressed.append(step),
    )
    monkeypatch.setattr("time.sleep", lambda seconds: waited.append(seconds))
    env = SimpleNamespace(
        hard_pause=True,
        unpause=lambda: None,
        pause=lambda: None,
    )

    controller.replay_menu_skip(env)

    action_steps = [
        step for step in controller.cfg.menu_skip
        if (step.spec.buttons or any(step.spec.left_stick)
            or any(step.spec.right_stick) or step.spec.left_trigger
            or step.spec.right_trigger)
    ]
    wait_steps = [
        step.spec.duration_seconds for step in controller.cfg.menu_skip
        if not (step.spec.buttons or any(step.spec.left_stick)
                or any(step.spec.right_stick) or step.spec.left_trigger
                or step.spec.right_trigger)
    ]
    assert pressed == action_steps
    assert waited == wait_steps
    assert env.hard_pause is True
