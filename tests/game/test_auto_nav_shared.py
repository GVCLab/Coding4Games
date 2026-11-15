from types import SimpleNamespace

from PIL import Image, ImageDraw
import pytest

from coding4game_gui.benchmark.windows.auto_nav import run_pad_sequence


class _Clock:
    def __init__(self):
        self.value = -0.05

    def __call__(self):
        self.value += 0.05
        return self.value


def test_navigation_ocr_errors_never_count_as_arrival(monkeypatch):
    from coding4game_gui.benchmark.windows import env as env_module

    clicked = []
    monkeypatch.setattr(env_module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(env_module, "_click_game_points", lambda *_a: clicked.append(True))
    def unavailable(*_a):
        raise RuntimeError("capture unavailable")
    env = SimpleNamespace(render=lambda: Image.new("RGB", (16, 9), "black"))
    reader = SimpleNamespace(check_text=unavailable)
    assert not env_module._click_until_text(
        "Demo.exe", env, (1, 1), "Search results", reader, settle=.4, retries=2)
    assert len(clicked) == 2


@pytest.mark.parametrize("ready", [True, False])
def test_recorded_wait_checks_destination_before_the_next_click(monkeypatch, ready):
    from coding4game_gui.benchmark.windows import env as env_module

    reads = []
    clicks = []
    wait = _entry(duration=.1)
    click = _entry(duration=.1)
    for entry in [wait, click]:
        entry.spec.click = None
        entry.spec.expect_text = None
        entry.spec.expect_absent = None
    wait.spec.expect_text = "recorded task"
    click.spec.click = (1, 1)
    controller = object.__new__(env_module.ResetController)
    controller.spec = {"scorer": {"calibrated_client_size": [16, 9]}}
    controller.cfg = SimpleNamespace(process="Demo.exe", menu_skip=[wait, click])
    controller._start_check = None
    controller._menu_skip_replay = "closed_loop"
    controller._force_foreground = lambda _env: True
    controller._ensure_menu_handoff_foreground = lambda _env: None
    def check(*_args):
        reads.append(True)
        return ready and len(reads) >= 2
    controller.nav_verifier = lambda _env: SimpleNamespace(check_text=check)
    env = SimpleNamespace(unpause=lambda: None, pause=lambda: None,
                          render=lambda: Image.new("RGB", (16, 9)))
    def click_after_wait(*_args, **_kwargs):
        assert len(reads) >= 2
        clicks.append(True)
        return True
    monkeypatch.setattr(env_module, "_click_until_changed", click_after_wait)
    monkeypatch.setattr(env_module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(env_module, "_client_size_of", lambda _p: (16, 9))
    if ready:
        controller.replay_menu_skip(env)
        assert clicks == [True]
    else:
        with pytest.raises(RuntimeError, match="recorded task"):
            controller.replay_menu_skip(env)
        assert clicks == []
    assert env.hard_pause


def test_navigation_crop_is_separate_from_the_settlement_crop():
    from coding4game_gui.benchmark.windows.env import ResetController

    controller = object.__new__(ResetController)
    controller.spec = {"scorer": {"kind": "ocr", "ocr": {"rect": [1, 2, 3, 4]}}}
    controller.startup_spec = {"menu_skip_ocr_rect": [0, 0, 16, 9]}
    controller._scorer = object()
    controller._nav_scorer = None
    seen = []
    controller._scorer_factory = SimpleNamespace(create_text_reader=lambda spec, **_kw:
                                               seen.append(spec) or "navigation")
    env = SimpleNamespace(window_width=16, window_height=9)
    assert controller.nav_verifier(env) == "navigation"
    assert seen[0]["ocr"]["rect"] == [0, 0, 16, 9]
    assert controller.spec["scorer"]["ocr"]["rect"] == [1, 2, 3, 4]


def test_save_scorer_can_use_explicit_navigation_ocr_without_changing_score():
    from coding4game_gui.benchmark.windows.env import ResetController
    controller = object.__new__(ResetController)
    controller.spec = {"scorer": {"kind": "save", "implementation": "save_verification"}}
    controller.startup_spec = {"menu_skip_ocr": {"rect": [0, 0, 16, 9], "device": "gpu:0"}}
    controller._scorer = object()
    controller._nav_scorer = None
    seen = []
    controller._scorer_factory = SimpleNamespace(create_text_reader=lambda spec, **_kw:
                                               seen.append(spec) or "navigation")
    assert controller.nav_verifier(SimpleNamespace(window_width=16, window_height=9)) == "navigation"
    assert seen[0]["ocr"]["device"] == "gpu:0"
    assert "ocr" not in controller.spec["scorer"]


@pytest.mark.parametrize("ready", [True, False])
def test_pad_text_destination_requires_observed_arrival(monkeypatch, ready):
    from coding4game_gui.benchmark.windows import env as module
    controller = object.__new__(module.ResetController)
    step = _entry(duration=.1)
    step.spec.expect_text = "confirmation"
    step.spec.expect_absent = None
    step.spec.gamepad_state = lambda: {"buttons": ["WEST"]}
    sent = []
    reads = []
    def check(*_args):
        reads.append(True)
        return ready and len(sent) >= 2
    controller.nav_verifier = lambda _env: SimpleNamespace(check_text=check)
    env = SimpleNamespace(render=lambda: Image.new("RGB", (16, 9)),
                          apply_state=lambda *a, **kw: sent.append(kw))
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    if ready:
        controller._press_until_screen_changes(env, step, retries=1)
        assert len(sent) == 2
    else:
        with pytest.raises(RuntimeError, match="confirmation"):
            controller._press_until_screen_changes(env, step, retries=1)
        assert sent[-1]["buttons"] == []


def test_settle_keeps_game_foreground_without_changing_recorded_wait(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    events = []
    monkeypatch.setattr(auto_nav.time, "sleep", lambda seconds: events.append(("sleep", seconds)))
    monkeypatch.setattr(auto_nav, "force_foreground", lambda hwnd: events.append(("focus", hwnd)))
    auto_nav.settle_with_foreground(7, 5.0)
    assert events == [("sleep", 2.0), ("focus", 7),
                      ("sleep", 2.0), ("focus", 7),
                      ("sleep", 1.0), ("focus", 7)]


@pytest.mark.parametrize("outside", [False, True])
def test_startup_pointer_leaves_taskbar_hover_without_clicks(monkeypatch, outside):
    from coding4game_gui.benchmark.windows import auto_nav

    cursor = [(300, 1560) if outside else (500, 500)]
    moves, events = [], []
    monkeypatch.setattr(auto_nav.win32gui, "GetForegroundWindow", lambda: 7)
    monkeypatch.setattr(auto_nav.win32gui, "GetClientRect", lambda _h: (0, 0, 2048, 1152))
    monkeypatch.setattr(auto_nav.win32gui, "ClientToScreen", lambda _h, _p: (256, 241))
    monkeypatch.setattr(auto_nav.win32api, "GetCursorPos", lambda: cursor[0])

    def move(point):
        moves.append(point)
        cursor[0] = point

    monkeypatch.setattr(auto_nav.win32api, "SetCursorPos", move)
    monkeypatch.setattr(auto_nav, "emit_windows_event", lambda *a, **k: events.append((a, k)))
    auto_nav.prepare_startup_pointer(7)
    assert moves == ([(1280, 817)] if outside else [])
    assert len(events) == int(outside)


@pytest.mark.parametrize("focused,delivered", [(False, True), (True, False)])
def test_startup_pointer_rejects_unfocused_or_undelivered_preparation(
        monkeypatch, focused, delivered):
    from coding4game_gui.benchmark.windows import auto_nav

    moves = []
    monkeypatch.setattr(auto_nav.win32gui, "GetForegroundWindow", lambda: 7 if focused else 8)
    monkeypatch.setattr(auto_nav.win32gui, "GetClientRect", lambda _h: (0, 0, 2048, 1152))
    monkeypatch.setattr(auto_nav.win32gui, "ClientToScreen", lambda _h, _p: (256, 241))
    monkeypatch.setattr(auto_nav.win32api, "GetCursorPos", lambda: (300, 1560))
    monkeypatch.setattr(auto_nav.win32api, "SetCursorPos", moves.append)
    with pytest.raises(RuntimeError, match="foreground|could not enter"):
        auto_nav.prepare_startup_pointer(7)
    assert len(moves) == int(focused)


def test_settle_waits_through_missing_bootstrap_window_without_focusing_it(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    slept = []
    focused = []
    clock = [0.0]
    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(auto_nav.time, "sleep", sleep)
    monkeypatch.setattr(auto_nav.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(auto_nav, "find_game_hwnd", lambda _p:
                        None if clock[0] < 4 else (8, "Game"))
    monkeypatch.setattr(auto_nav, "force_foreground", focused.append)
    assert auto_nav.settle_with_foreground(
        7, 5, process_name="Demo.exe", handoff_timeout_seconds=10) == 8
    assert slept == [2, 2, 2, 2, 1]
    assert focused == [8, 8, 8, 8]
    assert clock[0] == 9  # Five full seconds on the actual game window.


def test_replaced_window_gets_full_settle_and_handoffs_do_not_reset_budget(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    clock = [0.0]
    monkeypatch.setattr(auto_nav.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(auto_nav.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(auto_nav, "force_foreground", lambda _h: True)
    monkeypatch.setattr(auto_nav, "find_game_hwnd", lambda _p:
                        (7, "Bootstrap") if clock[0] < 4 else (8, "Game"))
    assert auto_nav.settle_with_foreground(
        7, 5, process_name="Demo.exe", handoff_timeout_seconds=4) == 8
    assert clock[0] == 9

    clock[0] = 0
    monkeypatch.setattr(auto_nav, "find_game_hwnd", lambda _p:
                        (int(clock[0]) + 10, "Transient"))
    with pytest.raises(RuntimeError, match="startup budget"):
        auto_nav.settle_with_foreground(
            7, 5, process_name="Demo.exe", handoff_timeout_seconds=4)
    assert clock[0] == 9


def test_force_foreground_posts_without_joining_the_game_input_queue(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    state = {"foreground": 11}
    monkeypatch.setattr(auto_nav.win32gui, "GetForegroundWindow",
                        lambda: state["foreground"])
    monkeypatch.setattr(auto_nav.win32process, "GetWindowThreadProcessId",
                        lambda _hwnd: (22, 33))
    monkeypatch.setattr(auto_nav.win32api, "GetCurrentThreadId", lambda: 44)
    monkeypatch.setattr(
        auto_nav.win32process, "AttachThreadInput",
        lambda *_args: pytest.fail("input queue attachment can block activation"),
    )
    monkeypatch.setattr(auto_nav.win32gui, "IsIconic", lambda _hwnd: False)
    monkeypatch.setattr(auto_nav.win32gui, "BringWindowToTop",
                        lambda _hwnd: pytest.fail("synchronous window activation"))
    positions = []
    monkeypatch.setattr(auto_nav.win32gui, "SetWindowPos",
                        lambda *args: positions.append(args))
    monkeypatch.setattr(auto_nav.win32gui, "SetForegroundWindow",
                        lambda hwnd: state.__setitem__("foreground", hwnd))

    assert auto_nav.force_foreground(77) is True
    assert positions[0][-1] & auto_nav.win32con.SWP_ASYNCWINDOWPOS


def test_force_foreground_does_nothing_when_already_focused(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    monkeypatch.setattr(auto_nav.win32gui, "GetForegroundWindow", lambda: 77)
    monkeypatch.setattr(auto_nav.win32gui, "IsIconic", lambda _hwnd: False)
    monkeypatch.setattr(auto_nav.win32gui, "SetWindowPos",
                        lambda *args: pytest.fail("already focused"))
    assert auto_nav.force_foreground(77) is True


@pytest.mark.parametrize("restored", [True, False])
def test_force_foreground_restores_an_iconic_foreground_window(monkeypatch, restored):
    from coding4game_gui.benchmark.windows import auto_nav

    state = {"iconic": True}
    posted = []
    monkeypatch.setattr(auto_nav.win32gui, "GetForegroundWindow", lambda: 77)
    monkeypatch.setattr(auto_nav.win32gui, "IsIconic", lambda _hwnd: state["iconic"])
    def post(*args):
        posted.append(args)
        state["iconic"] = not restored
    monkeypatch.setattr(auto_nav.win32gui, "PostMessage", post)
    monkeypatch.setattr(auto_nav.win32gui, "SetWindowPos", lambda *args: None)
    monkeypatch.setattr(auto_nav.win32gui, "SetForegroundWindow", lambda _hwnd: None)

    assert auto_nav.force_foreground(77) is restored
    assert posted == [(77, auto_nav.win32con.WM_SYSCOMMAND,
                       auto_nav.win32con.SC_RESTORE, 0)]


def test_force_foreground_request_does_not_claim_unconfirmed_activation(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    posted = []
    monkeypatch.setattr(auto_nav.win32gui, "GetForegroundWindow", lambda: 11)
    monkeypatch.setattr(auto_nav.win32gui, "IsIconic", lambda _hwnd: True)
    monkeypatch.setattr(auto_nav.win32gui, "PostMessage",
                        lambda *args: posted.append(args))
    monkeypatch.setattr(auto_nav.win32gui, "SetWindowPos", lambda *args: None)
    monkeypatch.setattr(auto_nav.win32gui, "SetForegroundWindow", lambda _hwnd: None)
    assert auto_nav.force_foreground(77) is False
    assert posted == [(77, auto_nav.win32con.WM_SYSCOMMAND,
                       auto_nav.win32con.SC_RESTORE, 0)]


def test_text_navigation_rechecks_late_modal_after_initial_perception(monkeypatch):
    from types import SimpleNamespace
    from PIL import Image
    from coding4game_gui.benchmark.windows import env as module

    state = {"modal": False, "clicked": False}
    events = []

    def check(_frame, _pattern):
        events.append("observe")
        if not state["clicked"]:
            state["modal"] = True
        return state["clicked"]

    def guard():
        events.append("guard")
        state["modal"] = False

    def click(*_args, **_kwargs):
        assert not state["modal"], "late modal must be cleared before input"
        events.append("click")
        state["clicked"] = True

    monkeypatch.setattr(module, "_click_game_points", click)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    driver = SimpleNamespace(render=lambda: Image.new("RGB", (10, 10)))
    reader = SimpleNamespace(check_text=check)
    assert module._click_until_text("Demo.exe", driver, (5, 5), "destination",
                                    reader, retries=1, before_click=guard)
    assert events == ["observe", "guard", "click", "observe"]


class _Pad:
    def __init__(self):
        self.resets = []

    def reset_pad(self, pad):
        self.resets.append(pad)


class _Env:
    def __init__(self):
        self.states = []
        self.gamepad = _Pad()

    def apply_state(self, pad, **state):
        self.states.append((pad, state))


def _entry(*, pad=0, duration=0.1, buttons=()):
    spec = SimpleNamespace(
        duration_seconds=duration,
        buttons=tuple(buttons),
        left_stick=(0.0, 0.0),
        right_stick=(0.0, 0.0),
        left_trigger=0.0,
        right_trigger=0.0,
        description="recorded step",
    )
    return SimpleNamespace(pad=pad, spec=spec)


def test_recorded_menu_sequence_is_replayed_once_without_retry():
    env = _Env()
    entries = [
        _entry(pad=0, duration=0.1, buttons=("A",)),
        _entry(pad=1, duration=9.0, buttons=("DPAD_DOWN",)),
    ]

    run_pad_sequence(
        env, entries, "menu_skip", max_duration=0.2, clock=_Clock())

    assert [pad for pad, _ in env.states] == [0, 1]
    assert env.states[0][1]["buttons"] == ["A"]
    assert env.states[1][1]["buttons"] == ["DPAD_DOWN"]
    assert env.gamepad.resets == [0, 1]


def test_palworld_eos_popup_detector_matches_one_button_modal():
    from coding4game_gui.benchmark.windows.auto_nav import is_palworld_eos_login_popup

    image = Image.new("RGB", (1280, 720), (3, 10, 14))
    draw = ImageDraw.Draw(image)
    draw.text((430, 225), "A network error has occurred.", fill="white")
    draw.text((350, 270), "Multiplayer functionality is unavailable.", fill="white")
    draw.rectangle((345, 355, 935, 357), fill=(72, 78, 80))
    draw.rectangle((538, 447, 742, 533), fill=(40, 52, 68))
    draw.rectangle((538, 447, 742, 533), outline=(180, 185, 190), width=2)
    draw.text((620, 480), "OK", fill="white")

    assert is_palworld_eos_login_popup(image)
    assert not is_palworld_eos_login_popup(Image.new("RGB", (1280, 720), "black"))


def test_palworld_eos_popup_detector_accepts_dim_real_world_button():
    from coding4game_gui.benchmark.windows.auto_nav import is_palworld_eos_login_popup

    image = Image.new("RGB", (1280, 720), (2, 8, 12))
    draw = ImageDraw.Draw(image)
    for y in (291, 312, 333):
        draw.rectangle((450, y, 830, y + 3), fill=(240, 245, 247))
    draw.rectangle((345, 358, 935, 359), fill=(72, 78, 80))
    draw.rectangle((550, 465, 730, 490), fill=(33, 40, 44))

    assert is_palworld_eos_login_popup(image)
    draw.rectangle((345, 358, 935, 359), fill=(2, 8, 12))
    assert not is_palworld_eos_login_popup(image)


def test_palworld_eos_popup_detector_accepts_login_timeout_layout():
    from coding4game_gui.benchmark.windows.auto_nav import is_palworld_eos_login_popup

    image = Image.new("RGB", (1191, 626), (3, 10, 14))
    draw = ImageDraw.Draw(image)
    draw.text((380, 180), "A network error has occurred.", fill="white")
    draw.text((300, 230), "The multiplayer feature is currently unavailable.",
              fill="white")
    draw.text((450, 280), "Error: EOS login Timeout", fill="white")
    draw.rectangle((120, 476, 1100, 479), fill=(72, 78, 80))
    draw.rectangle((415, 504, 695, 563), fill=(33, 40, 44))
    draw.text((540, 528), "OK", fill="white")

    assert is_palworld_eos_login_popup(image)


def test_eos_popup_gets_one_conditional_escape_press(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    popup = Image.new("RGB", (16, 9), "black")
    clear = Image.new("RGB", (16, 9), "white")
    env = _Env()
    frames = iter((popup, clear))
    env.render = lambda: next(frames)
    monkeypatch.setattr(
        auto_nav, "is_palworld_eos_login_popup",
        lambda frame: frame is popup,
    )
    waits = []
    keys = []
    monkeypatch.setattr(auto_nav.time, "sleep", waits.append)
    monkeypatch.setattr(auto_nav.win32api, "keybd_event",
                        lambda *args: keys.append(args))
    monkeypatch.setattr(auto_nav, "emit_windows_event", lambda *_a, **_k: None)
    monkeypatch.setattr(auto_nav, "force_foreground", lambda _hwnd: True)

    assert auto_nav.dismiss_palworld_eos_login_popup(
        env, "Palworld-Win64-Shipping.exe", 7) is True
    assert env.states == []
    assert env.gamepad.resets == []
    assert keys == [(auto_nav.win32con.VK_ESCAPE, 0, 0, 0),
                    (auto_nav.win32con.VK_ESCAPE, 0,
                     auto_nav.win32con.KEYEVENTF_KEYUP, 0)]
    assert waits == [0.12, 5.0]


def test_no_eos_popup_leaves_menu_input_unchanged(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    clear = Image.new("RGB", (16, 9), "white")
    env = _Env()
    env.render = lambda: clear
    monkeypatch.setattr(auto_nav, "is_palworld_eos_login_popup",
                        lambda _frame: False)
    keys = []
    monkeypatch.setattr(auto_nav.win32api, "keybd_event",
                        lambda *args: keys.append(args))

    assert not auto_nav.dismiss_palworld_eos_login_popup(
        env, "Palworld-Win64-Shipping.exe", 7)
    assert env.states == []
    assert keys == []


def test_persistent_eos_popup_aborts_before_menu_replay(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    env = _Env()
    env.render = lambda: Image.new("RGB", (16, 9), "black")
    monkeypatch.setattr(auto_nav, "is_palworld_eos_login_popup",
                        lambda _frame: True)
    monkeypatch.setattr(auto_nav.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(auto_nav, "emit_windows_event", lambda *_a, **_k: None)
    monkeypatch.setattr(auto_nav, "force_foreground", lambda _hwnd: True)
    keys = []
    monkeypatch.setattr(auto_nav.win32api, "keybd_event",
                        lambda *args: keys.append(args))

    with pytest.raises(RuntimeError, match="EOS login popup remained"):
        auto_nav.dismiss_palworld_eos_login_popup(
            env, "Palworld-Win64-Shipping.exe", 7)
    assert env.states == []
    assert env.gamepad.resets == []
    assert len(keys) == 2


def test_menu_replay_checks_for_modal_before_each_step():
    env = _Env()
    checked = []
    run_pad_sequence(env, [_entry(), _entry()], "menu_skip",
                     clock=_Clock(), before_step=checked.append)
    assert checked == [0, 1]
    assert len(env.states) == 2


def test_full_auto_launch_and_scorer_recorder_share_the_same_session_starter():
    import runpy
    from pathlib import Path

    from coding4game_gui.benchmark.windows.auto_nav import start_auto_launch_session
    from scripts.calibration import test_auto_launch_full

    recorder = runpy.run_path(str(
        Path(".agents/skills/add-new-game/scripts/record_scorer_session.py")))

    assert test_auto_launch_full.start_auto_launch_session is start_auto_launch_session
    assert recorder["start_auto_launch_session"] is start_auto_launch_session


def test_session_starter_reuses_prelaunch_pads_for_the_menu_replayer(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    class _SessionEnv:
        def reset(self):
            pass

        def render(self):
            return Image.new("RGB", (1280, 720), "black")

        def close(self):
            pass

    env = _SessionEnv()
    pads = object()

    def launch_with_pads(_launch, _process, *, n_pads, **_kwargs):
        assert n_pads == 2
        return SimpleNamespace(pads=pads)

    def make_env(*_args, n_players, input_driver, **_kwargs):
        assert n_players == 2
        assert input_driver is pads
        return env

    monkeypatch.setattr(auto_nav, "launch_with_pads", launch_with_pads)
    monkeypatch.setattr(auto_nav, "wait_for_game_hwnd", lambda *_a, **_k: (7, "Demo"))
    monkeypatch.setattr(auto_nav, "force_foreground", lambda *_a, **_k: True)
    monkeypatch.setattr(auto_nav, "settle_with_foreground", lambda *_a, **_k: None)
    monkeypatch.setattr(auto_nav, "make_env", make_env)
    cfg = SimpleNamespace(
        name="demo", process="Demo.exe", screenshot_backend="auto",
        auto_launch=SimpleNamespace(launch_path="Demo.exe", settle_seconds=0),
        menu_skip=[SimpleNamespace(pad=0), SimpleNamespace(pad=1)],
    )
    replayed = []

    got, _, _ = auto_nav.start_auto_launch_session(
        cfg, menu_skip_replayer=lambda e, steps: replayed.append((e, steps)))

    assert got is env
    assert replayed == [(env, cfg.menu_skip)]


@pytest.mark.parametrize("settled_hwnd", [7, 8])
@pytest.mark.parametrize("backend", ["auto", "dxcam"])
def test_session_applies_geometry_after_settle_before_renderer(monkeypatch, settled_hwnd, backend):
    from coding4game_gui.benchmark.windows import auto_nav

    configured = []
    env = SimpleNamespace(reset=lambda: None, close=lambda: None,
                          render=lambda: Image.new("RGB", (16, 9), "black"))
    monkeypatch.setattr(auto_nav, "launch_with_pads", lambda *_a, **_k:
                        SimpleNamespace(pads=None))
    monkeypatch.setattr(auto_nav, "wait_for_game_hwnd", lambda *_a, **_k: (7, "Demo"))
    def settle(*_args, **_kwargs):
        assert configured == []
        configured.append("settled")
        return settled_hwnd

    monkeypatch.setattr(auto_nav, "settle_with_foreground", settle)
    monkeypatch.setattr(auto_nav.win32gui, "IsWindow", lambda _h: True)
    monkeypatch.setattr(auto_nav, "force_foreground", lambda _h: True)
    monkeypatch.setattr(auto_nav, "prepare_startup_pointer", lambda h: configured.append(("pointer", h)))

    def make_env(*args, **kwargs):
        expected = ["settled", settled_hwnd]
        if backend == "dxcam":
            expected.append(("pointer", settled_hwnd))
        assert configured == expected
        return env

    monkeypatch.setattr(auto_nav, "make_env", make_env)
    cfg = SimpleNamespace(name="demo", process="Demo.exe", menu_skip=[],
                          screenshot_backend=backend, auto_launch=SimpleNamespace(
                              launch_path="Demo.exe", settle_seconds=60))
    actual_env, hwnd, _ = auto_nav.start_auto_launch_session(
        cfg, after_window=configured.append)
    assert actual_env is env
    assert hwnd == settled_hwnd


def test_session_waits_configured_window_budget_after_bootstrap(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    env = SimpleNamespace(reset=lambda: None, close=lambda: None,
                          render=lambda: Image.new("RGB", (16, 9), "black"))
    monkeypatch.setattr(auto_nav, "launch_with_pads", lambda *_a, **_k:
                        SimpleNamespace(pads=None))
    waits = []
    monkeypatch.setattr(auto_nav, "wait_for_game_hwnd", lambda _p, timeout:
                        waits.append(timeout) or (7, "Demo"))
    monkeypatch.setattr(auto_nav, "settle_with_foreground", lambda *_a, **_k: 7)
    monkeypatch.setattr(auto_nav.win32gui, "IsWindow", lambda _h: True)
    monkeypatch.setattr(auto_nav, "force_foreground", lambda _h: True)
    monkeypatch.setattr(auto_nav, "make_env", lambda *_a, **_k: env)
    cfg = SimpleNamespace(name="demo", process="Demo.exe", menu_skip=[],
                          screenshot_backend="auto", auto_launch=SimpleNamespace(
                              launch_path="Demo.exe", settle_seconds=60,
                              window_wait_seconds=180))
    auto_nav.start_auto_launch_session(cfg)
    assert waits == [180.0]


def test_stable_window_wait_follows_bootstrap_gap_without_relaunch(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    clock = [0.0]
    monkeypatch.setattr(auto_nav.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(auto_nav.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(auto_nav, "find_game_hwnd", lambda _p:
                        (7, "Bootstrap") if clock[0] < 1 else
                        None if clock[0] < 3 else (8, "Game"))
    assert auto_nav.wait_for_stable_game_window("Demo.exe", 10, stable_seconds=2)
    assert clock[0] == 5.0


def test_stable_window_wait_does_not_accept_transient_window(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    clock = [0.0]
    monkeypatch.setattr(auto_nav.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(auto_nav.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(auto_nav, "find_game_hwnd", lambda _p:
                        (7, "Bootstrap") if clock[0] < 1 else None)
    assert not auto_nav.wait_for_stable_game_window("Demo.exe", 4, stable_seconds=2)
    assert clock[0] == 4.0


@pytest.mark.parametrize("loading_style", [0x94000000, 0x94800000])
def test_aimlab_stability_starts_after_same_hwnd_leaves_loading(
        monkeypatch, loading_style):
    from coding4game_gui.benchmark.windows import auto_nav

    clock = [0.0]
    monkeypatch.setattr(auto_nav.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(auto_nav.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(auto_nav, "find_game_hwnd", lambda _p: (7, "aimlab_tb"))
    monkeypatch.setattr(auto_nav.win32gui, "GetWindowLong", lambda *_a:
                        loading_style if clock[0] < 2 else 0x14cf0000)
    assert auto_nav.wait_for_stable_game_window("AimLab_tb.exe", 10, stable_seconds=2)
    assert clock[0] == 4.0


def test_riders_stability_waits_for_windowed_main_window(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    clock = [0.0]
    monkeypatch.setattr(auto_nav.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(auto_nav.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(auto_nav, "find_game_hwnd", lambda _p: (7, "Riders Republic"))
    monkeypatch.setattr(auto_nav.win32gui, "GetClassName", lambda _h:
                        "ScimitarSplashScreenWindow" if clock[0] < 2 else "ScimitarEngineWindowClass")
    monkeypatch.setattr(auto_nav.win32gui, "IsIconic", lambda _h: 2 <= clock[0] < 4)
    def client_rect(_hwnd):
        if clock[0] < 6:
            return (0, 0, 0, 0)
        if clock[0] < 8:
            return (0, 0, 1258, 664)
        return (0, 0, 1600, 895)

    monkeypatch.setattr(auto_nav.win32gui, "GetClientRect", client_rect)
    monkeypatch.setattr(auto_nav.win32gui, "GetWindowLong", lambda *_args:
                        0x94cf0000 if clock[0] < 8 else 0x14cf0000)
    assert auto_nav.wait_for_stable_game_window("RidersRepublic.exe", 12, stable_seconds=2)
    assert clock[0] == 10.0


@pytest.mark.parametrize("titles,expected", [
    (["D3DProxyWindow", "TrackMania Nations Forever"], 2),
    (["TrackMania Nations Forever", "D3DProxyWindow"], 1),
    (["D3DProxyWindow"], None),
    (["Game overlay", "Pal  "], 2),
])
def test_startup_and_bundle_select_the_same_non_proxy_window(monkeypatch, titles, expected):
    import psutil
    from coding4game_gui.benchmark.windows import auto_nav
    from coding4game_gui.benchmark.windows.drivers.windows_xinput.lifecycle import select_game_window
    from coding4game_gui.benchmark.windows.mouse_driver import find_game_window

    windows = [{"hwnd": index, "title": title} for index, title in enumerate(titles, 1)]
    monkeypatch.setattr(auto_nav.psutil, "process_iter", lambda _attrs: [
        SimpleNamespace(info={"pid": 10, "name": "Game.exe"})])
    monkeypatch.setattr(auto_nav.win32process, "GetWindowThreadProcessId", lambda _h: (1, 10))
    monkeypatch.setattr(auto_nav.win32gui, "IsWindowVisible", lambda _h: True)
    monkeypatch.setattr(auto_nav.win32gui, "GetWindowText", lambda h: titles[h - 1])

    def enumerate_windows(callback, parameter):
        for window in windows:
            callback(window["hwnd"], parameter)

    monkeypatch.setattr(auto_nav.win32gui, "EnumWindows", enumerate_windows)
    monkeypatch.setattr(psutil, "Process", lambda _pid: SimpleNamespace(name=lambda: "Game.exe"))
    selected = select_game_window(windows)
    assert (selected["hwnd"] if selected else None) == expected
    hit = auto_nav.find_game_hwnd("Game.exe")
    assert (hit[0] if hit else None) == expected
    assert find_game_window("Game") == expected


def test_aimlab_loading_timeout_does_not_accept_a_persistent_splash(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    clock = [0.0]
    monkeypatch.setattr(auto_nav.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(auto_nav.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(auto_nav, "find_game_hwnd", lambda _p: (7, "aimlab_tb"))
    monkeypatch.setattr(auto_nav.win32gui, "GetWindowLong", lambda *_a: 0x94000000)
    assert not auto_nav.wait_for_stable_game_window("AimLab_tb.exe", 4, stable_seconds=2)
    assert clock[0] == 4.0


def test_aimlab_settle_restarts_when_same_hwnd_returns_to_fullscreen_loading(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    clock = [0.0]
    monkeypatch.setattr(auto_nav.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(auto_nav.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(auto_nav, "find_game_hwnd", lambda _p: (7, "aimlab_tb"))
    monkeypatch.setattr(auto_nav.win32gui, "GetWindowLong", lambda *_a:
                        0x94000000 if 2 <= clock[0] < 4 else 0x14cf0000)
    monkeypatch.setattr(auto_nav, "force_foreground", lambda _h: True)
    assert auto_nav.settle_with_foreground(
        7, 4, interval=1, process_name="AimLab_tb.exe", handoff_timeout_seconds=5) == 7
    assert clock[0] == 8


def test_full_verifier_does_not_focus_suspended_game_after_menu_skip(
        monkeypatch, tmp_path):
    from scripts.calibration import test_auto_launch_full

    class _SessionEnv:
        async def render_locked(self):
            return Image.new("RGB", (16, 9), "black")

        def unpause(self):
            cleanup.append("unpause")

        def close(self):
            cleanup.append("close")

    cleanup = []
    env = _SessionEnv()
    cfg = SimpleNamespace(
        process="Demo.exe",
        auto_launch=SimpleNamespace(launch_path="Demo.exe"),
        menu_skip=[SimpleNamespace(pad=0)],
    )
    focused = []
    handoffs = []
    controller = SimpleNamespace(
        platform="windows-kbm",
        tics_per_second=10,
        _force_foreground=lambda session_env: focused.append(7) or True,
        replay_menu_skip=lambda *_a: None,
        prepare_policy_handoff=lambda session_env: (
            handoffs.append(session_env) or True),
    )

    def _start(_cfg, **kwargs):
        assert kwargs["platform_name"] == "windows-kbm"
        assert kwargs["env_fps"] == 10
        kwargs["post_settle"](env, 7)
        return env, 7, "Demo"

    monkeypatch.setattr(test_auto_launch_full, "ROOT", tmp_path)
    monkeypatch.setattr(
        test_auto_launch_full, "load_game_spec",
        lambda _scenario: {"driver_game": "demo"},
    )
    monkeypatch.setattr(
        test_auto_launch_full, "load_game_config", lambda _game: cfg)
    monkeypatch.setattr(
        test_auto_launch_full, "ResetController",
        lambda _spec: controller,
    )
    monkeypatch.setattr(
        test_auto_launch_full, "start_auto_launch_session", _start)
    monkeypatch.setattr(test_auto_launch_full.time, "sleep", lambda _s: None)
    monkeypatch.setattr(test_auto_launch_full.ss, "kill_process", lambda _p: cleanup.append("kill"))
    monkeypatch.setattr(
        test_auto_launch_full.sys, "argv",
        ["test_auto_launch_full.py", "--scenario", "demo"],
    )

    assert test_auto_launch_full.main() == 0
    assert focused == [7]
    assert handoffs == [env]
    assert cleanup == ["unpause", "close", "kill"]


def test_scorer_recorder_can_capture_frozen_post_menu_frame_without_focus(
        monkeypatch, tmp_path):
    import runpy
    from pathlib import Path

    recorder = runpy.run_path(str(
        Path(".agents/skills/add-new-game/scripts/record_scorer_session.py")))
    focused = []
    recorder["force_foreground"] = lambda hwnd: focused.append(hwnd) or True
    monkeypatch.setattr(recorder["time"], "sleep", lambda _seconds: None)

    class _SessionEnv:
        async def render_locked(self):
            return Image.new("RGB", (16, 9), "black")

    out = tmp_path / "02_after_menu_skip.png"
    recorder["_capture_milestone"](
        _SessionEnv(), 7, out, require_focus=False)

    assert out.is_file()
    assert focused == []
