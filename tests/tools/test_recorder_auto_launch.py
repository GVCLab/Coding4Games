from pathlib import Path
from types import SimpleNamespace

from coding4game_gui.benchmark.windows import auto_nav
from coding4game_gui.benchmark.windows import game_launch
from coding4game_gui.benchmark.windows import games_config
from coding4game_gui.benchmark.windows import prelaunch
from coding4game_gui.benchmark.windows import save_state
from coding4game_gui.benchmark.windows import startup
from coding4game_gui.benchmark import spec as agentic
from scripts.calibration import record_pad_to_yaml as recorder


def test_recorder_auto_launch_allows_driver_without_benchmark_scenario(
        tmp_path, monkeypatch):
    """Menu recording is a driver-onboarding step, before a scenario exists."""
    game_path = tmp_path / "driver_only.yaml"
    cfg = SimpleNamespace(
        process="DriverOnly.exe",
        auto_launch=SimpleNamespace(
            launch_path="C:/Games/DriverOnly/DriverOnly.exe",
            window_wait_seconds=0.01,
            settle_seconds=0.0,
        ),
    )
    killed = []
    fake_driver = SimpleNamespace(close=lambda: None)
    launch_calls = []
    monkeypatch.setattr(
        save_state, "kill_process", lambda process: killed.append(process))
    monkeypatch.setattr(
        prelaunch,
        "launch_with_pads",
        lambda auto_launch, process, **kwargs: (
            launch_calls.append((auto_launch, process, kwargs))
            or SimpleNamespace(pads=fake_driver)
        ),
    )
    monkeypatch.setattr(
        auto_nav, "wait_for_game_hwnd", lambda _process, timeout: (123, "Demo"))
    monkeypatch.setattr(
        startup,
        "prepare_launch_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("driver-only recording has no scenario startup")),
    )
    monkeypatch.setattr(
        startup,
        "enforce_live_window",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("driver-only recording has no scenario geometry")),
    )
    monkeypatch.setattr(recorder, "_force_game_foreground", lambda _process: True)
    monkeypatch.setattr(recorder.time, "sleep", lambda _seconds: None)

    result = recorder.launch_and_wait(game_path, cfg, n_pads=1)

    assert result is fake_driver
    assert killed == ["DriverOnly.exe"]
    assert len(launch_calls) == 1
    assert launch_calls[0][2]['require_prepared_pads'] is True


def test_recorder_auto_launch_detects_window_by_process_not_title(monkeypatch):
    """Black Myth's process is descriptive but its window title is only 'b1'."""
    cfg = SimpleNamespace(
        process="b1-Win64-Shipping.exe",
        auto_launch=SimpleNamespace(
            launch_path="C:/Games/BlackMyth/b1-Win64-Shipping.exe",
            window_wait_seconds=0.01,
            settle_seconds=0.0,
        ),
    )
    monkeypatch.setattr(games_config, "load_game_config", lambda _path: cfg)
    monkeypatch.setattr(game_launch, "launch_game", lambda _spec: object())
    spec = {
        "driver_game": "black_myth",
        "startup": {},
        "scorer": {},
    }
    prepared = []
    enforced = []
    monkeypatch.setattr(agentic, "load_game_spec", lambda _scenario: spec)
    monkeypatch.setattr(
        startup,
        "prepare_launch_state",
        lambda actual, actual_cfg: prepared.append((actual, actual_cfg)),
    )
    monkeypatch.setattr(
        startup,
        "enforce_live_window",
        lambda actual, actual_cfg, hwnd: enforced.append(
            (actual, actual_cfg, hwnd)),
    )
    monkeypatch.setattr(
        auto_nav, "wait_for_game_hwnd", lambda _process, timeout: (321, "b1"))
    monkeypatch.setattr(recorder, "_force_game_foreground", lambda _process: True)

    wait_calls = []
    monkeypatch.setattr(
        save_state,
        "wait_for_game_window",
        lambda process_name, timeout: wait_calls.append((process_name, timeout)) or True,
    )

    monkeypatch.setattr(recorder.time, "sleep", lambda _seconds: None)

    fake_pads = SimpleNamespace(wakeup=lambda: None, close=lambda: None)
    monkeypatch.setattr(
        'coding4game_gui.benchmark.windows.drivers.windows_xinput.input.XInputDriver',
        lambda **_kwargs: fake_pads,
    )
    assert recorder.launch_and_wait(Path("games/black_myth.yaml")) is fake_pads

    assert wait_calls == [("b1-Win64-Shipping.exe", 0.01)]
    assert prepared == [(spec, cfg)]
    assert enforced == [(spec, cfg, 321)]


