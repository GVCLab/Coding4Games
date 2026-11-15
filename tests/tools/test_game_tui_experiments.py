"""Operator menu navigation, previews and default-deny launches."""
from os import terminal_size
from pathlib import Path
import shutil
import pytest
from coding4game_gui.benchmark.code import run_codex
from coding4game_gui.benchmark.experiment_plan import BENCHMARK_MODULE
from coding4game_gui.benchmark.experiment_plan import BenchmarkScenario
from coding4game_gui.benchmark.experiment_plan import DevelopmentPlan
from coding4game_gui.benchmark.experiment_plan import ProgramTestPlan
from coding4game_gui.benchmark.experiment_plan import build_development_args
from coding4game_gui.benchmark.experiment_plan import build_program_test_args
from coding4game_gui.benchmark.experiment_plan import discover_benchmark_scenarios
from coding4game_gui.catalog import FORMAL_SCENARIOS, input_mode
from coding4game_gui.operator_config import LAUNCHER_DEFAULTS
from coding4game_gui.paths import SCENARIO_DIR
from scripts import game_tui

def scenario():
    return BenchmarkScenario("balatro","Example","windows",Path("example.yaml"),"balatro",10)

def fields(tmp_path):
    return dict(scenario=scenario(),profile=tmp_path/"profile.json",
                out_dir=tmp_path/"out",tic_budget=100,wall_seconds=60,
                windows_input="gamepad-kbm")

def test_all_registered_scenarios_preview_development_and_independent_test(tmp_path, monkeypatch):
    scenario_dir = tmp_path / "scenarios"
    shutil.copytree(SCENARIO_DIR, scenario_dir)
    (scenario_dir / "gta_v.yaml").write_text(
        "engine: windows\ndriver_game: gta_v\n", encoding="utf-8")
    found = discover_benchmark_scenarios(scenario_dir)
    assert {s.engine for s in found} == {"windows"}
    assert {s.slug for s in found} == set(FORMAL_SCENARIOS)
    driver_dir = tmp_path / "games"
    driver_dir.mkdir()
    expected_drivers = {s.driver_game for s in found}
    for name in expected_drivers | {"kof98_2p", "gta_v"}:
        (driver_dir / f"{name}.yaml").touch()
    monkeypatch.setattr(game_tui, "GAMES_DIR", driver_dir)
    monkeypatch.setattr(game_tui, "discover_benchmark_scenarios", lambda: found)
    assert {path.stem for path in game_tui.game_files()} == expected_drivers
    monkeypatch.setattr(run_codex, "execute", lambda *_args: pytest.fail("preview started a run"))
    for registered in found:
        mode = input_mode(registered.slug)
        common = fields(tmp_path) | {"scenario": registered, "windows_input": mode}
        develop = run_codex.main(build_development_args(DevelopmentPlan(
            **common, model="fixture", upstream_url="https://example.invalid/v1", token_limit=100)))
        final_test = run_codex.main(build_program_test_args(ProgramTestPlan(
            **common, source_programs=tmp_path / "programs", program_id="a" * 64)))
        for preview in (develop, final_test):
            assert preview["scenario"] == registered.slug
            assert preview["windows_input"] == mode
            assert preview["runtime_backend"] == "wsl2"
            assert preview["execute"] is False
        assert develop["billing_backend"] == "host-model-gateway"
        assert final_test["program_id"] == "a" * 64
        assert final_test["billing_backend"] is None
    assert not (tmp_path / "out").exists()
    retired = build_program_test_args(ProgramTestPlan(
        **fields(tmp_path), source_programs=tmp_path / "programs"))
    retired[retired.index("--scenario") + 1] = "gta_v"
    with pytest.raises(SystemExit) as failure:
        run_codex.main(retired)
    assert failure.value.code == 2

def test_development_preview_is_pure_and_has_no_execute(tmp_path):
    plan=DevelopmentPlan(**fields(tmp_path),model="test-model",
        upstream_url="https://example.invalid/codex/v1",token_limit=30000)
    preview=build_development_args(plan)
    assert preview[0]=="develop"
    assert "--execute" not in preview and "--confirm-paid-run" not in preview
    assert not (tmp_path/"out").exists()
    launch=build_development_args(plan,execute=True)
    assert launch[-2:]==["--execute","--confirm-paid-run"]
    assert "--arm" not in launch and "--holdout-episodes" not in launch

