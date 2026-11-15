"""DoomEnv-shaped adapter: verb resolution, tic accounting, scorer wiring."""
from types import SimpleNamespace

import numpy as np
import pytest


@pytest.mark.parametrize('value', [True, 0, 1001, 1.5, '5'])
def test_invalid_runtime_max_action_tics_is_rejected(value):
    from coding4game_gui.benchmark.windows.timing import resolve_max_action_tics
    with pytest.raises(ValueError, match='runtime.max_action_tics'):
        resolve_max_action_tics({'max_action_tics': value})


def test_omitted_runtime_max_action_tics_preserves_global_default():
    from coding4game_gui.benchmark.windows.timing import resolve_max_action_tics
    assert resolve_max_action_tics({}) == 1000
from PIL import Image

from coding4game_gui.benchmark.windows.games_config import ActionSpec
from coding4game_gui.benchmark.windows.env import WindowsGameEnv


class FakeDriverEnv:
    """Records step() kwargs; returns a solid PIL frame. No real timing."""

    def __init__(self):
        self.steps = []
        self.frozen_refreshes = 0
        self.pad_states = []
        self.reset_pads = []
        self.pause_calls = 0
        self.unpause_calls = 0
        self.n_players = 2
        self.gamepad = self
        self.input = self

    def step(self, **kw):
        self.steps.append(kw)
        return Image.new("RGB", (32, 24), (10, 20, 30))

    def render(self):
        return Image.new("RGB", (32, 24), (10, 20, 30))

    def refresh_frozen_frame(self):
        self.frozen_refreshes += 1
        return Image.new("RGB", (32, 24), (99, 88, 77))

    def apply_state(self, idx, **state):
        self.pad_states.append((idx, state))

    def reset_pad(self, idx):
        self.reset_pads.append(idx)

    def pause(self):
        self.pause_calls += 1

    def unpause(self):
        self.unpause_calls += 1

    def freeze_now(self):
        pass

    def close(self):
        pass


class FakeScorer:
    def __init__(self, deltas=(1.0, 2.0, 3.0)):
        self.deltas = list(deltas)
        self.resets = 0
        self.terminal_checks = 0

    def reset(self):
        self.resets += 1

    def delta(self, frame):
        return self.deltas.pop(0) if self.deltas else 0.0

    def is_terminal(self, frame):
        self.terminal_checks += 1
        return self.terminal_checks >= len(self.deltas)

    def classify_phase(self, frame):
        return "combat"

    @property
    def total(self):
        return 0.0


SPEC = {"engine": "windows", "buttons": ["MOVE_UP", "CONFIRM"],
        "runtime": {"scenario": "brotato"}}


def _env(scorer=None, *, spec=None):
    actions = {"MOVE_UP": ActionSpec(name="move_up", description="",
                                     left_stick=(0.0, 1.0)),
               "CONFIRM": ActionSpec(name="confirm", description="",
                                     buttons=["A"])}
    return WindowsGameEnv("brotato", driver_env=FakeDriverEnv(),
                          scorer=scorer or FakeScorer(), actions=actions,
                          spec=SPEC if spec is None else spec)


def test_unknown_token_raises_before_touching_the_driver():
    env = _env()
    env.reset(episode_id=0)
    with pytest.raises(ValueError):
        env.act(["FLY"], 3)
    assert env._env.steps == []


def test_opt_in_frozen_refresh_replaces_stale_step_frame_without_extra_tics(monkeypatch):
    delays = []
    monkeypatch.setattr("coding4game_gui.benchmark.windows.env.time.sleep", delays.append)
    spec = {**SPEC, "runtime": {
        "scenario": "brotato", "post_action_observation_delay_seconds": 0.12}}
    env = WindowsGameEnv(
        "brotato", driver_env=FakeDriverEnv(), scorer=FakeScorer(),
        actions={"MOVE_UP": ActionSpec(name="move_up", description="",
                                      left_stick=(0.0, 1.0))}, spec=spec)
    env.reset(episode_id=0)
    env.act(["MOVE_UP"], 1)
    assert delays == [0.12]
    assert env._env.frozen_refreshes == 1
    assert tuple(env.frame()[0, 0]) == (77, 88, 99)
    assert env.tic == 1


def test_act_preserves_long_tics_and_holds_duration():
    env = _env()
    env.reset(episode_id=0)
    env.act(["MOVE_UP"], 7)
    env.act(["CONFIRM"], 99)
    assert env.tic == 7 + 99
    assert env._env.steps[0]["duration_seconds"] == pytest.approx(0.7)
    assert env._env.steps[1]["duration_seconds"] == pytest.approx(9.9)
    assert env._env.steps[0]["left_stick"] == (0.0, 1.0)
    assert env._env.steps[1]["buttons"] == ["A"]


