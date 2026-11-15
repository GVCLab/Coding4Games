"""No API calls: fake provider fixtures and intercepted runner launches only."""
import json
import os
from pathlib import Path

import pytest
import yaml

from coding4game_gui.operator_config import (
    LAUNCHER_DEFAULTS, load_provider_route, load_preset, save_preset,
)
from coding4game_gui.benchmark.experiment_plan import BenchmarkScenario
from scripts import game_tui


def provider(tmp_path, **overrides):
    params = dict(model="openai/gpt-5.6-terra", api_base="https://example.invalid/codex/v1",
                  api_key="fake-secret-do-not-print")
    params.update(overrides)
    path = tmp_path / "provider.yaml"
    path.write_text(yaml.safe_dump(dict(model_list=[dict(model_name="alias", litellm_params=params)])))
    return path


def test_select_actual_model_and_alias(tmp_path):
    path = provider(tmp_path)
    assert load_provider_route(path, "alias") == load_provider_route(path, "gpt-5.6-terra")
    assert load_provider_route(path, "alias").model == "gpt-5.6-terra"


def test_provider_route_exposes_chat_mode(tmp_path):
    path = provider(tmp_path)
    data = yaml.safe_load(path.read_text())
    data["model_list"][0]["model_info"] = {"mode": "chat"}
    path.write_text(yaml.safe_dump(data))

    route = load_provider_route(path, "alias")
    assert route.model == "gpt-5.6-terra"
    assert route.provider_model == "openai/gpt-5.6-terra"
    assert route.api_mode == "chat"


def test_provider_route_rejects_unknown_api_mode_without_disclosing_key(tmp_path):
    path = provider(tmp_path)
    data = yaml.safe_load(path.read_text())
    data["model_list"][0]["model_info"] = {"mode": "not-a-real-api"}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError) as error:
        load_provider_route(path, "alias")
    assert "fake-secret-do-not-print" not in str(error.value)


def test_explicit_non_openai_route_support_preserves_strict_default(tmp_path):
    path = provider(
        tmp_path,
        model="gemini/gemini-3.8-flash",
        api_base="https://example.invalid/gemini",
    )
    with pytest.raises(ValueError):
        load_provider_route(path, "alias")
    route = load_provider_route(path, "alias", allow_non_openai=True)
    assert route.model == "gemini-3.8-flash"
    assert route.api_base == "https://example.invalid/gemini"


def test_environment_reference(tmp_path, monkeypatch):
    path = provider(tmp_path, api_key="os.environ/FAKE_LAUNCH_TEST_KEY")
    monkeypatch.setenv("FAKE_LAUNCH_TEST_KEY", "fake-value")
    assert load_provider_route(path, "alias").api_key == "fake-value"
    monkeypatch.delenv("FAKE_LAUNCH_TEST_KEY")
    with pytest.raises(ValueError):
        load_provider_route(path, "alias")


@pytest.mark.parametrize("changes", [dict(api_base="http://example.invalid"),
    dict(api_base="https://user:fake-secret-do-not-print@example.invalid"),
    dict(api_base="https://example.invalid?key=fake-secret-do-not-print"),
    dict(api_key=""), dict(model="other/model"), dict(api_key=123)])
def test_provider_errors_do_not_disclose_values(tmp_path, changes):
    with pytest.raises(ValueError) as error:
        load_provider_route(provider(tmp_path, **changes), "alias")
    assert "fake-secret-do-not-print" not in str(error.value)


@pytest.mark.parametrize("contents", ["bad: [fake-secret-do-not-print", "null", "[]"])
def test_malformed_provider_is_sanitized(tmp_path, contents):
    path = tmp_path / "bad.yaml"
    path.write_text(contents)
    with pytest.raises(ValueError) as error:
        load_provider_route(path, "alias")
    assert "fake-secret-do-not-print" not in str(error.value)


def test_duplicate_or_missing_model_fails_closed(tmp_path):
    path = provider(tmp_path)
    with pytest.raises(ValueError):
        load_provider_route(path, "missing")
    data = yaml.safe_load(path.read_text())
    data["model_list"] *= 2
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        load_provider_route(path, "alias")


def test_preset_roundtrip_and_no_key_field(tmp_path):
    path = tmp_path / "local.json"
    values = dict(LAUNCHER_DEFAULTS, profile="test-profile.json")
    save_preset(path, values)
    assert load_preset(path) == values
    with pytest.raises(ValueError):
        save_preset(path, dict(values, api_key="fake"))
    assert "api_key" not in path.read_text()


@pytest.mark.parametrize("preview_status,approved", [(0, False), (0, True), (2, True)])
def test_configured_development_previews_shared_launcher_before_confirmation(
        tmp_path, monkeypatch, capsys, preview_status, approved):
    path = provider(tmp_path)
    profile = tmp_path / "profile.json"
    profile.write_text("{}")
    preset = tmp_path / "launcher.json"
    save_preset(preset, dict(LAUNCHER_DEFAULTS, profile=str(profile), credentials=str(path)))
    monkeypatch.setattr(game_tui, "LAUNCH_PRESET", preset)
    monkeypatch.setattr(game_tui, "menu", lambda *a, **kw: 0)
    monkeypatch.setattr(game_tui, "field", lambda *a, **kw: pytest.fail("No manual fields"))
    confirmations = []
    def confirm(message, default):
        confirmations.append(default)
        return approved
    monkeypatch.setattr(game_tui, "confirm", confirm)
    commands = []

    def run_command(command):
        commands.append(command)
        return preview_status

    monkeypatch.setattr(game_tui.shutil, "which", lambda _name: "pwsh")
    monkeypatch.setattr(game_tui, "run_command", run_command)
    monkeypatch.setattr(game_tui, "pause", lambda: None)
    before = dict(os.environ)
    scenario = BenchmarkScenario("balatro", "Balatro", "windows", Path("unused"), "balatro", 10)
    game_tui.launch_quick_development(scenario)
    assert confirmations == ([False] if preview_status == 0 else [])
    preview = commands[0]
    assert Path(preview[3]).name == "run_code.ps1"
    assert preview[preview.index("-ProviderConfig") + 1] == str(path)
    assert preview[preview.index("-WindowsInput") + 1] == "auto"
    assert "-Execute" not in preview
    assert all("fake-secret-do-not-print" not in str(command) for command in commands)
    assert commands[1:] == ([preview + ["-Execute"]] if preview_status == 0 and approved else [])
    assert dict(os.environ) == before
    assert "fake-secret-do-not-print" not in capsys.readouterr().out
    assert "fake-secret-do-not-print" not in preset.read_text()


def test_missing_config_cannot_launch_even_with_forced_menu_selection(tmp_path, monkeypatch):
    preset = tmp_path / "launcher.json"
    save_preset(preset, dict(LAUNCHER_DEFAULTS, profile=str(tmp_path / "missing")))
    monkeypatch.setattr(game_tui, "LAUNCH_PRESET", preset)
    monkeypatch.setattr(game_tui, "menu", lambda *a, **kw: 0)
    monkeypatch.setattr(game_tui, "confirm", lambda *a, **kw: pytest.fail("Invalid setup"))
    game_tui.launch_quick_development(None)
