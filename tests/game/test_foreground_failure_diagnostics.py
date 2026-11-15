"""Failed focus acquisition must retain host-only observation evidence."""
import json
import sys
from types import SimpleNamespace

import pytest

from coding4game_gui.benchmark.windows import auto_nav
from coding4game_gui.benchmark.windows import env as env_module


@pytest.fixture
def focus_chain(monkeypatch, tmp_path):
    calls = []
    state = {"foreground": 99, "focus_result": False}
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("C4G_WINDOWS_EVENT_LOG", str(log))

    def foreground():
        calls.append("foreground")
        return state["foreground"]

    gui = SimpleNamespace(
        GetForegroundWindow=foreground,
        IsWindow=lambda hwnd: True,
        IsWindowVisible=lambda hwnd: True,
        IsIconic=lambda hwnd: False,
        GetClassName=lambda hwnd: "GameWindow" if hwnd == 7 else "CoreWindow",
    )
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    monkeypatch.setitem(sys.modules, "win32process", SimpleNamespace(
        GetWindowThreadProcessId=lambda hwnd: (hwnd + 10, hwnd + 100)))
    import ctypes
    import psutil
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(
        GetAsyncKeyState=lambda key: 0x8000 if key == 0x5B else 0)), raising=False)
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(
        name=lambda: "Brotato.exe" if pid == 107 else "ShellExperienceHost.exe",
        create_time=lambda: float(pid)))

    def acquire(hwnd):
        calls.append("activate")
        assert hwnd == 7
        return state["focus_result"]

    monkeypatch.setattr(auto_nav, "force_foreground", acquire)
    monkeypatch.setattr(env_module.ResetController, "_send_focus_alt",
                        lambda self: calls.append("alt"))
    controller = env_module.ResetController.__new__(env_module.ResetController)
    driver = SimpleNamespace(lifecycle=SimpleNamespace(
        find=lambda: SimpleNamespace(extras={"hwnd": 7})))
    return controller, driver, log, state, calls, gui


def read_event(log):
    events = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "foreground_acquisition"
    assert event["state"] == "failed"
    return event


def test_real_focus_failure_retains_target_foreground_and_modifier_identity(focus_chain):
    controller, driver, log, state, calls, gui = focus_chain
    assert controller._force_foreground(driver) is False
    event = read_event(log)
    assert event["reason"] == "activation_not_observed"
    assert event["observation_scope"] == "after_failed_focus_request"
    assert event["target"]["hwnd"] == 7
    assert event["target"]["pid"] == 107
    assert event["target"]["process_name"] == "Brotato.exe"
    assert event["foreground"]["hwnd"] == 99
    assert event["foreground"]["class_name"] == "CoreWindow"
    assert event["foreground"]["process_name"] == "ShellExperienceHost.exe"
    assert event["modifiers_down"]["left_windows"] is True
    assert event["modifiers_down"]["alt"] is False
    assert calls[:3] == ["foreground", "alt", "activate"]


@pytest.mark.parametrize("already_foreground", [False, True])
def test_success_has_original_control_calls_and_no_observation(
        focus_chain, monkeypatch, already_foreground):
    controller, driver, log, state, calls, gui = focus_chain
    state["foreground"] = 7 if already_foreground else 99
    state["focus_result"] = True

    def forbidden(*args):
        pytest.fail("Successful focus must not query diagnostic window data")

    gui.IsWindow = forbidden
    assert controller._force_foreground(driver) is True
    assert calls == (["foreground"] if already_foreground else
                     ["foreground", "alt", "activate"])
    assert not log.exists()


def test_missing_target_is_distinct_from_activation_failure(focus_chain):
    controller, driver, log, state, calls, gui = focus_chain
    driver.lifecycle.find = lambda: SimpleNamespace(extras={})
    assert controller._force_foreground(driver) is False
    event = read_event(log)
    assert event["reason"] == "window_unavailable"
    assert event["target"]["hwnd"] is None
    assert event["foreground"]["pid"] == 199
    assert "alt" not in calls and "activate" not in calls


def test_focus_exception_retains_only_exception_type(focus_chain, monkeypatch):
    controller, driver, log, state, calls, gui = focus_chain

    def fail(hwnd):
        raise OSError("fixture-private-path-and-secret")

    monkeypatch.setattr(auto_nav, "force_foreground", fail)
    assert controller._force_foreground(driver) is False
    event = read_event(log)
    assert event["reason"] == "focus_exception"
    assert event["error_type"] == "OSError"
    assert "fixture-private-path-and-secret" not in log.read_text("utf-8")


def test_observation_failure_does_not_replace_original_focus_result(focus_chain):
    controller, driver, log, state, calls, gui = focus_chain

    def fail(hwnd):
        raise PermissionError("fixture-private-title")

    gui.IsWindow = fail
    assert controller._force_foreground(driver) is False
    event = read_event(log)
    assert event["target"]["observation_error_type"] == "PermissionError"
    assert "fixture-private-title" not in log.read_text("utf-8")


def test_disabled_event_log_performs_no_failure_observation(focus_chain, monkeypatch):
    controller, driver, log, state, calls, gui = focus_chain
    monkeypatch.delenv("C4G_WINDOWS_EVENT_LOG")

    def forbidden(hwnd):
        pytest.fail("Disabled diagnostics must not query native window data")

    gui.IsWindow = forbidden
    assert controller._force_foreground(driver) is False
    assert calls == ["foreground", "alt", "activate"]
    assert not log.exists()