def test_stepped_focus_preparation_runs_inside_driver_pulse():
    env = _env()
    prepared = []
    env.reset(episode_id=0)
    env._controller = SimpleNamespace(
        alive=lambda: True,
        prepare_input_foreground=lambda driver: prepared.append(driver))

    env.act(["CONFIRM"], 1)

    assert prepared == []
    env._env.steps[0]["before_input"]()
    assert prepared == [env._env]


def test_action_tic_cap_accepts_boundary_and_rejects_before_input():
    env = _env(FakeScorer(deltas=(0.,) * 10))
    env.reset()
    env.act(['CONFIRM'], 1000)
    assert env.tic == 1000
    with pytest.raises(ValueError, match='1000'):
        env.act(['CONFIRM'], 1001)
    with pytest.raises(ValueError, match='1000'):
        env.validate_action_plan([(['CONFIRM'], 1), (['CONFIRM'], 1001)])
    assert len(env._env.steps) == 1 and env.tic == 1000


def test_scenario_action_tic_cap_rejects_whole_plan_before_input():
    spec = {**SPEC, "runtime": {**SPEC["runtime"], "max_action_tics": 7}}
    env = _env(FakeScorer(deltas=(0.,) * 10), spec=spec)
    env.reset()

    with pytest.raises(ValueError, match="7"):
        env.validate_action_plan([(["CONFIRM"], 1), (["CONFIRM"], 8)])

    assert env._env.steps == [] and env.tic == 0


def test_measured_running_seconds_accumulate_without_per_action_rounding():
    env = _env(FakeScorer(deltas=(0.,) * 10))
    env.reset()
    def step(**kw):
        env._env.last_step_timing = SimpleNamespace(
            requested_s=.2, live_window_s=.26, step_wall_s=1.5, interrupted=False)
        return env._env.render()
    env._env.step = step
    env.act(["CONFIRM"], 2)
    assert env.tic == 3
    env.act(["CONFIRM"], 2)
    assert env.tic == 5
    assert env.game_running_seconds == pytest.approx(.52)


def test_running_tics_round_half_up():
    env = _env()
    env.reset()
    env._charge_running_time(.25)
    assert env.tic == 3


@pytest.mark.parametrize('value', [True, 0, 1001, 1.5, '3'])
def test_passive_scoring_interval_validation_precedes_gameplay(value):
    with pytest.raises(ValueError, match='passive_score_interval_tics'):
        _env(spec={**SPEC, 'runtime': {'passive_score_interval_tics': value}})


def test_passive_scoring_preserves_order_without_online_scoring_or_extra_actions():
    scorer = FakeScorer(deltas=(0.,) * 20)
    env = _env(scorer, spec={**SPEC, 'runtime': {
        **SPEC['runtime'], 'passive_score_interval_tics': 3}})
    env.reset()
    def step(**kwargs):
        env._env.steps.append(kwargs)
        # These are native elapsed times, independent of RPC/resume overhead.
        for elapsed in [.1, .3, .4, .6, .91]:
            kwargs['on_sample'](env._env.render(), elapsed)
            assert env.total_reward is None and len(scorer.deltas) == 20
        env._env.last_step_timing = SimpleNamespace(
            requested_s=1., live_window_s=1.01, interrupted=False)
        return env._env.render()
    env._env.step = step
    env.act(['CONFIRM'], 10)
    assert len(env._env.steps) == 1 and env._env.steps[0]['duration_seconds'] == 1.
    assert env._env.steps[0]['sample_interval'] == pytest.approx(.3)
    assert env._deferred_score.frame_tics == [3, 6, 9, 10]
    assert scorer.terminal_checks == 1  # Frozen endpoint only.
    assert env.tic == 10 and env.game_running_seconds == pytest.approx(1.01)
    assert env.total_reward is None
    assert env.finalize_score() == 0. and len(scorer.deltas) == 16


def test_scenario_tic_rate_controls_real_action_duration():
    driver = FakeDriverEnv()
    actions = {"MOVE_UP": ActionSpec(name="move_up", description="",
                                     left_stick=(0.0, 1.0))}
    spec = SPEC | {"runtime": {
        "scenario": "brotato", "tics_per_second": 20}}
    env = WindowsGameEnv("brotato", driver_env=driver,
                         scorer=FakeScorer(), actions=actions, spec=spec)
    env.reset(episode_id=0)

    env.act(["MOVE_UP"], 1)

    assert env.tics_per_second == 20
    assert env.TICRATE == 20
    assert driver.steps[0]["duration_seconds"] == pytest.approx(0.05)


@pytest.mark.parametrize("tics", [0, -1, True, 1.5, "3"])
def test_invalid_duration_has_no_driver_side_effects(tics):
    env = _env()
    env.reset()
    with pytest.raises(ValueError, match="positive integer"):
        env.validate_action_plan([(["CONFIRM"], 1), (["CONFIRM"], tics)])
    with pytest.raises(ValueError, match="positive integer"):
        env.act(["CONFIRM"], tics)
    assert env._env.steps == [] and env.tic == 0


