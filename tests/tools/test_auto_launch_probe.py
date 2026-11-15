import json
from types import SimpleNamespace

import pytest

from scripts.calibration import test_auto_launch_dry as probe


def test_failed_capture_cannot_skip_launched_game_cleanup(monkeypatch, tmp_path):
    from PIL import ImageGrab
    from coding4game_gui.benchmark.windows.drivers.windows_xinput import lifecycle

    loaded = []
    cfg = SimpleNamespace(process="Demo.exe", auto_launch=SimpleNamespace(
        launch_path="C:/Games/Demo.exe"))

    def load(_game, *, validate_files):
        loaded.append(validate_files)
        return cfg

    def fail_probe(*_args):
        raise RuntimeError("launch probe failed")

    def fail_capture():
        raise OSError("capture unavailable")

    closed = []
    monkeypatch.setattr(probe, "load_game_config", load)
    monkeypatch.setattr(probe.psutil, "process_iter", lambda *_a: [])
    monkeypatch.setattr(lifecycle, "set_dpi_awareness", lambda: None)
    monkeypatch.setattr(probe, "_probe", fail_probe)
    monkeypatch.setattr(ImageGrab, "grab", fail_capture)
    monkeypatch.setattr(probe.ss, "kill_process", lambda name:
                        closed.append(name) or 1)
    out = tmp_path / "evidence"
    monkeypatch.setattr(probe.sys, "argv", ["probe", "--game", "demo",
                                           "--out-dir", str(out)])
    with pytest.raises(RuntimeError, match="launch probe failed"):
        probe.main()
    report = json.loads((out / "result.json").read_text())
    assert loaded == [False]
    assert closed == ["Demo.exe"]
    assert report["error"] == "RuntimeError: launch probe failed"
    assert report["capture_error"] == "OSError: capture unavailable"
    assert report["killed_processes"] == 1


def test_probe_leaves_an_existing_game_untouched(monkeypatch, tmp_path):
    from coding4game_gui.benchmark.windows.drivers.windows_xinput import lifecycle

    cfg = SimpleNamespace(process="Demo.exe", auto_launch=SimpleNamespace(
        launch_path="C:/Games/Demo.exe"))
    monkeypatch.setattr(probe, "load_game_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(lifecycle, "set_dpi_awareness", lambda: None)
    monkeypatch.setattr(probe.psutil, "process_iter", lambda *_a:
                        [SimpleNamespace(info={"name": "Demo.exe"})])
    monkeypatch.setattr(probe.ss, "kill_process", lambda _n:
                        pytest.fail("existing game must not be terminated"))
    out = tmp_path / "evidence"
    monkeypatch.setattr(probe.sys, "argv", ["probe", "--game", "demo",
                                           "--out-dir", str(out)])
    assert probe.main() == 2
    assert not out.exists()
