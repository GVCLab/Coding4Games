"""The scorer's CONTINUE? signal must actually end a live episode.

The fixture tests next door prove the discriminator; this proves the WIRING,
which is a separate thing and the one main's refactor could silently break.

Why it needs its own test rather than a live run: reaching CONTINUE? in the
real game means losing a whole three-character match, ~2200 tics against a
500-tic episode budget, so a live episode ends on budget and never exercises
this path. A previous BBT scorer shipped an `is_terminal` that could never
fire under main's deferred scoring and the symptom was an episode that quietly
burned its entire budget — indistinguishable from a policy playing badly.

What makes this load-bearing is the interaction it pins: main scores DEFERRED
(`env.act` only queues frames; `delta` runs at settlement), but terminal
detection is LIVE (`_check_terminal` at env.py:372 and :678, throttled by
`lifecycle.terminal_scan_tics`). A scorer whose `is_terminal` depends on state
that `delta` sets therefore never terminates. Ours reads the frame directly —
this test is what says so, against the real scenario spec and real frames.
"""
from __future__ import annotations

import pathlib

import cv2
import pytest
import yaml
from PIL import Image

from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.env import WindowsGameEnv
from coding4game_gui.benchmark.windows.games_config import ActionSpec
from coding4game_gui.benchmark.windows.scorers.kof98_terminal import Kof98TerminalScorer

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "kof98_terminal"
CLIENT = (854, 480)


def _pil(name: str) -> Image.Image:
    """Serve a fixture the way a driver env does: a PIL image in RGB."""
    bgr = cv2.imread(str(FIXTURES / name))
    assert bgr is not None, name
    return Image.fromarray(bgr[:, :, ::-1])


class ScriptedDriverEnv:
    """Replays a list of fixture frames, holding the last one forever."""

    def __init__(self, names):
        self.frames = [_pil(n) for n in names]
        self.i = 0
        self.n_players = 1
        self.gamepad = self
        self.input = self
        self.pause_calls = 0
        self.unpause_calls = 0

    def _current(self):
        frame = self.frames[min(self.i, len(self.frames) - 1)]
        self.i += 1
        return frame

    def step(self, **kw):
        return self._current()

    def render(self):
        return self._current()

    def apply_state(self, idx, **state):
        pass

    def reset_pad(self, idx):
        pass

    def pause(self):
        self.pause_calls += 1

    def unpause(self):
        self.unpause_calls += 1

    def freeze_now(self):
        pass

    def close(self):
        pass


ACTIONS = {"NEUTRAL": ActionSpec(name="neutral", description="")}


def _env(names):
    """A live env over scripted frames, using the REAL scenario spec.

    The spec matters: terminal_scan_tics, terminal_detection and the scorer's
    calibrated thresholds all come from the shipped scenario, so a change there
    reaches this test instead of being shadowed by a hand-written stub.
    """
    spec = load_game_spec("kof98_terminal")
    # These historical frames require their original capture calibration.
    spec["scorer"] = yaml.safe_load((FIXTURES / "mr_why_calibration.yaml").read_text(encoding="utf-8"))["scorer"]
    scorer = Kof98TerminalScorer(spec["scorer"], client_size=CLIENT)
    env = WindowsGameEnv("kof98_terminal", driver_env=ScriptedDriverEnv(names),
                         scorer=scorer, actions=ACTIONS, spec=spec)
    env.reset(episode_id=0)
    return env


def _drive(env, steps, tics=3):
    for _ in range(steps):
        if env._done:
            break
        env.act(["NEUTRAL"], tics)
    return env


def test_terminal_detection_is_enabled_for_this_scenario():
    spec = load_game_spec("kof98_terminal")
    assert spec["lifecycle"]["terminal_detection"] is True
    assert spec["runtime"]["max_action_tics"] == 5
    assert WindowsGameEnv("kof98_terminal", driver_env=ScriptedDriverEnv(
        ["combat_f00434.jpg"]), scorer=Kof98TerminalScorer(
            spec["scorer"], client_size=CLIENT), actions=ACTIONS,
        spec=spec).terminal_detection is True


