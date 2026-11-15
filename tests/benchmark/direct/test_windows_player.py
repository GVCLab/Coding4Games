"""Frozen-notes exam loop: ACT parsing, stateful llm_fn, ledger/trace rows."""
import json

import numpy as np
import pytest

from coding4game_gui.benchmark.direct.player import build_direct_system, play_one


def test_direct_windows_prompt_has_goal_episode_contract_and_action_table():
    spec = {
        "title": "Example Windows game",
        "engine": "windows",
        "gameplay": "The policy must read the screen and navigate the menu.",
        "objective": "Score as many points as possible (small budget).",
        "episode_contract": (
            "The episode begins from a fresh save and ends on GAME OVER, "
            "global tic-budget exhaustion, or a safety watchdog."),
        "action_catalog": [
            {"name": "A", "description": "confirm"},
            {"name": "NEUTRAL", "description": "release controls"},
        ],
        "runtime": {"launch_path": "SECRET_EXE"},
        "scorer": {"ocr": {"rect": "SECRET_RECT"}},
        "startup": {"save_path": "SECRET_SAVE"},
    }

    prompt = build_direct_system(spec, max_burst=None)

    assert "Score as many points as possible." in prompt
    assert "The episode begins from a fresh save" in prompt
    assert "`A`: confirm" in prompt
    assert "`NEUTRAL`: release controls" in prompt
    assert "one or more complete action lines" in prompt
    assert "LS[x,y]" in prompt and "ACTION_ERROR" in prompt
    assert "SECRET_EXE" not in prompt
    assert "SECRET_RECT" not in prompt
    assert "SECRET_SAVE" not in prompt
    assert "global tic-budget" not in prompt
    assert "policy must" not in prompt.lower()
    assert "you must read the screen" in prompt.lower()
    assert "The per-action cap is 1000 tics" in prompt
    assert "SECRET" not in prompt


def test_direct_windows_prompt_exposes_scenario_action_cap():
    spec = {
        "title": "Capped Windows game",
        "engine": "windows",
        "gameplay": "Play.",
        "objective": "Score.",
        "action_catalog": [{"name": "ATTACK", "description": "attack"}],
        "runtime": {"max_action_tics": 7},
    }

    prompt = build_direct_system(spec, max_burst=None)

    assert "integer from 1 to 7" in prompt
    assert "The per-action cap is 7 tics" in prompt
    assert "per-action cap is 1000" not in prompt


class FakeEnv:
    """Minimal stepped environment: 3 act() calls end the episode."""
    def __init__(self):
        self.n = 0
    def reset(self, episode_id):
        self.n = 0
    def frame(self):
        return np.zeros((24, 32, 3), dtype=np.uint8)
    def act(self, buttons, tics):
        self.n += 1
        return 1.0
    @property
    def done(self):
        return self.n >= 3
    @property
    def tic(self):
        return self.n * 4
    @property
    def total_reward(self):
        return float(self.n)


def test_direct_rejects_capped_whole_batch_before_executing_prefix():
    from coding4game_gui.benchmark.windows.env import WindowsGameEnv
    from coding4game_gui.benchmark.windows.games_config import ActionSpec

    class CappedEnv(FakeEnv):
        validate_action_plan = WindowsGameEnv.validate_action_plan
        max_action_tics = 7
        windows_input = "gamepad"
        spec = {"runtime": {"max_action_tics": 7}}
        def reset(self, *, episode_id):
            self.n = 0

        _actions = {
            "ATTACK": ActionSpec(name="attack", description="", buttons=["A"]),
        }

    env = CappedEnv()
    out = play_one(env, lambda *_args, **_kwargs: ("ACT 1 ATTACK\nACT 8 ATTACK", 1, 1), "m", "notes",
                      max_burst=None, max_calls_per_ep=1, episode_id=0)

    assert env.n == 0
    assert out.error is not None
    assert "7" in out.error


def test_play_one_max_calls_per_ep_none_means_unlimited():
    # brotato needs episodes to run until env.done (death / stall watchdog),
    # not get truncated by a decision-count cap partway through a still-live
    # episode (windows-engine episodes can span hundreds of decisions).
    # max_calls_per_ep=None must disable the `out.calls < max_calls_per_ep`
    # cap entirely, so only env.done ends the loop.
    class LongEnv(FakeEnv):
        DONE_AT = 50  # far past any real max_calls_per_ep default (25/60/80)
        @property
        def done(self):
            return self.n >= self.DONE_AT

    env = LongEnv()
    out = play_one(env, lambda m, s, msgs: ("ACT 1 ATTACK", 1, 1), "m", "notes",
                      max_burst=5, max_calls_per_ep=None, episode_id=0)
    assert out.calls == LongEnv.DONE_AT
    assert out.error is None