def test_final_plan_has_no_model_configuration(tmp_path):
    plan=ProgramTestPlan(**fields(tmp_path),source_programs=tmp_path/"programs")
    args=build_program_test_args(plan,execute=True)
    assert args[0]=="test" and args[-1]=="--execute"
    assert not any(x in args for x in ("--model","--token-limit","--confirm-paid-run"))

@pytest.mark.parametrize("url",["http://example.invalid/v1","https://key:secret@example.invalid/v1",
                               "https://example.invalid/v1?key=secret"])
def test_model_plan_rejects_credential_urls(tmp_path,url):
    with pytest.raises(ValueError,match="HTTPS"):
        build_development_args(DevelopmentPlan(**fields(tmp_path),model="m",
            upstream_url=url,token_limit=10))

def test_direct_tui_routes_to_shared_launcher(monkeypatch):
    calls = []
    monkeypatch.setattr(game_tui, "clear", lambda: None)
    monkeypatch.setattr(game_tui, "banner", lambda *_args: None)
    monkeypatch.setattr(game_tui, "load_preset", lambda _path: {"model": "test-model"})
    monkeypatch.setattr(game_tui, "field", lambda _label, default, **_kwargs: default)
    monkeypatch.setattr(game_tui, "int_field", lambda _label, default: default)
    monkeypatch.setattr(game_tui, "preview_and_run_launcher",
                        lambda script, args, _question: calls.append((script, args)))
    game_tui.launch_direct_baseline(scenario())
    assert calls == [("run_baseline.ps1", [
        "-Games", "balatro", "-Models", "test-model", "-Episodes", "3", "-TicBudget", "10000",
    ])]

@pytest.mark.parametrize("approved",[False,True])
def test_codex_tui_shows_exact_command_and_defaults_to_denial(tmp_path,monkeypatch,capsys,approved):
    monkeypatch.setattr(game_tui,"select_benchmark_scenario",lambda *a,**k:scenario())
    monkeypatch.setattr(game_tui,"menu",lambda *a,**k:0)
    monkeypatch.setattr(game_tui,"_current_program_fields",lambda s:fields(tmp_path))
    answers=iter(["test-model","https://example.invalid/v1","TEST_API_ENV"])
    monkeypatch.setattr(game_tui,"field",lambda *a,**k:next(answers))
    monkeypatch.setattr(game_tui,"int_field",lambda *a,**k:30000)
    checks=[]
    monkeypatch.setattr(game_tui,"confirm",lambda message,default:checks.append(default) or approved)
    launched=[]
    monkeypatch.setattr(game_tui,"run_python_module",lambda *args:launched.append(args))
    game_tui.launch_manual_development(scenario())
    output=capsys.readouterr().out
    assert BENCHMARK_MODULE in output and "--confirm-paid-run" in output
    assert "30000" in output and "TEST_API_ENV" in output
    assert "usage" in output and "may exceed" in output
    assert "--interactive-debug" not in output
    assert "--runtime-backend wsl2" in output
    assert checks==[False]
    assert bool(launched) is approved
    if approved:
        assert launched[0][0]==BENCHMARK_MODULE

def test_program_test_tui_requires_confirmation(tmp_path,monkeypatch):
    monkeypatch.setattr(game_tui,"select_benchmark_scenario",lambda *a,**k:scenario())
    monkeypatch.setattr(game_tui,"_current_program_fields",lambda s:fields(tmp_path))
    answers=iter([str(tmp_path/"programs"),""])
    monkeypatch.setattr(game_tui,"field",lambda *a,**k:next(answers))
    monkeypatch.setattr(game_tui,"confirm",lambda *a,**k:False)
    monkeypatch.setattr(game_tui,"run_python_module",lambda *a:pytest.fail("must not launch"))
    game_tui.launch_manual_submission_test()


