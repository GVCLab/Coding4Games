from __future__ import annotations

import numpy as np
import pytest
from types import SimpleNamespace
from PIL import Image

from coding4game_gui.benchmark.direct.player import play_one
from coding4game_gui.benchmark.windows.raw_actions import parse_windows_acts
from coding4game_gui.benchmark.windows.env import WindowsGameEnv


class Driver:
    def __init__(self):
        self.steps = []
        self.gamepad = self

    def render(self):
        return Image.new("RGB", (16, 16), (0, 0, 0))

    def step(self, **state):
        live_action = state.pop("live_action", None)
        self.steps.append(state)
        if live_action is not None:
            live_action()
        return self.render()

    def pause(self):
        pass

    def unpause(self):
        pass


class Scorer:
    def reset(self):
        pass

    def delta(self, _frame):
        return 0.0

    def is_terminal(self, _frame):
        return False

    def classify_phase(self, _frame):
        return "combat"


def make_env(driver):
    return WindowsGameEnv(
        "raw-test", driver_env=driver, scorer=Scorer(), actions={},
        spec={"engine": "windows", "runtime": {"scenario": "raw-test"}},
    )


def make_kbm_env(driver):
    return WindowsGameEnv(
        "raw-test", driver_env=driver, scorer=Scorer(), actions={},
        spec={"engine": "windows", "runtime": {"scenario": "raw-test"},
              "scorer": {"calibrated_client_size": [1280, 768]}},
        windows_input="gamepad-kbm",
    )


@pytest.mark.parametrize('phase, end_reason', [
    ('death', 'game_over'), ('victory', 'terminal:victory'), (None, 'terminal:unknown'),
])
def test_registered_terminal_stops_step_batch_without_opt_in(phase, end_reason):
    driver = Driver()
    env = make_kbm_env(driver)
    env._scorer.is_terminal = lambda frame: bool(driver.steps)
    env._scorer.classify_phase = lambda frame: phase
    calls = []
    def player(*args):
        calls.append(True)
        return 'ACT 2 KEY[D]\nACT 2 KEY[A]', 0, 0
    result = play_one(env, player, 'fake', '', max_tics=20, episode_id=0, max_burst=None, max_calls_per_ep=3)
    assert len(driver.steps) == 1
    assert len(calls) == 1
    assert result.end_reason == end_reason and result.tics == 2
    assert result.error is None


@pytest.mark.parametrize('phase', ['victory', None])
def test_failure_detection_does_not_end_non_death_phases(phase):
    driver = Driver()
    env = make_kbm_env(driver)
    env.terminal_detection = True
    env._scorer.classify_phase = lambda frame: phase
    result = play_one(env, lambda *args: ('ACT 2 KEY[D]', 0, 0), 'fake', '',
                      max_tics=4, episode_id=0, max_burst=None, max_calls_per_ep=3)
    assert len(driver.steps) == 2
    assert result.end_reason != 'game_over'


def test_death_detection_can_be_explicitly_disabled():
    driver = Driver()
    env = make_kbm_env(driver)
    env.terminal_detection = False
    env._scorer.is_terminal = lambda frame: True
    env._scorer.classify_phase = lambda frame: 'death'
    result = play_one(env, lambda *args: ('ACT 2 KEY[D]', 0, 0), 'fake', '',
                      max_tics=4, episode_id=0, max_burst=None, max_calls_per_ep=3)
    assert len(driver.steps) == 2
    assert result.end_reason != 'game_over'


def test_direct_preserves_seconds_and_stops_at_rounded_tic_budget():
    driver = Driver()
    original = driver.step
    def measured_step(**kw):
        frame = original(**kw)
        elapsed = .26 if len(driver.steps) == 1 else kw['duration_seconds']
        driver.last_step_timing = SimpleNamespace(
            requested_s=kw['duration_seconds'], live_window_s=elapsed, interrupted=False)
        return frame
    driver.step = measured_step
    result = play_one(make_env(driver), lambda *a: ('ACT 2 NEUTRAL', 0, 0), 'fake', 'notes',
                      episode_id=0, max_burst=None, max_calls_per_ep=3, max_tics=3)
    assert result.error is None
    assert result.tics == pytest.approx(3)
    assert result.game_running_seconds == pytest.approx(.26)
    assert len(driver.steps) == 1
    assert driver.steps[-1]['duration_seconds'] == pytest.approx(.2)


@pytest.mark.parametrize('overshoot, valid', [(1, True), (2, True), (3, False)])
def test_direct_final_action_tolerance_preserves_usage_and_stops(overshoot, valid):
    driver = Driver()
    original = driver.step
    def measured_step(**kw):
        frame = original(**kw)
        driver.last_step_timing = SimpleNamespace(
            requested_s=kw['duration_seconds'], live_window_s=(3 + overshoot) / 10,
            interrupted=False)
        return frame
    driver.step = measured_step
    result = play_one(make_env(driver), lambda *a: ('ACT 3 NEUTRAL\nACT 1 NEUTRAL', 0, 0), 'fake', 'notes',
                      episode_id=0, max_burst=None, max_calls_per_ep=3, max_tics=3)
    assert (result.outcome_kind == 'valid') is valid
    assert result.tics == 3 + overshoot
    assert result.budget_overshoot_tics == overshoot
    assert len(driver.steps) == 1


