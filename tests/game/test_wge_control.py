"""Windows cold-start controller behavior; no benchmark reset paths exist."""
from types import SimpleNamespace

import pytest

from coding4game_gui.benchmark.windows import env as wge


def _controller():
    c = wge.ResetController.__new__(wge.ResetController)
    c.cfg = SimpleNamespace(process="fixture.exe")
    c._env = None
    c._scorer = None
    return c


def test_prepare_input_foreground_uses_live_dance_without_pause(monkeypatch):
    c = _controller()
    log = []

    class Found:
        extras = {"hwnd": 987654321}

    class Driver:
        class lifecycle:
            _found_cache = Found()

    c._force_foreground = lambda env: log.append("dance") or True
    monkeypatch.setattr("win32gui.GetForegroundWindow", lambda: 123)
    c.prepare_input_foreground(Driver())
    assert log == ["dance"]


def test_prepare_input_foreground_skips_dance_when_already_focused(monkeypatch):
    c = _controller()
    c._force_foreground = lambda _env: pytest.fail("unexpected focus dance")
    monkeypatch.setattr("win32gui.GetForegroundWindow", lambda: 987654321)
    c.prepare_input_foreground(SimpleNamespace(
        lifecycle=SimpleNamespace(
            _found_cache=SimpleNamespace(extras={"hwnd": 987654321}))))


def test_prepare_input_foreground_fails_before_input(monkeypatch):
    c = _controller()
    c._force_foreground = lambda _env: False
    monkeypatch.setattr("win32gui.GetForegroundWindow", lambda: 123)
    driver = SimpleNamespace(lifecycle=SimpleNamespace(
        _found_cache=SimpleNamespace(extras={"hwnd": 987654321})))
    with pytest.raises(RuntimeError, match="failed to foreground"):
        c.prepare_input_foreground(driver)


def test_cold_restart_is_shutdown_then_boot():
    c = _controller()
    calls = []
    c.shutdown = lambda: calls.append("shutdown")
    fresh = object()
    c.ensure_env = lambda: calls.append("boot") or fresh

    assert c.cold_restart() is fresh
    assert calls == ["shutdown", "boot"]


def test_shutdown_closes_driver_and_kills_process(monkeypatch):
    c = _controller()
    calls = []

    class Driver:
        def unpause(self):
            calls.append("unpause")

        def close(self):
            calls.append("close")

    class Cfg:
        process = "game.exe"

    c._env = Driver()
    c.cfg = Cfg()
    monkeypatch.setattr("coding4game_gui.benchmark.windows.save_state.kill_process",
                        lambda name: calls.append(("kill", name)))
    c.shutdown()

    assert calls == ["unpause", "close", ("kill", "game.exe")]
    assert c._env is None and c._scorer is None


def test_controller_exposes_no_in_game_reset_operations():
    for name in ("episode_restart", "full_restart", "recover",
                 "restart_steps", "abort_steps"):
        assert not hasattr(wge.ResetController, name)
