"""Static diagnosis uses local metadata only and does not echo provider secrets."""
import hashlib
import json

import pytest

from coding4game_gui import doctor
from coding4game_gui.benchmark.windows import save_verification
from coding4game_gui.benchmark.windows.save_verification import PALWORLD_TOOLKIT_FILES
from scripts.calibration import add_new_game_preflight


def host_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.metadata, "version", lambda package: "installed")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "C:/Windows/wsl.exe")
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    monkeypatch.setattr(doctor.sysconfig, "get_platform", lambda: "win-amd64")
    baseline = tmp_path / "baseline.tar"
    baseline.write_bytes(b"fixed baseline fixture")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "format": "c4g-wsl2-v1", "id": "0123456789ab",
        "root": str(tmp_path / "runtime-0123456789ab"), "baseline": str(baseline),
        "baseline_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
        "user": "c4gprobe", "uid": 1000, "python": "/usr/bin/python3",
        "agents": {"claude": "/opt/c4g-tools/claude"},
        "ports": dict(zip(("development_control", "development_game", "final_test_control",
                            "final_test_game", "gateway"), range(15001, 15006))),
    }))
    (tmp_path / "launcher.local.json").write_text(json.dumps({"profile": str(profile)}))
    (tmp_path / "litellm.config.yaml").write_text("""model_list:
  - model_name: test-route
    litellm_params:
      model: openai/test-model
      api_base: https://provider.example.test/v1
      api_key: test-secret-never-echo
""")
    return baseline


def test_doctor_verifies_local_hash_without_starting_processes(tmp_path, monkeypatch):
    host_fixture(tmp_path, monkeypatch)
    def forbidden(*args, **kwargs):
        raise AssertionError("Static diagnosis must not start a process")
    monkeypatch.setattr("subprocess.run", forbidden)
    report = doctor.diagnose(tmp_path, model="test-route", verify_baseline=True)
    assert report["static_checks_pass"] is True
    assert report["real_game_acceptance_performed"] is False
    assert "test-secret-never-echo" not in json.dumps(report)
    assert any(item["id"] == "baseline_hash" and item["status"] == "pass" for item in report["checks"])
    assert not (tmp_path / "out").exists()


def test_doctor_rejects_missing_agent_and_modified_baseline(tmp_path, monkeypatch):
    baseline = host_fixture(tmp_path, monkeypatch)
    baseline.write_bytes(b"modified")
    report = doctor.diagnose(tmp_path, verify_baseline=True)
    assert report["static_checks_pass"] is False
    assert any(item["id"] == "baseline_hash" and item["status"] == "blocked" for item in report["checks"])
    report = doctor.diagnose(tmp_path, agent="codex")
    assert any(item["id"] == "profile" and item["status"] == "blocked" for item in report["checks"])


@pytest.mark.parametrize(("game", "driver", "needs_ocr"), [
    ("kof98_terminal", "kof98", False),
    ("aimlab", "aimlab", True),
    ("battleblock", "battleblock", True),
])
def test_doctor_checks_ocr_only_for_selected_scenarios(tmp_path, monkeypatch, game, driver, needs_ocr):
    host_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(add_new_game_preflight, "audit_game", lambda *_: {"config_ready": True})
    monkeypatch.setattr(doctor, "_host_packages", lambda *, ocr=False: ["paddleocr"] if ocr else [])

    def installed(package):
        raise doctor.metadata.PackageNotFoundError(package)

    monkeypatch.setattr(doctor.metadata, "version", installed)
    scenario = tmp_path / "coding4game_gui" / "benchmark" / "scenarios" / f"{game}.yaml"
    scenario.parent.mkdir(parents=True)
    scenario.write_bytes((doctor.SCENARIO_DIR / f"{game}.yaml").read_bytes())
    driver_path = tmp_path / "games" / f"{driver}.yaml"
    driver_path.parent.mkdir()
    driver_path.write_text("name: Demo\n", encoding="utf-8")
    report = doctor.diagnose(tmp_path, game=game)
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["dependencies"]["status"] == "pass"
    if needs_ocr:
        assert checks["game_ocr_dependencies"]["status"] == "blocked"
        assert "./scripts/start.ps1 -Setup -Ocr" in checks["game_ocr_dependencies"]["detail"]
    else:
        assert "game_ocr_dependencies" not in checks


def test_doctor_checks_palworld_toolkit_declared_in_lifecycle(tmp_path, monkeypatch):
    host_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(add_new_game_preflight, "audit_game", lambda *_: {"config_ready": True})
    game = "palworld_capture_penking"
    scenario = tmp_path / "coding4game_gui" / "benchmark" / "scenarios" / f"{game}.yaml"
    scenario.parent.mkdir(parents=True)
    scenario.write_bytes((doctor.SCENARIO_DIR / f"{game}.yaml").read_bytes())
    driver = tmp_path / "games" / "palworld_capture.yaml"
    driver.parent.mkdir()
    driver.write_text("name: Palworld\n", encoding="utf-8")

    def dependency_status():
        report = doctor.diagnose(tmp_path, game=game)
        return {check["id"]: check["status"] for check in report["checks"]
                if check["id"].startswith("game_save_")}

    assert dependency_status() == {"game_save_toolkit": "blocked", "game_save_inspector": "pass"}
    for name in PALWORLD_TOOLKIT_FILES:
        resource = tmp_path / "out" / "third_party" / "palworld-save-toolkit" / name
        resource.parent.mkdir(parents=True, exist_ok=True)
        resource.touch()
    assert dependency_status() == {"game_save_toolkit": "pass", "game_save_inspector": "pass"}
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None if name == "node" else "C:/Windows/wsl.exe")
    monkeypatch.setattr(save_verification, "PALWORLD_INSPECTOR_SCRIPT", tmp_path / "missing.mjs")
    assert dependency_status() == {"game_save_toolkit": "blocked", "game_save_inspector": "blocked"}
