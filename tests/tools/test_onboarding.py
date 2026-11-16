"""Exercise clean-checkout setup and path selection without models or desktop input."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

import pytest

from coding4game_gui.operator_config import LAUNCHER_DEFAULTS, initialize, load_preset, load_provider_route, operator_paths
from coding4game_gui.benchmark.windows.local_paths import read_driver_data

ROOT = Path(__file__).resolve().parents[2]


def test_initialize_preserves_operator_files_and_resolves_from_checkout(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "docs" / "examples", tmp_path / "docs" / "examples")
    assert set(initialize(tmp_path)) == {"launcher.local.json", "litellm.config.yaml", "games.local.json"}
    assert load_preset(tmp_path / "launcher.local.json")["output_root"] == "out/experiments"
    provider = tmp_path / "litellm.config.yaml"
    monkeypatch.setenv("C4G_MODEL_API_KEY", "test-key")
    alias = load_preset(tmp_path / "launcher.local.json")["model"]
    route = load_provider_route(provider, alias, allow_non_openai=True)
    assert route.provider_model.startswith("anthropic/")
    assert route.api_base == "https://api.anthropic.com"
    provider.write_text("operator-owned bytes", encoding="utf-8")
    assert initialize(tmp_path) == []
    assert provider.read_text() == "operator-owned bytes"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert operator_paths(tmp_path)["profile"] == tmp_path / "runtime" / "profile.json"
    assert operator_paths(tmp_path)["output_root"] == tmp_path / "out" / "experiments"


def test_path_preferences_keep_legacy_presets_and_explicit_roots(tmp_path):
    absolute = tmp_path / "custom-runs"
    (tmp_path / "launcher.local.json").write_text(json.dumps(dict(
        LAUNCHER_DEFAULTS, profile="custom.json", output_root=str(absolute))))
    assert operator_paths(tmp_path)["output_root"] == absolute
    assert operator_paths(tmp_path)["profile"] == tmp_path / "custom.json"
    preset = {key: value for key, value in LAUNCHER_DEFAULTS.items() if key != "output_root"}
    (tmp_path / "launcher.local.json").write_text(json.dumps(dict(preset, profile="old.json")))
    assert operator_paths(tmp_path)["output_root"] == tmp_path / "out" / "experiments"


@pytest.mark.parametrize("payload", [[], {"profile": 12}, {"credentials": "\nprivate-value"}])
def test_invalid_operator_paths_fail_without_echoing_values(tmp_path, payload):
    (tmp_path / "launcher.local.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError) as error:
        operator_paths(tmp_path)
    assert "private-value" not in str(error.value)


def driver_fixture(tmp_path):
    games = tmp_path / "games"
    games.mkdir()
    path = games / "example.yaml"
    path.write_text("auto_launch:\n  launch_path: C:/old/Game.exe\n  settle_seconds: 60\nsave_state:\n  restore_to: C:/old/save\nmenu_skip: [unchanged]\nactions: {neutral: {description: Neutral}}\n")
    return path


def test_local_game_paths_preserve_registered_actions_and_timing(tmp_path):
    path = driver_fixture(tmp_path)
    original = path.read_bytes()
    (tmp_path / "games.local.json").write_text(json.dumps({"example": {
        "auto_launch": {"launch_path": "D:/Games/Game.exe"},
        "save_state": {"restore_to": "D:/approved-task-save"}}}))
    effective = read_driver_data(path)
    assert effective["auto_launch"] == {"launch_path": "D:/Games/Game.exe", "settle_seconds": 60}
    assert effective["save_state"]["restore_to"] == "D:/approved-task-save"
    assert effective["menu_skip"] == ["unchanged"]
    assert effective["actions"] == {"neutral": {"description": "Neutral"}}
    assert path.read_bytes() == original


@pytest.mark.parametrize("override", [
    {"actions": {"neutral": {}}}, {"auto_launch": {"settle_seconds": 0}},
    {"save_state": {"golden": "some-other-task.zip"}}, {"scorer": {"score": 999}},
])
def test_host_path_overrides_cannot_change_experiment_protocol(tmp_path, override):
    path = driver_fixture(tmp_path)
    (tmp_path / "games.local.json").write_text(json.dumps({"example": override}))
    with pytest.raises(ValueError, match="only auto_launch"):
        read_driver_data(path)


@pytest.fixture
def setup_launcher(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if sys.platform != "win32" or shell is None:
        pytest.skip("Windows PowerShell is required")
    checkout = tmp_path / "checkout with spaces"
    scripts = checkout / "scripts"
    (scripts / "launchers").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/start.ps1", scripts / "start.ps1")
    shutil.copyfile(ROOT / "scripts/launchers/setup.ps1", scripts / "launchers/setup.ps1")
    return checkout, [shell, "-NoProfile", "-File", str(scripts / "start.ps1")]


def test_first_launch_previews_setup_from_another_directory(tmp_path, setup_launcher):
    checkout, command = setup_launcher
    if (sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 11)
            or sysconfig.get_platform() != "win-amd64"):
        pytest.skip("Python 3.11 x64 is required")
    env = dict(os.environ, PATH=str(Path(sys.executable).parent))
    result = subprocess.run([*command, "-Preview"], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Preview only" in result.stdout
    assert "Python 3.11." in result.stdout
    assert 'pip install -e "."' in result.stdout
    assert str(checkout / ".venv") in result.stdout
    assert not (checkout / ".venv").exists()
    assert not (checkout / "launcher.local.json").exists()


def test_setup_preview_forwards_optional_ocr_dependencies(tmp_path, setup_launcher):
    checkout, command = setup_launcher
    if (sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 11)
            or sysconfig.get_platform() != "win-amd64"):
        pytest.skip("Python 3.11 x64 is required")
    result = subprocess.run([*command, "-Setup", "-Dev", "-Ocr", "-Preview",
                             "-Python", sys.executable],
                            cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'pip install -e ".[dev,ocr]"' in result.stdout
    assert not (checkout / ".venv").exists()


@pytest.mark.parametrize("arguments", [[], ["-Setup"]])
def test_preview_rejects_missing_explicit_python(tmp_path, setup_launcher, arguments):
    checkout, command = setup_launcher
    result = subprocess.run([*command, *arguments, "-Preview",
                             "-Python", str(tmp_path / "missing-python.exe")],
                            cwd=tmp_path, capture_output=True, timeout=20)
    assert result.returncode != 0
    assert b"Supply the full path" in result.stderr
    assert not (checkout / ".venv").exists()


def test_setup_preview_without_a_python_explains_how_to_choose_one(tmp_path, setup_launcher):
    checkout, command = setup_launcher
    result = subprocess.run([*command, "-Setup", "-Preview"], cwd=tmp_path,
                            env=dict(os.environ, PATH=""), capture_output=True, timeout=20)
    assert result.returncode != 0
    assert b"Python 3.11 x64 (CPython) was not found" in result.stderr
    assert b"-Python C:/path/to/python.exe" in result.stderr
    assert not (checkout / ".venv").exists()


def test_setup_preview_explains_and_preserves_incomplete_venv(tmp_path, setup_launcher):
    checkout, command = setup_launcher
    (checkout / ".venv").mkdir()
    marker = checkout / ".venv" / "keep.txt"
    marker.write_text("evidence", encoding="utf-8")
    result = subprocess.run([*command, "-Setup", "-Preview"], cwd=tmp_path,
                            capture_output=True, timeout=20)
    assert result.returncode != 0
    assert b"Incomplete environment" in result.stderr
    assert marker.read_text(encoding="utf-8") == "evidence"


def test_tui_missing_dependencies_explains_how_to_prepare_python():
    result = subprocess.run([sys.executable, "-S", str(ROOT / "scripts/game_tui.py")],
                            cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "PyYAML" in result.stderr
    assert "./scripts/start.ps1 -Setup" in result.stderr
    assert "-Python" in result.stderr


def test_tui_missing_dependency_after_yaml_import_explains_setup(tmp_path):
    (tmp_path / "yaml.py").write_text("", encoding="utf-8")
    result = subprocess.run([sys.executable, "-S", str(ROOT / "scripts/game_tui.py")],
                            env=dict(os.environ, PYTHONPATH=str(tmp_path)), cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "psutil could not be imported" in result.stderr
    assert "./scripts/start.ps1 -Setup" in result.stderr
    assert "Traceback" not in result.stderr


def test_wsl_setup_preview_uses_the_checkout_provisioner(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell is required")
    scripts = tmp_path / "checkout with spaces" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/start.ps1", scripts / "start.ps1")
    (scripts / "setup_wsl2_runtime.py").write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(scripts / "start.ps1"), "-SetupWsl2",
         "-Preview", "-Python", sys.executable],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == ["--first-run", "--distro", "C4G-WSL2-Base-LOCAL"]
