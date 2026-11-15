from types import SimpleNamespace

import pytest

from coding4game_gui.benchmark.windows import games_config
from coding4game_gui.benchmark.windows.env import ResetController


@pytest.mark.parametrize("mode,backend,focus_succeeds", [
    ("closed_loop", "dxcam", True),
    ("closed_loop", "pyautogui", False),
    ("open_loop", "printwindow_visible", True),
    ("open_loop", "visible_client", False),
])
def test_screen_capture_reacquires_foreground_before_freezing(
        monkeypatch, mode, backend, focus_succeeds):
    from coding4game_gui.benchmark.windows import auto_nav

    cfg = SimpleNamespace(process="Demo.exe", menu_skip=[])
    monkeypatch.setattr(games_config, "load_game_config", lambda _name: cfg)
    controller = ResetController({
        "driver_game": "demo", "startup": {"menu_skip_replay": mode}})
    events = []
    focused = [True]
    first_request = [True]

    def focus(_env):
        events.append("focus")
        if first_request:
            first_request.pop()
            return True
        focused[0] = focus_succeeds
        return focus_succeeds

    def lose_focus():
        focused[0] = False
        return SimpleNamespace(size=(2, 2))

    def pause():
        events.append(("pause", focused[0]))

    monkeypatch.setattr(controller, "_force_foreground", focus)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr(auto_nav, "run_pad_sequence", lambda *_a, **_k: lose_focus())
    env = SimpleNamespace(
        hard_pause=True, unpause=lambda: None, pause=pause,
        render=lose_focus, screenshot_backend_name=backend)
    if focus_succeeds:
        controller.replay_menu_skip(env)
        assert events[-2:] == ["focus", ("pause", True)]
    else:
        with pytest.raises(RuntimeError, match="lost foreground"):
            controller.replay_menu_skip(env)
        assert events[-1] == ("pause", False)
    assert env.hard_pause is True


def test_hwnd_capture_needs_no_foreground_at_handoff(monkeypatch):
    controller = object.__new__(ResetController)
    controller.cfg = SimpleNamespace(process="Demo.exe")
    monkeypatch.setattr(controller, "_force_foreground", lambda _env: pytest.fail(
        "PrintWindow does not need a desktop foreground change"))
    controller._ensure_menu_handoff_foreground(SimpleNamespace(
        screenshot_backend_name="printwindow"))


def test_failed_declared_menu_destination_stops_before_later_input(monkeypatch):
    from coding4game_gui.benchmark.windows import env as env_module

    entry = SimpleNamespace(pad=0, spec=SimpleNamespace(
        click=(10, 10), expect_text="Search results", expect_absent=None,
        duration_seconds=1, description="Open search", type_text=None,
        click_current=False))
    cfg = SimpleNamespace(process="Demo.exe", menu_skip=[entry])
    monkeypatch.setattr(games_config, "load_game_config", lambda _name: cfg)
    controller = ResetController({"driver_game": "demo", "startup": {}})
    monkeypatch.setattr(controller, "_force_foreground", lambda _env: True)
    monkeypatch.setattr(controller, "nav_verifier", lambda _env: object())
    monkeypatch.setattr(env_module, "_click_until_text", lambda *_a, **_k: False)
    paused = []
    env = SimpleNamespace(
        hard_pause=True, unpause=lambda: None, pause=lambda: paused.append(True),
        render=lambda: SimpleNamespace(size=(2, 2)))
    with pytest.raises(RuntimeError, match="never reached.*Search results"):
        controller.replay_menu_skip(env)
    assert paused == [True] and env.hard_pause is True


@pytest.mark.parametrize("mode", ["closed_loop", "open_loop"])
def test_failed_initial_focus_never_reads_or_clicks_another_app(monkeypatch, mode):
    from coding4game_gui.benchmark.windows import auto_nav, env as env_module

    entry = SimpleNamespace(pad=0, spec=SimpleNamespace(
        click=(10, 10), expect_text=None, expect_absent=None,
        duration_seconds=.1, description="Open task", type_text=None,
        click_current=False))
    cfg = SimpleNamespace(process="Demo.exe", menu_skip=[entry])
    monkeypatch.setattr(games_config, "load_game_config", lambda _name: cfg)
    controller = ResetController({"driver_game": "demo",
                                  "startup": {"menu_skip_replay": mode}})
    events = []
    monkeypatch.setattr(controller, "_force_foreground", lambda _env: False)
    monkeypatch.setattr(env_module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(env_module, "_click_until_changed",
                        lambda *_a, **_k: events.append("click") or True)
    monkeypatch.setattr(auto_nav, "run_pad_sequence",
                        lambda *_a, **_k: events.append("input"))
    env = SimpleNamespace(
        hard_pause=True, unpause=lambda: None,
        pause=lambda: events.append("pause"), screenshot_backend_name="dxcam",
        render=lambda: events.append("read") or SimpleNamespace(size=(2, 2)))
    with pytest.raises(RuntimeError, match="before menu replay"):
        controller.replay_menu_skip(env)
    assert events == ["pause"]
    assert env.hard_pause is True


def test_capture_mouse_startup_can_unlock_a_background_launch(monkeypatch, load_driver_metadata):
    import ctypes
    import sys
    from coding4game_gui.benchmark.spec import load_game_spec
    from coding4game_gui.benchmark.windows import auto_nav

    controller = ResetController(load_game_spec(
        "palworld_capture_penking", windows_input="gamepad-kbm"))
    foreground = [99]
    keys = []
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        GetForegroundWindow=lambda: foreground[0]))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(
        keybd_event=lambda key, scan, flags, extra: keys.append((key, flags)))))

    def request(hwnd):
        if keys == [(0x12, 0), (0x12, 2)]:
            foreground[0] = hwnd
        return foreground[0] == hwnd

    monkeypatch.setattr(auto_nav, "force_foreground", request)
    env = SimpleNamespace(lifecycle=SimpleNamespace(
        find=lambda: SimpleNamespace(extras={"hwnd": 7})))
    assert controller._force_foreground(env)
    assert foreground[0] == 7


@pytest.mark.parametrize("focus_succeeds", [True, False])
def test_first_startup_capture_uses_configured_focus_unlock(monkeypatch, load_driver_metadata, focus_succeeds):
    import sys
    from coding4game_gui.benchmark.windows import auto_nav
    from coding4game_gui.benchmark.spec import load_game_spec

    controller = ResetController(load_game_spec("palworld_capture_penking"))
    foreground = [99]
    calls = []
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        GetForegroundWindow=lambda: foreground[0]))

    def unlock():
        calls.append("configured_unlock")
        if focus_succeeds:
            foreground[0] = 7
        return True

    async def capture():
        assert foreground[0] == 7
        calls.append("capture")

    env = SimpleNamespace(lifecycle=SimpleNamespace(
        find=lambda: SimpleNamespace(extras={"hwnd": 7})), render_locked=capture)
    monkeypatch.setattr(controller, "_send_focus_alt", unlock)
    monkeypatch.setattr(auto_nav, "force_foreground", lambda h: foreground[0] == h)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    def start(_cfg, **kwargs):
        kwargs["post_settle"](env, 7)
        return env, 7, "Pal"

    monkeypatch.setattr(auto_nav, "start_auto_launch_session", start)
    if focus_succeeds:
        assert controller._boot() is env
        assert calls == ["configured_unlock", "capture"]
    else:
        with pytest.raises(RuntimeError, match="before startup capture"):
            controller._boot()
        assert calls == ["configured_unlock"]