def test_combat_frames_never_end_the_episode():
    """The negative control. Without it, a scorer that terminates on anything
    would pass the positive test below."""
    env = _drive(_env(["combat_f00434.jpg", "combat_f00667.jpg"] * 20), 12)
    assert env._done is False
    assert env.end_reason is None


def test_hard_negatives_never_end_the_episode():
    """The two frames that defeat each half of the gate, served repeatedly."""
    env = _drive(_env(["white_flash_f01032.jpg", "winner_pose_f01038.jpg"] * 20), 12)
    assert env._done is False, (
        "the KO whiteout and the winner pose must not read as a match end")


def test_a_ko_ends_the_episode():
    """The episode boundary, moved to the first KO on 2026-09-19.

    The full-health frames in front are not decoration: `_seen_live` only
    latches once both bars have been observed healthy, so an episode cannot be
    ended by a KO reading it saw before the fight began.
    """
    names = ["full_health_f00244.jpg"] * 2 + ["ko_p1_empty_854_t00280.jpg"] * 12
    env = _drive(_env(names), 16)
    assert env._done is True
    assert env.end_reason is not None, "a KO must set an end reason"


def test_first_action_can_end_on_a_ko_after_a_healthy_initial_observation():
    """The already captured initial HUD must prime the healthy-round latch."""
    env = _env(["full_health_f00244.jpg"] + ["ko_p1_empty_854_t00280.jpg"] * 3)
    env.act(["NEUTRAL"], env.max_action_tics)
    assert env.done, "the first action must not discard the validated initial health"
    assert env.finalize_score() < 0.05


def test_a_continue_screen_no_longer_ends_the_episode():
    """It did until 2026-09-19. Pinned so the change is deliberate, not drift.

    The CONTINUE? calibration is still exercised by the fixture tests and still
    drives `classify_phase`; what it no longer does is end an episode, because
    a consecutive-frame rule counts `act` calls rather than tics.
    """
    names = ["combat_f00434.jpg"] * 4 + ["continue_f01055.jpg"] * 12
    env = _drive(_env(names), 16)
    assert env._done is False


def test_terminal_is_reached_through_the_live_act_path():
    """Not by calling is_terminal ourselves — through `act`, which is the only
    thing that runs during an episode."""
    env = _env(["full_health_f00244.jpg"] * 2
               + ["ko_p1_empty_854_t00280.jpg"] * 12)
    seen = []
    real = env._scorer.is_terminal

    def spy(frame):
        seen.append(frame.shape)
        return real(frame)

    env._scorer.is_terminal = spy
    _drive(env, 8)
    assert seen, "act() never consulted the scorer for a terminal screen"
    assert env._done is True


def test_a_nearly_dead_bar_does_not_end_the_episode():
    """One column of health left is not a KO. The margin is 0.0181 against 0."""
    names = (["full_health_f00244.jpg"] * 2 + ["nearly_ko_854_t00275.jpg"]
             + ["combat_f00434.jpg"] * 12)
    env = _drive(_env(names), 10)
    assert env._done is False, (
        "a bar that still has colour left must not read as a KO")


def test_scoring_still_settles_after_a_terminal_episode():
    """Terminal is live, scoring is deferred — the two must not collide.

    An episode that ends on a terminal screen still has to settle the frames it
    queued, and settlement calls `reset()` then replays `delta` over all of
    them. If terminal detection left the scorer in a state settlement depends
    on, this is where it would show.
    """
    names = (["full_health_f00244.jpg"] * 2 + ["combat_f00434.jpg", "combat_f00667.jpg"] * 3
             + ["ko_p1_empty_854_t00280.jpg"] * 8)
    env = _drive(_env(names), 14)
    assert env._done is True
    total = env.finalize_score()
    assert isinstance(total, float)
    # Settlement is `latest_valid_frame`, so this is the KO frame's snapshot,
    # not a replayed sum. P1 is empty against a nearly full P2, which is the
    # worst outcome the rule can express.
    assert 0.0 <= total <= 1.0, total
    assert total < 0.05, (
        "an empty bar against a full one must score at the bottom of the range")