def test_direct_stepped_dynamic_action_reaches_driver_exactly_for_three_tics():
    driver = Driver()
    env = make_env(driver)
    env.reset()
    acts = parse_windows_acts("ACT 3 LS[0.1,0.1]", max_burst=None)
    assert acts == [(["LS[0.1,0.1]"], 3)]
    env.act(*acts[0])
    assert driver.steps[-1]["left_stick"] == (0.1, 0.1)
    assert driver.steps[-1]["duration_seconds"] == 0.3


def test_direct_stepped_pointer_plan_validates_then_reaches_same_execution_seam():
    driver = Driver()
    env = make_kbm_env(driver)
    clicked = []
    env._do_clicks = lambda points: clicked.extend(points)
    env.reset()
    acts = parse_windows_acts("ACT 7 CLICK[250,750]", max_burst=None)
    env.validate_action_plan(acts)
    env.act(*acts[0])
    assert clicked == [(250.0, 750.0)]
    assert driver.steps[-1]["buttons"] == []


def test_direct_stepped_move_plan_hovers_for_the_complete_pulse():
    driver = Driver()
    env = make_kbm_env(driver)
    moved = []
    env._do_moves = lambda points: moved.extend(points)
    env.reset()
    acts = parse_windows_acts("ACT 5 MOVE[250,750]", max_burst=None)
    env.validate_action_plan(acts)
    env.act(*acts[0])
    assert moved == [(250.0, 750.0)]
    assert driver.steps[-1]["buttons"] == []
    assert driver.steps[-1]["duration_seconds"] == 0.5


def test_direct_stepped_relative_move_reaches_camera_input_seam():
    driver = Driver()
    env = make_kbm_env(driver)
    moved = []
    env._do_relative_moves = lambda deltas: moved.extend(deltas)
    env.reset()
    acts = parse_windows_acts("ACT 3 RMOVE[-25,40]", max_burst=None)
    env.validate_action_plan(acts)
    env.act(*acts[0])
    assert moved == [(-25, 40)]
    assert driver.steps[-1]["buttons"] == []
    assert driver.steps[-1]["duration_seconds"] == 0.3


def test_extended_pointer_actions_share_the_development_and_direct_execution_seam():
    driver = Driver()
    env = make_kbm_env(driver)
    seen = []
    env._do_right_clicks = lambda points: seen.append(("right", points))
    env._do_middle_clicks = lambda points: seen.append(("middle", points))
    env._do_double_clicks = lambda points: seen.append(("double", points))
    env._do_drags = lambda drags, duration: seen.append(
        ("drag", drags, duration))
    env._do_scrolls = lambda scrolls: seen.append(("scroll", scrolls))
    env.reset()

    acts = parse_windows_acts(
        "ACT 2 RCLICK[100,200]\n"
        "ACT 2 MCLICK[150,250]\n"
        "ACT 3 DOUBLE_CLICK[200,300]\n"
        "ACT 4 DRAG[10,20,30,40]\n"
        "ACT 5 SCROLL[400,500,-2,3]",
        max_burst=None,
    )
    env.validate_action_plan(acts)
    for act in acts:
        env.act(*act)

    assert seen == [
        ("right", [(100.0, 200.0)]),
        ("middle", [(150.0, 250.0)]),
        ("double", [(200.0, 300.0)]),
        ("drag", [((10.0, 20.0), (30.0, 40.0))], 0.4),
        ("scroll", [((400.0, 500.0), -2, 3)]),
    ]
    assert [step["duration_seconds"] for step in driver.steps[-5:]] == [
        0.2, 0.2, 0.3, 0.4, 0.5]
    assert all(step["buttons"] == [] for step in driver.steps[-5:])


def test_keyboard_tokens_reach_the_driver_as_one_complete_pulse():
    driver = Driver()
    env = make_kbm_env(driver)
    env.reset()
    env.act(["KEY[SHIFT]", "KEY[W]"], 3)
    assert driver.steps[-1]["buttons"] == ["KEY[SHIFT]", "KEY[W]"]
    assert driver.steps[-1]["duration_seconds"] == 0.3


def test_code_dynamic_action_uses_the_same_environment_boundary():
    driver = Driver()
    env = make_env(driver)
    env.reset()
    # The program bridge and stepped Direct parser share wire action tokens.
    env.act(["LS[0.1,0.1]"], 3)
    assert driver.steps[-1]["left_stick"] == (0.1, 0.1)


def test_direct_stepped_rollout_clamps_final_action_exactly_to_max_tics():
    driver = Driver()
    env = make_env(driver)
    result = play_one(env, lambda *_args: ("ACT 35 LS[0.1,0.1]", 4, 2), "model", "notes",
                      episode_id=0, max_tics=5, max_burst=None, max_calls_per_ep=None)

    assert result.error is None
    assert result.calls == 1
    assert result.tics == 5
    assert result.actions == [(0, ["LS[0.1,0.1]"], 5, None)]
    assert result.end_reason == "max_tics (5) reached"
    assert driver.steps[-1]["duration_seconds"] == 0.5