def test_long_click_is_injected_once_not_held_or_repeated():
    driver = FakeDriverEnv()
    env = WindowsGameEnv("balatro", driver_env=driver, scorer=FakeScorer(),
                         actions={}, spec=SPEC | {"scorer": {
                             "calibrated_client_size": [1280, 768]}},
                         windows_input="gamepad-kbm")
    env.reset()
    clicks = []
    env._do_clicks = lambda points: clicks.append(points)
    def step(**kw):
        driver.steps.append(kw)
        kw["live_action"]()
        return driver.render()
    driver.step = step
    env.act(["CLICK[640,384]"], 100)
    assert len(clicks) == 1
    assert len(driver.steps) == 1
    assert driver.steps[0]["duration_seconds"] == 10
    assert env.tic == 100


def test_long_hold_checks_terminal_only_on_final_frame():
    driver = FakeDriverEnv()
    env = WindowsGameEnv("balatro", driver_env=driver,
                         scorer=RedIsDeath(deltas=(0,) * 9), actions={},
                         spec=SPEC | {"lifecycle": {"terminal_scan_tics": 1}},
                         windows_input="gamepad-kbm")
    env.reset()
    def step(**kw):
        assert "frame_sink" not in kw
        assert kw["on_frame"] is None
        driver.last_step_timing = SimpleNamespace(
            requested_s=10., live_window_s=10., interrupted=False)
        return Image.new("RGB", (32, 24), (255, 0, 0))
    driver.step = step
    env.act(["KEY[A]"], 100)
    assert env.tic == 100 and env.done
    assert env.frame()[0, 0, 2] == 255


def test_failed_stream_retains_consumed_tics():
    env = _env()
    env.reset()
    def step(**kw):
        env._env.last_step_timing = SimpleNamespace(live_window_s=.34)
        raise RuntimeError("capture failed")
    env._env.step = step
    with pytest.raises(RuntimeError, match="capture failed"):
        env.act(["CONFIRM"], 100)
    assert env.tic == 3


def test_slow_terminal_ocr_does_not_extend_action():
    import time
    driver = FakeDriverEnv()
    driver.live = False
    reads = []

    class SlowScorer(FakeScorer):
        def is_terminal(self, frame):
            assert not driver.live, "OCR must run after freeze, never during input"
            time.sleep(.03)
            reads.append("terminal")
            return False

        def delta(self, frame):
            assert not driver.live
            return 0.

    env = WindowsGameEnv("balatro", driver_env=driver, scorer=SlowScorer(),
                         actions={}, windows_input="gamepad-kbm",
                         spec=SPEC | {"lifecycle": {"terminal_scan_tics": 1}})
    env.reset()
    reads.clear()
    def step(**kw):
        driver.live = True
        started = time.perf_counter()
        try:
            assert kw['on_frame'] is None and kw['sample_interval'] == 0
            elapsed = time.perf_counter() - started
            assert reads == []
        finally:
            driver.live = False
        driver.last_step_timing = SimpleNamespace(
            requested_s=1., live_window_s=1. + max(0., elapsed - .1), interrupted=False)
        assert elapsed < .1, "OCR blocked the live action callback"
        return driver.render()
    driver.step = step
    env.act(["NEUTRAL"], 10)
    assert env.tic == 10
    assert reads == ['terminal']


@pytest.mark.parametrize(
    "requested,click_elapsed,live_elapsed,wall_elapsed,charged,warns",
    [(10, 0.074, 1.0, 1.07, 10, False),
     # Captured Balatro shape: fixed resume/suspend/render overhead must not
     # turn a correct 0.10s live pulse into an action-timing warning.
     (1, 0.06, 0.10, 0.17, 1, False),
     (1, 0.24, 0.24, 0.31, 2, True)],
)
def test_click_pulse_warns_on_live_time_not_outer_step_wall(
        monkeypatch, capsys, requested, click_elapsed, live_elapsed,
        wall_elapsed, charged, warns):
    import coding4game_gui.benchmark.windows.env as env_mod

    driver = FakeDriverEnv()
    actions = {"CLICK": ActionSpec(name="click", description="",
                                    click=(10.0, 10.0))}
    spec = SPEC | {"buttons": ["CLICK"]}
    env = WindowsGameEnv("balatro", driver_env=driver,
                         scorer=FakeScorer(), actions=actions, spec=spec)
    env.reset(episode_id=0)
    now = [0.0]

    def click(_points):
        now[0] += click_elapsed

    def step(**kw):
        driver.steps.append(kw)
        kw["live_action"]()
        driver.last_step_timing = SimpleNamespace(
            requested_s=requested / env.TICRATE,
            live_window_s=live_elapsed,
            step_wall_s=wall_elapsed,
            step_overhead_s=wall_elapsed - live_elapsed,
        )
        now[0] = wall_elapsed
        return Image.new("RGB", (32, 24), (10, 20, 30))

    env._do_clicks = click
    driver.step = step
    monkeypatch.setattr(env_mod.time, "perf_counter", lambda: now[0])

    env.act(["CLICK"], requested)

    assert driver.steps[0]["duration_seconds"] == pytest.approx(
        requested / env.TICRATE)
    assert env.tic == pytest.approx(charged)
    output = capsys.readouterr().out
    assert ("act timing" in output) is warns
    assert "asked 0.00s" not in output
    if warns:
        assert "asked 0.10s live 0.24s, wall 0.31s" in output


