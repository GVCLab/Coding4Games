"""Reject retired runtime inputs before any model, game or WSL side effect."""
import json
from types import SimpleNamespace

import pytest

from coding4game_gui.benchmark.code import run_codex as runner
from coding4game_gui.benchmark.code.submissions import ProgramStore
from coding4game_gui.benchmark import __main__ as public_cli


def arguments(tmp_path, command="develop"):
    values = [command, "--profile", str(tmp_path / "profile.json"),
              "--scenario", "balatro", "--out-dir", str(tmp_path / "out"),
              "--tic-budget", "10", "--wall-seconds", "60"]
    if command == "develop":
        values += ["--model", "test-only", "--upstream-url", "https://example.invalid/v1",
                   "--token-limit", "100"]
    else:
        values += ["--source-programs", str(tmp_path / "programs")]
    return values


def test_public_development_defaults_to_wsl_claude(tmp_path):
    args = runner.parser().parse_args(arguments(tmp_path))
    plan = runner._plan(args)
    assert (plan["runtime_backend"], plan["agent"]) == ("wsl2", "claude")
    assert "clean WSL clone" in " ".join(plan["side_effects"])
    assert not (tmp_path / "out_input-gamepad").exists()


@pytest.mark.parametrize("option,value", [
    ("--runtime-backend", "windows-native"), ("--agent", "gemini")])
def test_parser_rejects_unsupported_runtime_and_agent(tmp_path, option, value):
    with pytest.raises(SystemExit) as error:
        runner.parser().parse_args(arguments(tmp_path) + [option, value])
    assert error.value.code != 0


@pytest.mark.parametrize("override", [
    {"runtime_backend": "windows-native"}, {"interactive_debug": True},
    {"resume_checkpoint": {"config": {}}}, {"agent": "gemini"}])
def test_programmatic_execution_cannot_bypass_runtime_selection(tmp_path, override):
    args = runner.parser().parse_args(arguments(tmp_path))
    vars(args).update(override)
    with pytest.raises(ValueError):
        runner.execute(args, deps={})
    assert not (tmp_path / "out_input-gamepad").exists()


@pytest.mark.parametrize("platform", [None, "windows"])
def test_old_program_platform_rejected_before_runtime_setup(tmp_path, platform):
    store = ProgramStore(tmp_path / "programs")
    record = store.save({"main.py": b"pass"},
                        {"argv": ["main.py"], "mode": "stepped", "runtime": "python"},
                        execution_platform=platform)
    store.finish(record["id"])
    args = runner.parser().parse_args(arguments(tmp_path, "test"))
    with pytest.raises(ValueError, match="frozen Linux Python"):
        runner.execute(args, deps={"ProgramStore": ProgramStore})
    assert not (tmp_path / "out_input-gamepad").exists()
    assert store.final()["id"] == record["id"]


@pytest.mark.parametrize("mode,runtime,message", [
    ("live", "python", "mode must be stepped"),
    ("stepped", "native", "supports Python"),
])
def test_retired_program_contract_is_rejected_before_runtime_setup(tmp_path, mode, runtime, message):
    class Archive:
        def __init__(self, *_args, **_kwargs):
            pass

        def final(self):
            return self.get("example")[0]

        def get(self, program_id):
            return {"id": program_id, "execution_platform": "linux",
                    "entrypoint": {"mode": mode, "runtime": runtime}}, {}

    args = runner.parser().parse_args(arguments(tmp_path, "test"))
    with pytest.raises(ValueError, match=message):
        runner.execute(args, deps={"ProgramStore": Archive})
    assert not (tmp_path / "out_input-gamepad").exists()


def test_public_cli_lists_the_three_benchmark_workflows(capsys):
    public_cli.main(["--help"])
    assert "{develop,test,direct}" in capsys.readouterr().out


def test_default_dependency_graph_has_only_wsl_runtime():
    deps = runner._dependencies()
    assert deps["WslProfile"].backend == "wsl2"
    assert "OpenRuntime" not in deps and "run_agent_debug" not in deps


def test_single_launcher_rejects_old_profile_without_starting_process(tmp_path, monkeypatch, capsys):
    from scripts import run_single_experiment as single
    profile = tmp_path / "old-profile.json"
    profile.write_text(json.dumps({"format": "retired-windows-runtime-v1"}))
    monkeypatch.setattr(single, "load_provider_route", lambda *a, **k: SimpleNamespace(
        model="test-only", api_base="https://example.invalid/v1", api_key="test-secret",
        provider_model="anthropic/test-only", api_mode="responses"))
    monkeypatch.setattr(single, "load_game_spec", lambda *a, **k: {"runtime": {}})
    monkeypatch.setattr(single.subprocess, "run", lambda *a, **k: pytest.fail("process started"))
    assert single.main(["--game", "balatro", "--model", "test-only", "--profile", str(profile)]) == 2
    assert "Invalid WSL2 runtime profile" in capsys.readouterr().out