def test_player_infrastructure_error_is_not_parsed_as_a_model_reply():
    from coding4game_gui.benchmark.direct.llm import PlayerInfrastructureError

    def unavailable(*_args, **_kwargs):
        raise PlayerInfrastructureError(
            "ConnectionError: upstream unavailable", attempts=2)

    out = play_one(FakeEnv(), unavailable, "m", "notes", max_burst=5, max_calls_per_ep=25, episode_id=0)

    assert out.outcome_kind == "infrastructure_error"
    assert out.end_reason == "infrastructure_error"
    assert "upstream unavailable" in out.error
    assert out.replies == []
    assert out.calls == 1
    assert out.infrastructure_attempts == 2


def test_direct_player_receives_resized_jpeg_observation():
    import base64
    import cv2

    seen = {}

    def inspect_image(_model, _system, messages):
        source = messages[-1]["content"][1]["source"]
        seen.update(source)
        image = cv2.imdecode(
            np.frombuffer(base64.b64decode(source["data"]), dtype=np.uint8),
            cv2.IMREAD_COLOR)
        seen["shape"] = image.shape
        return "ACT 4 ATTACK", 1, 1

    out = play_one(FakeEnv(), inspect_image, "m", "notes", max_burst=5, max_calls_per_ep=25, episode_id=0)

    assert out.error is None
    assert seen["media_type"] == "image/jpeg"
    assert seen["shape"] == (768, 1280, 3)


def test_direct_player_keeps_text_history_but_windows_request_images():
    class EighteenTurnEnv(FakeEnv):
        @property
        def done(self):
            return self.n >= 18

    image_counts = []
    text_snapshots = []

    def fake_llm(model, system, messages, **kw):
        image_counts.append(sum(
            1 for message in messages
            for block in (message.get("content")
                          if isinstance(message.get("content"), list) else [])
            if isinstance(block, dict) and block.get("type") == "image"
        ))
        text_snapshots.append(json.dumps(messages))
        return "ACT 1 ATTACK", 1, 1

    out = play_one(EighteenTurnEnv(), fake_llm, "fake", "notes",
                      max_burst=1, max_calls_per_ep=None, episode_id=0)

    assert out.calls == 18
    assert image_counts == list(range(1, 17)) + [16, 16]
    assert all(f"Episode 0, tic {i}." in text_snapshots[-1]
               for i in range(0, 72, 4))
    assert text_snapshots[-1].count("ACT 1 ATTACK") == 17


def test_play_one_contains_act_time_exception():
    # a ScorerLost / watchdog RuntimeError from env.act must end THE EPISODE with
    # an error (spec §5.2), never propagate and crash play_notes / collect().
    class ScorerLostEnv:
        def __init__(self):
            self.n = 0
            self._tic = 0
            self._reward = 0.0

        def reset(self, episode_id):
            self.n = 0
            self._tic = 0
            self._reward = 0.0

        def frame(self):
            return np.zeros((8, 8, 3), dtype=np.uint8)

        def act(self, buttons, tics):
            self.n += 1
            if self.n >= 2:
                raise RuntimeError("scorer lost")
            self._tic += tics
            self._reward += 1.0
            return 1.0

        @property
        def done(self):
            return False

        @property
        def tic(self):
            return self._tic

        @property
        def total_reward(self):
            return self._reward

    env = ScorerLostEnv()
    out = play_one(env, lambda m, s, msgs: ("ACT 4 ATTACK", 1, 1), "m", "notes",
                      max_burst=5, max_calls_per_ep=25, episode_id=0)
    assert out.error is not None and "scorer lost" in out.error
    assert out.score == 1.0     # first act succeeded before the failure
    assert out.tics == 4        # exactly one 4-tic act landed


@pytest.mark.parametrize("budget", [12, 100])
def test_direct_player_gets_exact_prompt_and_per_episode_cap(budget):
    class WindowsEnv:
        def __init__(self):
            self._tic = 0
            self.held = []
            self.finished = 0

        def reset(self, episode_id):
            self.episode_id = episode_id
            self._tic = 0

        def frame(self):
            return np.zeros((8, 8, 3), dtype=np.uint8)

        def act(self, buttons, tics):
            self.held.append(tics)
            self._tic += tics
            return 1000.0

        @property
        def done(self):
            return False

        @property
        def tic(self):
            return self._tic

        @property
        def total_reward(self):
            return 77.0

        def finish_episode(self):
            self.finished += 1

        def close(self):
            pass

    seen = {}
    env = WindowsEnv()

    def llm(model, system, messages):
        seen["system"] = system
        seen["status"] = messages[-1]["content"][0]["text"]
        return "ACT 200 MOVE_UP", 1, 1

    notes = "# My exact player prompt\nDo the thing."
    out = play_one(env, llm, "m", notes,
                      max_burst=None, max_calls_per_ep=None, episode_id=42, max_tics=budget)

    assert seen["system"] == notes
    assert "reward" not in seen["status"].lower()
    assert seen["status"].startswith("Episode 42, tic 0.")
    assert out.episode_id == 42
    assert out.tics == budget and out.end_reason == f"max_tics ({budget}) reached"
    assert env.held == [budget] and env.finished == 1