def test_move_pulse_hovers_for_requested_duration_and_charges_overrun(
        monkeypatch, capsys):
    import coding4game_gui.benchmark.windows.env as env_mod

    driver = FakeDriverEnv()
    env = WindowsGameEnv(
        "balatro", driver_env=driver, scorer=FakeScorer(), actions={},
        spec=SPEC | {"scorer": {"calibrated_client_size": [1280, 768]}},
        windows_input="gamepad-kbm")
    env.reset(episode_id=0)
    now = [0.0]

    def move(_points):
        now[0] += 0.24

    def step(**kw):
        driver.steps.append(kw)
        kw["live_action"]()
        driver.last_step_timing = SimpleNamespace(
            requested_s=0.1, live_window_s=0.24,
            step_wall_s=0.31, step_overhead_s=0.07)
        now[0] = 0.31
        return Image.new("RGB", (32, 24), (10, 20, 30))

    env._do_moves = move
    driver.step = step
    monkeypatch.setattr(env_mod.time, "perf_counter", lambda: now[0])

    env.act(["MOVE[640,384]"], 1)

    assert driver.steps[0]["duration_seconds"] == pytest.approx(0.1)
    assert env.tic == 2
    output = capsys.readouterr().out
    assert "asked 0.10s live 0.24s, wall 0.31s, move delivery 0.24s" in output
    assert "asked 0.00s" not in output


def test_reward_and_done_come_from_the_scorer():
    env = _env(FakeScorer(deltas=(5.0, 0.0)))
    env.reset(episode_id=3)
    assert env.act(["MOVE_UP"], 1) is None
    assert not env.done
    assert env.act(["MOVE_UP"], 1) is None
    assert env.done
    assert env.total_reward is None
    assert env.finalize_score() == 5.0


def test_terminal_end_reason_reports_the_classified_phase():
    class VictoryScorer(FakeScorer):
        def classify_phase(self, frame):
            return "victory"

    env = _env(VictoryScorer(deltas=(1.0,)))
    env.reset(episode_id=3)
    env.act(["MOVE_UP"], 1)

    assert env.done
    assert env.end_reason == 'terminal:victory'


def test_frame_is_bgr_uint8():
    env = _env()
    env.reset(episode_id=0)
    env.act(["MOVE_UP"], 1)
    f = env.frame()
    assert f.dtype == np.uint8 and f.shape == (24, 32, 3)
    assert tuple(f[0, 0]) == (30, 20, 10)      # RGB(10,20,30) -> BGR


def test_reset_zeroes_counters_and_resets_scorer():
    env = _env()
    env.reset(episode_id=0)
    env.act(["MOVE_UP"], 5)
    env.reset(episode_id=1)
    assert env.tic == 0 and env.total_reward is None and not env.done
    assert env._scorer.resets == 2


def test_case_insensitive_tokens():
    env = _env()
    env.reset(episode_id=0)
    env.act(["move_up"], 1)                # code arm may emit lowercase
    assert env._env.steps[0]["left_stick"] == (0.0, 1.0)


def test_episode_clicks_use_calibrated_coordinate_space(
        monkeypatch):
    import coding4game_gui.benchmark.windows.env as env_mod

    seen = {}

    def fake_click(process, driver_env, points, content_size, *, atomic=False,
                   coordinate_size=None, require_game_target=False):
        seen.update(process=process, driver_env=driver_env, points=points,
                    content_size=content_size, atomic=atomic,
                    coordinate_size=coordinate_size,
                    require_game_target=require_game_target)

    spec = SPEC | {"scorer": {"calibrated_client_size": [1536, 864]}}
    env = WindowsGameEnv("balatro", driver_env=FakeDriverEnv(),
                         scorer=FakeScorer(), actions={
                             "CLICK": ActionSpec(
                                 name="click", description="", click=(1, 2))},
                         controller=SimpleNamespace(
                             cfg=SimpleNamespace(process="Balatro.exe")),
                         spec=spec)
    monkeypatch.setattr(env_mod, "_click_game_points", fake_click)

    env._do_clicks([(474, 605)])

    assert seen["content_size"] == (1536, 864)
    assert seen["atomic"] is True
    assert seen["coordinate_size"] == (1536, 864)
    assert seen["require_game_target"] is True
    assert env._render_size is None


