"""Construction failures must release the registered startup resources."""
from types import SimpleNamespace

import pytest

from coding4game_gui.benchmark.windows import auto_nav
from coding4game_gui.benchmark.windows import env as env_module
from coding4game_gui.benchmark.windows import save_state
from coding4game_gui.benchmark.windows import startup


@pytest.fixture
def startup_chain(monkeypatch):
    calls = []
    state = {"alive": False, "focus": False, "cleanup_fails": False}

    class Driver:
        def reset(self):
            calls.append("reset")

        def close(self):
            calls.append("close")

        def unpause(self):
            calls.append("unpause")

    driver = Driver()

    def launch(*_args, **_kwargs):
        calls.append("launch")
        state["alive"] = True
        return SimpleNamespace(pads=None)

    def kill(name, **_kwargs):
        assert name == "Brotato.exe"
        calls.append("kill")
        if state["cleanup_fails"]:
            raise OSError("fixture cleanup failure")
        state["alive"] = False

    monkeypatch.setattr(env_module, "_install_exit_cleanup", lambda _name: None)
    monkeypatch.setattr(startup, "prepare_launch_state", lambda *_a: None)
    monkeypatch.setattr(startup, "enforce_live_window", lambda *_a: None)
    monkeypatch.setattr(auto_nav, "launch_with_pads", launch)
    monkeypatch.setattr(auto_nav, "wait_for_game_hwnd", lambda *_a, **_k: (7, "Fixture"))
    monkeypatch.setattr(auto_nav, "force_foreground", lambda *_a: True)
    monkeypatch.setattr(auto_nav, "settle_with_foreground", lambda *_a, **_k: 7)
    monkeypatch.setattr(auto_nav, "make_env", lambda *_a, **_k: driver)
    monkeypatch.setattr(env_module.ResetController, "_force_foreground",
                        lambda *_a: state["focus"])
    monkeypatch.setattr(save_state, "kill_process", kill)
    return calls, state, driver


def test_real_constructor_post_settle_focus_failure_closes_registered_game(startup_chain):
    calls, state, _driver = startup_chain
    with pytest.raises(RuntimeError, match="could not focus game before startup capture"):
        env_module.WindowsGameEnv("brotato", windows_input="gamepad-kbm")

    assert calls == ["launch", "reset", "close", "kill"]
    assert not state["alive"]


def test_cleanup_failure_after_constructor_error_is_fatal_without_startup_retries(
        startup_chain, tmp_path):
    from coding4game_gui.benchmark.windows.session import GameSession
    from coding4game_gui.benchmark.windows.session import StartupRetriesExhausted

    calls, state, _driver = startup_chain
    state["cleanup_fails"] = True
    with GameSession(lambda: env_module.WindowsGameEnv(
            "brotato", windows_input="gamepad-kbm"), tmp_path,
            tic_budget=10, max_tics=1) as session:
        with pytest.raises(StartupRetriesExhausted, match="startup_cleanup_failed"):
            session.start(mode="stepped", max_tics=1)
        results = session.status()["episodes"]

    assert calls == ["launch", "reset", "close", "kill"]
    assert state["alive"]
    assert len(results) == 1
    assert results[0]["tics"] == 0
    assert results[0]["score"] is None
    assert not results[0]["complete"]


def test_successful_construction_adds_no_cleanup_call(monkeypatch, startup_chain):
    calls, state, driver = startup_chain
    monkeypatch.setattr(env_module.ResetController, "_boot", lambda _self: driver)
    monkeypatch.setattr(env_module.ResetController, "make_scorer", lambda *_a: object())
    monkeypatch.setattr(env_module.ResetController, "action_table", lambda *_a: {})

    result = env_module.WindowsGameEnv("brotato", windows_input="gamepad-kbm")
    assert result._env is driver
    assert calls == []


def test_successfully_cleaned_focus_failures_retain_three_attempt_limit(
        startup_chain, tmp_path):
    from coding4game_gui.benchmark.windows.session import GameSession
    from coding4game_gui.benchmark.windows.session import StartupRetriesExhausted

    calls, state, _driver = startup_chain
    with GameSession(lambda: env_module.WindowsGameEnv(
            "brotato", windows_input="gamepad-kbm"), tmp_path,
            tic_budget=10, max_tics=1) as session:
        with pytest.raises(StartupRetriesExhausted, match="startup_retries_exhausted"):
            session.start(mode="stepped", max_tics=1)
        results = session.status()["episodes"]

    assert calls == ["launch", "reset", "close", "kill"] * 3
    assert not state["alive"]
    assert len(results) == 3
    assert all(x["tics"] == 0 and x["score"] is None and not x["complete"]
               for x in results)


@pytest.mark.parametrize("failed_method", ["make_scorer", "action_table"])
def test_wiring_failure_after_driver_creation_shuts_down_once(
        monkeypatch, startup_chain, failed_method):
    calls, state, driver = startup_chain

    def boot(_self):
        state["alive"] = True
        return driver

    def fail(*_args):
        raise ValueError("fixture wiring failure")

    monkeypatch.setattr(env_module.ResetController, "_boot", boot)
    monkeypatch.setattr(env_module.ResetController, "make_scorer", lambda *_a: object())
    monkeypatch.setattr(env_module.ResetController, failed_method, fail)

    with pytest.raises(ValueError, match="fixture wiring failure"):
        env_module.WindowsGameEnv("brotato", windows_input="gamepad-kbm")

    assert calls == ["unpause", "close", "kill"]
    assert not state["alive"]