@pytest.mark.parametrize("choice,source,script,args", [
    (0, "out/experiments/run1", "test_code.ps1", ["-RunDirs", "out/experiments/run1"]),
    (1, "", "test_code_remaining.ps1", []),
])
def test_independent_test_menu_routes_to_shared_launchers(monkeypatch, choice, source, script, args):
    calls = []
    monkeypatch.setattr(game_tui, "menu", lambda *a, **k: choice)
    monkeypatch.setattr(game_tui, "field", lambda *a, **k: source)
    monkeypatch.setattr(game_tui, "int_field", lambda _label, default: default)
    monkeypatch.setattr(game_tui, "preview_and_run_launcher", lambda name, flags, _question: calls.append((name, flags)))

    game_tui.final_tests_menu()

    assert calls == [(script, [*args, "-Repeats", "3"])]


def test_setup_menu_uses_the_same_installer_as_first_launch(monkeypatch):
    calls = []
    monkeypatch.setattr(game_tui, "menu", lambda *a, **k: 0)
    monkeypatch.setattr(game_tui, "preview_and_run_launcher", lambda name, args, _question: calls.append((name, args)))

    game_tui.host_setup_menu()

    assert calls == [("setup.ps1", [])]


@pytest.mark.parametrize("size", [(80, 20), (48, 12)])
def test_scenario_menu_keeps_the_selected_row_in_the_console(size, monkeypatch, capsys):
    items = [{"label": scenario.slug, "hint": scenario.title, "tag": "Windows"}
             for scenario in discover_benchmark_scenarios()]
    keys = iter(["up", "enter"])
    monkeypatch.setattr(game_tui, "_supports_keys", lambda: True)
    monkeypatch.setattr(game_tui, "read_key", lambda: next(keys))
    monkeypatch.setattr(game_tui.shutil, "get_terminal_size", lambda *_args: terminal_size(size))

    assert game_tui.menu("Run Windows experiment", items, subtitle="Registered scenarios") == len(items) - 1
    frames = capsys.readouterr().out.split("\x1b[2J\x1b[3J\x1b[H")[1:]
    assert items[0]["label"] in frames[0]
    assert items[-1]["label"] in frames[-1]
    for frame in frames:
        lines = frame.replace("\x1b[?25h", "").splitlines()
        assert len(lines) < size[1]
        assert all(game_tui.vlen(line) < size[0] for line in lines)


def test_menu_shows_why_no_items_are_available(monkeypatch, capsys):
    pauses = []
    monkeypatch.setattr(game_tui, "pause", lambda: pauses.append(True))
    assert game_tui.menu("Choose a scenario", [
        {"label": "Example", "disabled": True, "hint": "Missing checkpoint archive"},
    ]) is None
    assert "Missing checkpoint archive" in capsys.readouterr().out
    assert pauses == [True]


def test_numbered_menu_retries_until_an_available_item_is_selected(monkeypatch, capsys):
    answers = iter(["typo", "2", "99", "3"])
    monkeypatch.setattr(game_tui, "_supports_keys", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert game_tui.menu("Choose", [
        {"label": "First"}, {"label": "Unavailable", "disabled": True}, {"label": "Third"},
    ]) == 2
    assert "available number" in capsys.readouterr().out


def test_editing_settings_keeps_the_saved_input_mode_by_default(tmp_path, monkeypatch):
    values = dict(LAUNCHER_DEFAULTS, windows_input="gamepad")
    monkeypatch.setattr(game_tui, "LAUNCH_PRESET", tmp_path / "launcher.local.json")
    monkeypatch.setattr(game_tui, "field", lambda _label, default, **_kwargs: default)
    monkeypatch.setattr(game_tui, "int_field", lambda _label, default: default)
    monkeypatch.setattr(game_tui, "menu", lambda *_args, start_index=0: start_index)

    assert game_tui._edit_launch_preset(values)["windows_input"] == "gamepad"


def test_experiment_menu_selects_linux_codex(monkeypatch):
    chosen = scenario()
    monkeypatch.setattr(game_tui, "select_benchmark_scenario", lambda *a, **k: chosen)
    monkeypatch.setattr(game_tui, "menu", lambda *a, **k: 2)
    calls = []
    monkeypatch.setattr(game_tui, "launch_quick_development",
                        lambda *a, **kw: calls.append((a, kw)))
    game_tui.launch_experiment()
    assert calls == [((chosen,), {"agent": "codex"})]
