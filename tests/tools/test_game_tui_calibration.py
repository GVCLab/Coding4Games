from pathlib import Path

from coding4game_gui.benchmark.experiment_plan import BenchmarkScenario
from scripts import game_tui


def test_record_scorer_calibration_calls_bundled_recorder(monkeypatch):
    calls = []
    monkeypatch.setattr(game_tui, "clear", lambda: None)
    monkeypatch.setattr(game_tui, "banner", lambda *_args: None)
    monkeypatch.setattr(game_tui, "confirm", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        game_tui,
        "run_python_script",
        lambda path, args: calls.append((path, args)),
    )
    monkeypatch.setattr(game_tui, "pause", lambda: None)

    game_tui.record_scorer_calibration("balatro")

    assert calls == [(
        game_tui.SCORER_SESSION_RECORDER,
        ["--scenario", "balatro", "--fps", "10", "--until-stopped"],
    )]
    assert calls[0][0].name == "record_scorer_session.py"


def test_record_scorer_calibration_cancel_does_not_launch(monkeypatch):
    calls = []
    monkeypatch.setattr(game_tui, "clear", lambda: None)
    monkeypatch.setattr(game_tui, "banner", lambda *_args: None)
    monkeypatch.setattr(game_tui, "confirm", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        game_tui,
        "run_python_script",
        lambda path, args: calls.append((path, args)),
    )

    game_tui.record_scorer_calibration("balatro")

    assert calls == []


def test_recording_menu_routes_scorer_calibration(monkeypatch):
    selected = iter([1, None])
    called = []
    monkeypatch.setattr(game_tui, "menu", lambda *_args, **_kwargs: next(selected))
    monkeypatch.setattr(
        game_tui,
        "select_benchmark_scenario",
        lambda *_args, **_kwargs: type("Scenario", (), {"slug": "balatro"})(),
    )
    monkeypatch.setattr(
        game_tui,
        "record_scorer_calibration",
        lambda slug: called.append(slug),
    )

    game_tui.recording_menu()

    assert called == ["balatro"]


def test_menu_recording_uses_the_selected_scenarios_start_state(monkeypatch):
    scenario = BenchmarkScenario(
        "kof98_terminal", "KOF98", "windows", Path("kof98_terminal.yaml"), "kof98", 10)
    choices = iter([0, None])
    calls = []
    monkeypatch.setattr(game_tui, "menu", lambda *_args, **_kwargs: next(choices))
    monkeypatch.setattr(game_tui, "select_benchmark_scenario", lambda *_a, **_kw: scenario)
    monkeypatch.setattr(game_tui, "clear", lambda: None)
    monkeypatch.setattr(game_tui, "banner", lambda *_args: None)
    monkeypatch.setattr(game_tui, "confirm", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(game_tui, "_load_yaml", lambda _slug: {"menu_skip": []})
    monkeypatch.setattr(game_tui, "run_script", lambda path, args: calls.append((path, args)))
    monkeypatch.setattr(game_tui, "close_or_pause", lambda _slug: None)

    game_tui.recording_menu()

    assert calls == [("calibration/record_pad_to_yaml.py", [
        "--game", "kof98", "--scenario", "kof98_terminal", "--auto-launch"])]


def test_driver_validation_can_inspect_an_invalid_config(monkeypatch, capsys):
    choices = iter([3, 0, None])
    monkeypatch.setattr(game_tui, "game_files", lambda: [Path("balatro.yaml")])
    monkeypatch.setattr(game_tui, "validate", lambda _path: (False, "Invalid saved configuration"))
    monkeypatch.setattr(game_tui, "pause", lambda: None)

    def select(title, items, **_kwargs):
        if title == "Validate driver config · pick a game":
            assert items[0]["label"] == "balatro"
            assert not items[0]["disabled"]
        return next(choices)

    monkeypatch.setattr(game_tui, "menu", select)
    game_tui.recording_menu()
    assert "Invalid saved configuration" in capsys.readouterr().out


def test_full_launch_check_selects_the_scenario_for_the_driver(monkeypatch):
    scenarios = [
        BenchmarkScenario("aimlab", "Aim Lab", "windows", Path("aimlab.yaml"), "aimlab", 10),
        BenchmarkScenario("overcooked_2_2_1", "Story 2-1", "windows", Path("story.yaml"), "overcooked_2", 10),
    ]
    choices = iter([2, 0, None])
    calls = []
    menus = []
    monkeypatch.setattr(game_tui, "discover_benchmark_scenarios", lambda: scenarios)
    monkeypatch.setattr(game_tui, "_scenario_ready", lambda _scenario: (True, "ready"))

    def select(_title, items, **_kwargs):
        menus.append([item["label"] for item in items])
        return next(choices)

    monkeypatch.setattr(game_tui, "menu", select)
    monkeypatch.setattr(game_tui, "run_script", lambda path, args: calls.append((path, args)))
    monkeypatch.setattr(game_tui, "close_or_pause", lambda _slug: None)

    game_tui.launch_checks_menu("overcooked_2")

    assert menus[1] == ["overcooked_2_2_1"]
    assert calls == [("calibration/test_auto_launch_full.py", ["--scenario", "overcooked_2_2_1"])]