def test_episode_moves_use_calibrated_coordinates_without_parking(monkeypatch):
    import coding4game_gui.benchmark.windows.env as env_mod

    seen = {}

    def fake_move(process, driver_env, points, content_size, *,
                  coordinate_size=None):
        seen.update(process=process, driver_env=driver_env, points=points,
                    content_size=content_size,
                    coordinate_size=coordinate_size)

    spec = SPEC | {"scorer": {"calibrated_client_size": [1536, 864]}}
    env = WindowsGameEnv(
        "balatro", driver_env=FakeDriverEnv(), scorer=FakeScorer(), actions={},
        controller=SimpleNamespace(cfg=SimpleNamespace(process="Balatro.exe")),
        spec=spec, windows_input="gamepad-kbm")
    monkeypatch.setattr(env_mod, "_move_game_points", fake_move)

    env._do_moves([(552, 293)])

    assert seen == {
        "process": "Balatro.exe", "driver_env": env._env,
        "points": [(552, 293)], "content_size": (1536, 864),
        "coordinate_size": (1536, 864),
    }


class SequenceDriverEnv(FakeDriverEnv):
    """step() returns frames from a programmable sequence (last one repeats)."""

    def __init__(self, frames):
        super().__init__()
        self.frames = list(frames)

    def step(self, **kw):
        self.steps.append(kw)
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]


def _img(color):
    return Image.new("RGB", (32, 24), color)


def _img_patch(color, patch=(255, 255, 255)):
    """Base color with a 2x2 patch (4/768 = 0.5%% of pixels) — a 'cursor'."""
    im = Image.new("RGB", (32, 24), color)
    for x in range(2):
        for y in range(2):
            im.putpixel((x, y), patch)
    return im


def test_visual_watchdogs_are_not_called_when_terminal_detection_is_disabled():
    class NoOnlineScorer(FakeScorer):
        def delta(self, frame):
            raise AssertionError("online scoring")
        def is_terminal(self, frame):
            raise AssertionError("online terminal scan")
        def classify_phase(self, frame):
            raise AssertionError("online phase scan")
        def text_signature(self, frame):
            raise AssertionError("online text scan")
    env = _env(NoOnlineScorer())
    env.terminal_detection = False
    env.reset()
    for _ in range(5):
        env.act(["CONFIRM"], 100)
    assert env.tic == 500 and not env.done and env.total_reward is None


def test_registered_aimlab_defers_all_scoring_until_fixed_endpoint(monkeypatch):
    from coding4game_gui.benchmark.spec import load_game_spec

    class NoOnlineScorer(FakeScorer):
        delta_calls = 0

        def delta(self, frame):
            self.delta_calls += 1
            return 0.0

        def is_terminal(self, frame):
            raise AssertionError("fixed-duration Aimlab must not scan during play")

        def classify_phase(self, frame):
            raise AssertionError("fixed-duration Aimlab must not OCR during play")

    spec = load_game_spec("aimlab", windows_input="gamepad-kbm")
    assert spec["runtime"]["max_tics"] == 350
    assert spec["runtime"]["max_calls_per_ep"] == 240
    assert spec["scorer"]["ocr"]["device"] == "gpu:0"
    monkeypatch.setattr("coding4game_gui.benchmark.windows.env.time.sleep", lambda _s: None)
    scorer = NoOnlineScorer()
    env = WindowsGameEnv(
        "aimlab", driver_env=FakeDriverEnv(), scorer=scorer, actions={},
        spec=spec, windows_input="gamepad-kbm")
    env.reset()
    for _ in range(7):
        env.act(["NEUTRAL"], 50)
        assert env.total_reward is None and scorer.delta_calls == 0
        assert not env.done
    assert env.tic == 350 and len(env._env.steps) == 7
    assert env.finalize_score() == 0.0 and scorer.delta_calls == 7
    assert env.finalize_score() == 0.0 and scorer.delta_calls == 7


class SinkDriverEnv(FakeDriverEnv):
    """step() streams programmable PIL frames until its consumer stops."""

    def __init__(self, sink_frames, final):
        super().__init__()
        self.sink_frames = list(sink_frames)
        self.final = final

    def step(self, **kw):
        self.steps.append(kw)
        if kw.get("on_frame") is not None:
            for frame in self.sink_frames:
                if kw["on_frame"](frame):
                    break
        return self.final


class RedIsDeath(FakeScorer):
    """Terminal iff the frame is dominantly red (BGR channel 2)."""

    def is_terminal(self, frame):
        return frame[0, 0, 2] > 200


def test_transient_mid_action_terminal_is_not_used_for_scoring():
    normal, death, after = (_img((10, 20, 30)), _img((255, 0, 0)),
                            _img((0, 255, 0)))
    # the death screen appears MID-act and the act ends on a post-death
    # frame (results page) — without the scan the episode would continue.
    drv = SinkDriverEnv([normal, death, after], final=after)
    actions = {"CONFIRM": ActionSpec(name="confirm", description="",
                                     buttons=["A"]),
               "MOVE_UP": ActionSpec(name="move_up", description="",
                                     left_stick=(0.0, 1.0))}
    spec = SPEC | {"lifecycle": {"terminal_scan_tics": 1,
                                 "nonprogress_window_tics": 0,
                                 "still_window_tics": 0}}
    env = WindowsGameEnv("brotato", driver_env=drv,
                         scorer=RedIsDeath(deltas=(0.0,) * 9),
                         actions=actions, spec=spec)
    env.reset(episode_id=0)
    env.act(["CONFIRM"], 5)
    assert not env.done
    # Explicit trade-off: only the final frozen frame is authoritative.
    assert env.frame()[0, 0, 2] == 0 and env.frame()[0, 0, 1] == 255