def test_recorder_releases_game_cursor_clip(monkeypatch):
    calls = []
    fake_user32 = SimpleNamespace(
        ClipCursor=lambda rect: calls.append(rect) or 1,
    )
    monkeypatch.setattr(recorder.sys, "platform", "win32")
    monkeypatch.setattr(
        recorder.ctypes,
        "windll",
        SimpleNamespace(user32=fake_user32),
    )

    assert recorder._release_cursor_clip() is True
    assert calls == [None]


def test_explicit_scenario_does_not_restore_the_legacy_driver_checkpoint(tmp_path, monkeypatch):
    for name in ("kof98", "kof98_terminal"):
        (tmp_path / f"{name}.yaml").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(agentic, "WINDOWS_GAMES_DIR", tmp_path)
    selected = []
    spec = {"driver_game": "kof98", "startup": {}, "scorer": {}}

    def load(slug):
        assert slug == "kof98_terminal", "the legacy scenario requires a nonexistent checkpoint"
        return spec

    monkeypatch.setattr(agentic, "load_game_spec", load)
    monkeypatch.setattr(startup, "prepare_launch_state", lambda s, c: selected.append(s))
    monkeypatch.setattr(startup, "enforce_live_window", lambda *_a: None)
    driver = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(prelaunch, "launch_with_pads", lambda *_a, **_k: SimpleNamespace(pads=driver))
    monkeypatch.setattr(auto_nav, "wait_for_game_hwnd", lambda *_a, **_k: (1, "KOF"))
    monkeypatch.setattr(recorder.time, "sleep", lambda _s: None)
    cfg = SimpleNamespace(process="KOF.exe", auto_launch=SimpleNamespace(
        launch_path="C:/Games/KOF.exe", settle_seconds=0.0))
    result = recorder.launch_and_wait(tmp_path / "kof98.yaml", cfg, scenario="kof98_terminal")
    assert result is driver
    assert selected == [spec]


def test_left_mouse_hold_preserves_duration(monkeypatch):
    """Holding a recorder control must preserve the real mouse-down duration."""
    events = []
    timestamps = iter((10.0, 11.5))
    panel = SimpleNamespace(
        pad_idx=1,
        _toggled={},
        _last_left_t={},
        _tap_active={},
        _widgets={"A": SimpleNamespace(configure=lambda **_kwargs: None)},
        q=SimpleNamespace(put=events.append),
        _debounced=lambda *_args: False,
    )
    monkeypatch.setattr(recorder.time, "monotonic", lambda: next(timestamps))

    recorder.PadPanel._on_left_press(panel, "A", "button")
    assert [event['action'] for event in events] == ['down']
    recorder.PadPanel._on_left_release(panel, "A", "button")

    tracker = recorder.PadStateTracker()
    for event in events:
        tracker.on_event(
            event["t"], event["pad"], event["kind"], event["token"], event["action"],
        )
    assert [event["action"] for event in events] == ["down", "up"]
    assert tracker.steps[0].duration_seconds == 1.5


def test_input_guard_keeps_emergency_hotkeys_while_keyboard_is_blocked():
    controls = []
    guard = recorder.PhysicalInputIsolation(lambda *_args: None, controls.append)

    assert guard._handle_physical_key(0xA2, guard.WM_KEYDOWN) is None  # left Ctrl
    assert guard._handle_physical_key(0xA4, guard.WM_SYSKEYDOWN) is None  # left Alt
    assert guard._handle_physical_key(ord("Q"), guard.WM_KEYDOWN) == "cancel"
