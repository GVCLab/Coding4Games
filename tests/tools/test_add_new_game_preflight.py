import importlib.util
from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "calibration" / "add_new_game_preflight.py"


def _module():
    assert SCRIPT.is_file()
    spec = importlib.util.spec_from_file_location("add_new_game_preflight", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_direct_preflight_uses_its_own_checkout(tmp_path):
    foreign = tmp_path / "foreign" / "coding4game_gui"
    foreign.mkdir(parents=True)
    (foreign / "__init__.py").write_text(
        "raise RuntimeError('foreign checkout imported')", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--game", "brotato", "--json"],
        cwd=tmp_path, env=dict(os.environ, PYTHONPATH=str(foreign.parent)),
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    report = json.loads(result.stdout)
    assert report['game'] == 'brotato'
    assert 'foreign checkout imported' not in result.stdout


def _driver(path: Path, save_dir: Path, *, complete: bool = False):
    (path / "games").mkdir(parents=True)
    executable = path / "Demo.exe"
    executable.write_bytes(b"MZ")
    data = {
        "name": "Demo",
        "process": "Demo.exe",
        "auto_launch": {"launch_path": str(executable)},
        "scorer": {"kind": "ocr", "implementation": "ocr"},
        "actions": {
            "confirm": {"description": "Confirm", "buttons": ["A"]},
        },
        "save_state": {"restore_to": str(save_dir)},
        "menu_skip": [],
    }
    if complete:
        data["save_state"]["golden"] = str(path / "snapshots" / "demo_golden.zip")
        data["menu_skip"] = [
            {"description": "into run", "buttons": ["A"]},
        ]
    (path / "games" / "demo.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False),
        encoding="utf-8",
    )


def _benchmark(path: Path, *, extra: dict | None = None):
    bench_dir = path / "coding4game_gui" / "benchmark" / "scenarios"
    bench_dir.mkdir(parents=True)
    data = {
        "title": "Demo",
        "engine": "windows",
        "driver_game": "demo",
        "gameplay": "Play",
        "objective": "Score",
        "episode_contract": "One cold start",
        "runtime": {"tics_per_second": 10},
        "startup": {
            "golden_checkpoint": "golden",
            "menu_skip_replay": "closed_loop",
            "start_check": {"pattern": "READY"},
        },
        "lifecycle": {"progress_phases": ["play"]},
        "scorer": {
            "kind": "ocr", "implementation": "ocr",
            "calibrated_client_size": [1280, 720],
            "phases": {"play": {}, "done": {}},
            "terminal_phases": ["done"],
        },
    }
    data.update(extra or {})
    (bench_dir / "demo.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_audit_separates_auto_work_from_manual_gates(tmp_path):
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    _driver(tmp_path, save_dir)

    report = _module().audit_game(tmp_path, "demo")
    by_id = {item["id"]: item for item in report["checks"]}

    assert by_id["bench_spec"]["status"] == "auto"
    assert by_id["menu_skip"]["status"] == "manual"
    assert by_id["golden_checkpoint"]["status"] == "manual"
    assert by_id["driver_yaml"]["status"] == "pass"
    assert report["summary"]["error"] == 0


def test_disabled_detector_does_not_request_irrelevant_terminal_screens(tmp_path):
    save_dir = tmp_path / 'save'
    save_dir.mkdir()
    _driver(tmp_path, save_dir)
    _benchmark(tmp_path, extra={
        'lifecycle': {'terminal_detection': False},
        'scorer': {'calibrated_client_size': [1280,720], 'terminal_phases': []},
    })
    report = _module().audit_game(tmp_path, 'demo')
    check = next(row for row in report['checks'] if row['id'] == 'terminal_phases')
    assert check['status'] == 'pass'
    assert 'episode limit' in check['detail']


def test_complete_fixture_has_no_manual_gates(tmp_path):
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    (snapshots / "demo_golden.zip").write_bytes(b"zip")
    _driver(tmp_path, save_dir, complete=True)
    report = _module().audit_game(tmp_path, "demo")
    by_id = {item["id"]: item for item in report["checks"]}
    assert by_id["golden_checkpoint"]["status"] == "pass"
    assert by_id["menu_skip"]["status"] == "pass"
    assert "scorer_calibration" not in by_id


def test_legacy_steam_uri_is_an_error(tmp_path):
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    _driver(tmp_path, save_dir)
    driver_path = tmp_path / "games" / "demo.yaml"
    data = yaml.safe_load(driver_path.read_text(encoding="utf-8"))
    data["auto_launch"] = {"steam_url": "steam://rungameid/1"}
    driver_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    report = _module().audit_game(tmp_path, "demo")
    by_id = {item["id"]: item for item in report["checks"]}
    assert by_id["auto_launch"]["status"] == "error"


def test_no_save_game_does_not_require_golden_checkpoint(tmp_path):
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    _driver(tmp_path, save_dir)
    driver_path = tmp_path / "games" / "demo.yaml"
    data = yaml.safe_load(driver_path.read_text(encoding="utf-8"))
    data.pop("save_state")
    driver_path.write_text(yaml.safe_dump(data, sort_keys=False),
                           encoding="utf-8")

    report = _module().audit_game(tmp_path, "demo")
    by_id = {item["id"]: item for item in report["checks"]}

    assert by_id["save_directory"]["status"] == "pass"
    assert by_id["golden_checkpoint"]["status"] == "pass"
    assert "not required" in by_id["golden_checkpoint"]["detail"]


def test_explicit_process_only_is_static_configuration_not_live_acceptance(tmp_path):
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    _driver(tmp_path, save_dir, complete=True)
    driver_path = tmp_path / "games/demo.yaml"
    data = yaml.safe_load(driver_path.read_text())
    data.pop("save_state")
    driver_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    _benchmark(tmp_path, extra={"startup": {"no_save_cleanup": "process_kill",
        "start_check": {"pattern": "READY"}}})
    report = _module().audit_game(tmp_path, "demo")
    by_id = {item["id"]: item for item in report["checks"]}
    assert report["config_ready"]
    assert by_id["state_preparation_contract"]["status"] == "pass"
    assert by_id["cold_start_lifecycle"]["status"] == "manual"
    assert by_id["start_check"]["status"] == "manual"


def test_process_only_preflight_cannot_mask_a_missing_checkpoint(tmp_path):
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    _driver(tmp_path, save_dir, complete=True)
    _benchmark(tmp_path, extra={"startup": {"no_save_cleanup": "process_kill",
        "golden_checkpoint": "golden"}})
    report = _module().audit_game(tmp_path, "demo")
    by_id = {item["id"]: item for item in report["checks"]}
    assert not report["config_ready"]
    assert by_id["state_preparation_contract"]["status"] == "error"


def test_config_ready_is_static_and_live_evidence_remains_manual(tmp_path, capsys):
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / "demo_golden.zip").write_bytes(b"zip")
    _driver(tmp_path, save_dir, complete=True)
    _benchmark(tmp_path)

    mod = _module()
    report = mod.audit_game(tmp_path, "demo")
    mod._print_human(report)
    output = capsys.readouterr().out

    assert report["config_ready"] is True, report
    assert report["ready"] is True
    assert report["readiness_scope"] == "static_configuration"
    assert report["summary"]["manual"] > 0
    assert "CONFIG READY" in output
    assert "static configuration only" in output


def test_static_contract_rejects_action_durations_and_extra_bench_keys(tmp_path):
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    _driver(tmp_path, save_dir)
    driver_path = tmp_path / "games" / "demo.yaml"
    driver = yaml.safe_load(driver_path.read_text(encoding="utf-8"))
    driver["actions"]["confirm"]["duration_seconds"] = 0.1
    driver_path.write_text(yaml.safe_dump(driver), encoding="utf-8")
    _benchmark(tmp_path, extra={"obsolete": True})

    report = _module().audit_game(tmp_path, "demo")
    by_id = {item["id"]: item for item in report["checks"]}

    assert by_id["action_duration_contract"]["status"] == "error"
    assert by_id["bench_contract"]["status"] == "error"
    assert report["config_ready"] is False


def _timing_fixture(tmp_path, monkeypatch, *, machine=0x14C):
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots/demo_golden.zip").write_bytes(b"zip")
    _driver(tmp_path, save_dir, complete=True)
    _benchmark(tmp_path)
    driver_path = tmp_path / "games/demo.yaml"
    driver = yaml.safe_load(driver_path.read_text(encoding="utf-8"))
    driver["use_speedhack"] = True
    driver_path.write_text(yaml.safe_dump(driver), encoding="utf-8")
    header = bytearray(70)
    header[:2] = b"MZ"
    header[60:64] = (64).to_bytes(4, "little")
    header[64:68] = b"PE\0\0"
    header[68:70] = machine.to_bytes(2, "little")
    (tmp_path / "Demo.exe").write_bytes(header)
    package_root = tmp_path / "site-packages"
    binaries = package_root / "xspeedhack/bin"
    binaries.mkdir(parents=True)
    for name in ["injector32.exe", "speedhack32.dll", "injector64.exe", "speedhack64.dll"]:
        (binaries / name).write_bytes(b"test resource; never executed")
    monkeypatch.setattr(metadata, "distribution", lambda name: SimpleNamespace(
        locate_file=lambda relative: package_root / relative))
    return binaries


@pytest.mark.parametrize("missing", ["injector32.exe", "speedhack32.dll"])
def test_required_x86_timing_resource_blocks_static_readiness(tmp_path, monkeypatch, missing):
    binaries = _timing_fixture(tmp_path, monkeypatch)
    (binaries / missing).unlink()
    report = _module().audit_game(tmp_path, "demo")
    assert report["config_ready"] is False
    check = next(row for row in report["checks"] if row["id"] == "time_control_resources")
    assert check["status"] == "error"
    assert missing in check["detail"]
    assert "x86" in check["detail"]


def test_x64_timing_uses_only_matching_architecture_resources(tmp_path, monkeypatch):
    binaries = _timing_fixture(tmp_path, monkeypatch, machine=0x8664)
    (binaries / "injector32.exe").unlink()
    report = _module().audit_game(tmp_path, "demo")
    assert report["config_ready"] is True
    check = next(row for row in report["checks"] if row["id"] == "time_control_resources")
    assert check["status"] == "pass"
    assert "x64" in check["detail"]


def test_missing_timing_package_blocks_static_readiness(tmp_path, monkeypatch):
    _timing_fixture(tmp_path, monkeypatch)

    def missing_distribution(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "distribution", missing_distribution)
    report = _module().audit_game(tmp_path, "demo")
    assert report["config_ready"] is False
    check = next(row for row in report["checks"] if row["id"] == "time_control_resources")
    assert check["status"] == "error"
    assert "xspeedhack" in check["detail"]


@pytest.mark.parametrize("executable", [b"MZ", b"not a PE executable"])
def test_unknown_timing_architecture_cannot_claim_static_readiness(tmp_path, monkeypatch, executable):
    _timing_fixture(tmp_path, monkeypatch)
    (tmp_path / "Demo.exe").write_bytes(executable)
    report = _module().audit_game(tmp_path, "demo")
    assert report["config_ready"] is False
    check = next(row for row in report["checks"] if row["id"] == "time_control_resources")
    assert check["status"] == "error"