def test_reset_controller_restores_checkpoint_to_driver_save_directory(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav, games_config, save_state
    from coding4game_gui.benchmark.windows import env as env_module
    from coding4game_gui.benchmark.windows.env import ResetController

    declared_save = object()
    cfg = SimpleNamespace(process="fake-game.exe", save_state=declared_save,
                          menu_skip=[], actions={}, use_speedhack=True)
    seen = {}
    other_mouse = object()
    monkeypatch.setitem(env_module._MOUSE_DRIVERS, "fake-game.exe", object())
    monkeypatch.setitem(env_module._MOUSE_DRIVERS, "other-game.exe", other_mouse)
    fake_env = SimpleNamespace(pause=lambda: seen.setdefault("paused", True),
                               hard_pause=False)
    monkeypatch.setattr(games_config, "load_game_config", lambda _name: cfg)
    monkeypatch.setattr(save_state, "kill_process", lambda _name: 0)
    monkeypatch.setattr(save_state, "resolve_for_run",
                        lambda *args: seen.setdefault("restore_args", args))
    monkeypatch.setattr(save_state, "restore", lambda *_args: {})

    def start(config, **kwargs):
        assert config is cfg
        assert "fake-game.exe" not in env_module._MOUSE_DRIVERS
        assert env_module._MOUSE_DRIVERS["other-game.exe"] is other_mouse
        kwargs["before_launch"]()
        seen.update(kwargs)
        return fake_env, 7, "Fake"

    monkeypatch.setattr(auto_nav, "start_auto_launch_session", start)
    controller = ResetController({
        "driver_game": "black_myth",
        "runtime": {"tics_per_second": 20},
        "startup": {"golden_checkpoint": "golden", "menu_skip_replay": "open_loop"},
    })
    assert controller._boot() is fake_env
    assert seen["restore_args"] == ("black_myth", declared_save, "golden")
    assert seen["env_fps"] == 20
    assert seen["attempts"] == 3
    assert seen["use_speedhack"] is True
    assert seen["menu_skip_replayer"] == controller.replay_menu_skip
    assert callable(seen["on_attempt_failed"])
    assert callable(seen["window_ok"])
    assert fake_env.hard_pause is True
    assert seen["paused"] is True


def test_shared_start_rejects_required_speedhack_noop_fallback(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav

    closed = []
    env = SimpleNamespace(
        use_speedhack=False,
        close=lambda: closed.append(True),
        reset=lambda: pytest.fail("must reject before pad wakeup"),
    )
    monkeypatch.setattr(auto_nav, "launch_with_pads",
                        lambda *_a, **_k: SimpleNamespace(pads=None))
    monkeypatch.setattr(auto_nav, "wait_for_game_hwnd", lambda *_a, **_k: (7, "Fake"))
    monkeypatch.setattr(auto_nav, "force_foreground", lambda *_a: True)
    monkeypatch.setattr(auto_nav, "settle_with_foreground", lambda *_a, **_k: None)
    monkeypatch.setattr(auto_nav, "make_env", lambda *_a, **_k: env)
    cfg = SimpleNamespace(
        name="fake", process="fake.exe", menu_skip=[], screenshot_backend="auto",
        auto_launch=SimpleNamespace(launch_path="fake.exe", settle_seconds=0),
    )
    with pytest.raises(RuntimeError, match="requires xspeedhack"):
        auto_nav.start_auto_launch_session(cfg, use_speedhack=True)
    assert closed == [True]


def test_reset_controller_can_disable_alt_focus_gesture(monkeypatch):
    from coding4game_gui.benchmark.windows import games_config
    from coding4game_gui.benchmark.windows.env import ResetController

    cfg = SimpleNamespace(process="fake-game.exe")
    monkeypatch.setattr(games_config, "load_game_config", lambda _name: cfg)

    controller = ResetController({
        "driver_game": "black_myth",
        "startup": {"focus_alt_key": False},
    })

    assert controller._send_focus_alt() is False


def test_reset_controller_open_loop_menu_skip_uses_shared_tui_replay(
        monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav
    from coding4game_gui.benchmark.windows import games_config
    from coding4game_gui.benchmark.windows.env import ResetController

    cfg = SimpleNamespace(process="fake-game.exe")
    monkeypatch.setattr(games_config, "load_game_config", lambda _name: cfg)
    controller = ResetController({
        "driver_game": "black_myth",
        "startup": {"menu_skip_replay": "open_loop"},
    })
    monkeypatch.setattr(controller, "_force_foreground", lambda _env: True)
    monkeypatch.setattr(
        controller, "_press_until_screen_changes",
        lambda *_a, **_k: pytest.fail("closed-loop retry must not run"),
    )

    calls = []
    monkeypatch.setattr(
        auto_nav, "run_pad_sequence",
        lambda env, steps, label, max_duration: calls.append(
            (env, steps, label, max_duration)),
    )
    env = SimpleNamespace(
        hard_pause=True,
        unpause=lambda: None,
        pause=lambda: None,
    )
    steps = [object(), object()]

    controller._navigate(env, steps)

    assert calls == [(env, steps, "menu_skip", 999.0)]
    assert env.hard_pause is True


@pytest.mark.parametrize("preserve_tail", [None, False, True])
def test_menu_skip_tail_policy(monkeypatch, preserve_tail):
    from coding4game_gui.benchmark.windows import auto_nav
    from coding4game_gui.benchmark.windows import games_config
    from coding4game_gui.benchmark.windows.env import ResetController

    cfg = SimpleNamespace(process="fake-game.exe")
    monkeypatch.setattr(games_config, "load_game_config", lambda _name: cfg)
    controller = ResetController({
        "driver_game": "black_myth",
        "runtime": {"tics_per_second": 10},
        "startup": {
            "menu_skip_replay": "open_loop",
            **({} if preserve_tail is None else {
                "menu_skip_preserve_tail": preserve_tail}),
            "start_check": {"pattern": "READY"},
        },
    })
    monkeypatch.setattr(controller, "_force_foreground", lambda _env: True)
    calls = []
    monkeypatch.setattr(
        auto_nav, "run_pad_sequence",
        lambda env, steps, label, max_duration: calls.append(
            (env, list(steps), label, max_duration)),
    )
    action = SimpleNamespace(
        pad=0,
        spec=SimpleNamespace(
            buttons=("A",), click=None, left_stick=(0.0, 0.0),
            right_stick=(0.0, 0.0), left_trigger=0.0,
            right_trigger=0.0, duration_seconds=0.1,
            description="confirm start",
        ),
    )
    trailing_wait = SimpleNamespace(
        pad=0,
        spec=SimpleNamespace(
            buttons=(), click=None, left_stick=(0.0, 0.0),
            right_stick=(0.0, 0.0), left_trigger=0.0,
            right_trigger=0.0, duration_seconds=3.0,
            description="wait 3.00s (trailing)",
        ),
    )
    env = SimpleNamespace(
        hard_pause=True, unpause=lambda: None, pause=lambda: None)

    controller.replay_menu_skip(env, [action, trailing_wait])

    expected = [action] if preserve_tail is False else [action, trailing_wait]
    assert calls == [(env, expected, "menu_skip", 999.0)]
    assert env.hard_pause is True


def test_registered_closed_loop_replays_complete_menu(monkeypatch, load_driver_metadata):
    from coding4game_gui.benchmark.spec import load_game_spec
    from coding4game_gui.benchmark.windows.env import ResetController

    controller = ResetController(load_game_spec("brotato"))
    calls = []
    monkeypatch.setattr(controller, "_force_foreground", lambda env: True)
    monkeypatch.setattr(controller, "_press_until_screen_changes",
                        lambda env, step: calls.append(step))
    monkeypatch.setattr("time.sleep", lambda seconds: calls.append(seconds))
    env = SimpleNamespace(hard_pause=True, unpause=lambda: None, pause=lambda: None)
    controller.replay_menu_skip(env)
    expected = [step if (step.spec.buttons or any(step.spec.left_stick)
                        or any(step.spec.right_stick) or step.spec.left_trigger
                        or step.spec.right_trigger)
                else step.spec.duration_seconds for step in controller.cfg.menu_skip]
    assert calls == expected
    assert env.hard_pause is True


def test_start_check_ocr_runs_only_while_game_is_hard_paused(monkeypatch):
    from coding4game_gui.benchmark.windows.env import ResetController

    class _PulseEnv:
        def __init__(self):
            self.hard_pause = True
            self.running = False
            self.pause_calls = 0
            self.unpause_calls = 0

        def pause(self):
            self.running = False
            self.pause_calls += 1

        def unpause(self):
            self.running = True
            self.unpause_calls += 1

        def render(self):
            return Image.new("RGB", (8, 8), "black")

    env = _PulseEnv()
    controller = object.__new__(ResetController)
    controller.cfg = SimpleNamespace(process="fake-game.exe")
    controller.tics_per_second = 10
    controller._start_check = {"pattern": "READY"}
    checks_while_running = []

    class _Scorer:
        def check_text(self, _frame, _pattern, rect=None):
            checks_while_running.append(env.running)
            return len(checks_while_running) >= 2

        def read_region(self, _frame, _name):
            raise AssertionError("no region checks configured")

    controller._scorer = _Scorer()
    slept = []

    def _sleep(seconds):
        assert env.running is True
        slept.append(seconds)

    monkeypatch.setattr("time.sleep", _sleep)

    assert controller.wait_start_ok(env, timeout=5.0) is True
    assert checks_while_running == [False, False]
    assert slept == [pytest.approx(0.1)]
    assert env.unpause_calls == 1
    assert env.running is False
    assert env.hard_pause is True


def test_failed_start_check_still_leaves_game_hard_paused(monkeypatch, tmp_path):
    from coding4game_gui.benchmark.windows import env as env_module
    from coding4game_gui.benchmark.windows.env import ResetController

    monkeypatch.setattr(env_module, "STARTUP_FAILURE_DIR", tmp_path)
    class _Env:
        hard_pause = False
        running = True

        def pause(self):
            self.running = False

        def unpause(self):
            self.running = True

        def render(self):
            assert not self.running
            return Image.new("RGB", (8, 8), "black")

    env = _Env()
    controller = object.__new__(ResetController)
    controller.cfg = SimpleNamespace(process="fake-game.exe")
    controller.tics_per_second = 10
    controller._start_check = {"pattern": "READY"}
    controller._scorer = SimpleNamespace(
        check_text=lambda *_a, **_k: False,
        read_region=lambda *_a, **_k: None,
    )

    assert controller.wait_start_ok(env, timeout=0.0) is False
    assert env.running is False
    assert env.hard_pause is True
    saved = list(tmp_path.glob("*.png"))
    assert len(saved) == 1
    assert Image.open(saved[0]).size == (8, 8)


@pytest.mark.parametrize("pad_state", [
    {"left_trigger": 1.0},
    {"right_trigger": 1.0},
    {"left_stick": (0.5, 0.0)},
    {"right_stick": (0.0, -0.5)},
])
def test_closed_loop_navigation_drives_non_button_pad_states(
        monkeypatch, pad_state):
    from coding4game_gui.benchmark.windows.env import ResetController

    controller = object.__new__(ResetController)
    controller._menu_skip_replay = "closed_loop"
    controller.cfg = SimpleNamespace(process="fake-game.exe")
    monkeypatch.setattr(controller, "_force_foreground", lambda _env: True)

    driven = []
    monkeypatch.setattr(
        controller, "_press_until_screen_changes",
        lambda _env, step: driven.append(step),
    )
    env = SimpleNamespace(
        hard_pause=True,
        unpause=lambda: None,
        pause=lambda: None,
    )
    values = {
        "buttons": [],
        "left_stick": (0.0, 0.0),
        "right_stick": (0.0, 0.0),
        "left_trigger": 0.0,
        "right_trigger": 0.0,
    }
    values.update(pad_state)
    step = SimpleNamespace(
        pad=0,
        spec=SimpleNamespace(
            click=None,
            duration_seconds=0.0,
            description="recorded non-button pad input",
            **values,
        ),
    )

    controller._navigate(env, [step])

    assert driven == [step]
    assert env.hard_pause is True


def test_palworld_capture_replay_preserves_recorded_waits(monkeypatch):
    from coding4game_gui.benchmark.windows import auto_nav
    from coding4game_gui.benchmark.windows import env as game_env
    from coding4game_gui.benchmark.windows.env import ResetController

    controller = object.__new__(ResetController)
    controller._menu_skip_replay = "closed_loop"
    controller._menu_skip_open_loop_tail = 0
    controller.cfg = SimpleNamespace(
        name="Palworld capture benchmark",
        process="Palworld-Win64-Shipping.exe")
    monkeypatch.setattr(controller, "_force_foreground", lambda _env: True)
    pressed = []
    monkeypatch.setattr(controller, "_press_until_screen_changes",
                        lambda _env, step: pressed.append(step.spec.buttons))
    monkeypatch.setattr(auto_nav, "dismiss_palworld_eos_login_popup",
                        lambda *_a, **_k: False)
    slept = []
    monkeypatch.setattr(game_env.time, "sleep", slept.append)
    monkeypatch.setattr(game_env, "emit_windows_event", lambda *_a, **_k: None)
    env = SimpleNamespace(
        hard_pause=True, unpause=lambda: None, pause=lambda: None,
        lifecycle=SimpleNamespace(find=lambda: SimpleNamespace(extras={"hwnd": 7})),
        render=lambda: (_ for _ in ()).throw(RuntimeError("no display")))

    def step(buttons, seconds):
        return SimpleNamespace(pad=0, spec=SimpleNamespace(
            click=None, buttons=buttons, duration_seconds=seconds,
            description="recorded step", left_stick=(0.0, 0.0),
            right_stick=(0.0, 0.0), left_trigger=0.0,
            right_trigger=0.0))

    controller.replay_menu_skip(env, [step(["SOUTH"], .12), step([], 1.33),
                                      step(["SOUTH"], .12), step([], 20.0),
                                      step(["WEST"], .12), step([], 3.0)])

    assert pressed == [["SOUTH"], ["SOUTH"], ["WEST"]]
    assert slept == [1.33, 20.0, 3.0]