def test_direct_stepped_invalid_raw_reply_is_rejected_then_corrected_at_same_tic():
    driver = Driver()
    env = make_env(driver)
    replies = iter([
        ("ACT 4 NEUTRAL,B,LS[0,0.8]", 4, 2),
        ("ACT 3 NEUTRAL", 5, 1),
    ])
    requests = []

    def player(_model, _system, messages):
        requests.append(str(messages[-1]))
        return next(replies)

    result = play_one(env, player, "model", "notes",
                      episode_id=0, max_tics=3, max_burst=None, max_calls_per_ep=None)

    assert result.error is None
    assert result.outcome_kind == "valid"
    assert result.calls == 2
    assert result.invalid_action_replies == 1
    assert result.tokens_in == 9
    assert result.tokens_out == 3
    assert result.tics == 3
    assert result.actions == [(0, ["NEUTRAL"], 3, None)]
    assert len(driver.steps) == 1
    assert "ACTION_ERROR" in requests[1]
    assert "No action was executed" in requests[1]


def test_direct_stepped_reply_plan_is_validated_before_any_action_executes():
    driver = Driver()
    env = make_env(driver)
    replies = iter([
        ("ACT 1 NEUTRAL\nACT 1 NEUTRAL,B", 0, 0),
        ("ACT 1 NEUTRAL", 0, 0),
    ])

    result = play_one(env, lambda *_args: next(replies), "model", "notes",
                      episode_id=0, max_tics=1, max_burst=None, max_calls_per_ep=None)

    assert result.error is None
    assert result.invalid_action_replies == 1
    assert result.actions == [(0, ["NEUTRAL"], 1, None)]
    assert len(driver.steps) == 1


def test_direct_stepped_burst_overflow_rejects_whole_reply_then_corrects_at_same_tic():
    driver = Driver()
    env = make_env(driver)
    replies = iter([
        ("ACT 1 NEUTRAL\nACT 1 NEUTRAL\nACT 1 NEUTRAL", 3, 1),
        ("ACT 2 NEUTRAL", 2, 1),
    ])
    requests = []

    def player(_model, _system, messages):
        requests.append(str(messages[-1]))
        return next(replies)

    result = play_one(env, player, "model", "notes",
                      episode_id=0, max_tics=2, max_burst=2, max_calls_per_ep=None)

    assert result.error is None
    assert result.invalid_action_replies == 1
    assert result.calls == 2
    assert result.tics == 2
    assert result.actions == [(0, ["NEUTRAL"], 2, None)]
    assert len(driver.steps) == 1
    assert "ACTION_ERROR" in requests[1]
    assert "received 3 parseable ACT lines, cap is 2" in requests[1]


def test_malformed_dynamic_action_is_an_explicit_action_error():
    driver = Driver()
    env = make_env(driver)
    env.reset()
    try:
        env.act(["LS[2,0]"], 3)
    except ValueError as exc:
        assert "invalid raw action" in str(exc)
    else:
        raise AssertionError("malformed raw action was accepted")
    assert driver.steps == []


def test_direct_stepped_parse_error_ends_after_three_consecutive_invalid_replies(
        monkeypatch):
    class EpisodeEnv:
        done = False
        tic = 0
        total_reward = 0.0

        def reset(self, episode_id=0):
            pass

        def frame(self):
            return np.zeros((8, 8, 3), dtype=np.uint8)

    def bad_parser(_reply, _max_burst):
        raise ValueError("invalid raw ACT syntax")

    import coding4game_gui.benchmark.windows.raw_actions as raw_actions
    monkeypatch.setattr(raw_actions, "parse_windows_acts", bad_parser)
    out = play_one(EpisodeEnv(), lambda *_args: ("ACT 3 LS[bad]", 0, 0), "model", "notes",
                      episode_id=0, max_burst=None, max_calls_per_ep=None)
    assert out.error.startswith("ValueError: invalid raw ACT syntax")
    assert out.outcome_kind == "action_error"
    assert out.end_reason == "invalid_action_replies_exhausted"
    assert out.calls == 3
    assert out.invalid_action_replies == 3


def test_windows_no_act_is_classified_as_action_error():
    class EpisodeEnv:
        done = False
        tic = 0
        total_reward = 0.0

        def reset(self, episode_id=0):
            pass

        def frame(self):
            return np.zeros((8, 8, 3), dtype=np.uint8)

    out = play_one(EpisodeEnv(), lambda *_args: ("I will wait.", 0, 0), "model", "notes",
                      episode_id=0, max_burst=None, max_calls_per_ep=None)
    assert out.error == "no ACT lines parsed"
    assert out.outcome_kind == "action_error"
    assert out.end_reason == "invalid_action_replies_exhausted"
    assert out.calls == 3
    assert out.invalid_action_replies == 3
