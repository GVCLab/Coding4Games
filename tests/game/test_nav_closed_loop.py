"""Navigation steps verify ARRIVAL, not merely that the screen moved.

A screen transition is itself a change, so change-detection accepted clicks
that landed mid-animation: the reset "ran", left the game on the wrong
screen, failed start_check, and fell through to full_restart — measured 6
times in a single lang-arm run at ~120s each. These pin the closed loop.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from coding4game_gui.benchmark.windows.games_config import parse_game_config
from coding4game_gui.benchmark.windows.env import _click_until_text

REPO = Path(__file__).resolve().parents[2]
DRIVER = REPO / "games" / "balatro.yaml"
SCENARIO = (REPO / "coding4game_gui" / "benchmark" / "scenarios"
            / "balatro.yaml")


class _Scorer:
    """Reports the expected text only once `arrive_on` clicks have happened."""

    def __init__(self, arrive_on: int):
        self.arrive_on = arrive_on
        self.clicks = 0

    def check_text(self, frame, pattern, rect=None):
        return self.clicks >= self.arrive_on


class _Env:
    def __init__(self, scorer):
        self.scorer = scorer

    def render(self):
        from PIL import Image
        return Image.new("RGB", (1536, 864))


def _patch_click(monkeypatch, scorer):
    import coding4game_gui.benchmark.windows.env as envmod

    def fake_click(process, env, points, content_size=None):
        scorer.clicks += 1

    monkeypatch.setattr(envmod, "_click_game_points", fake_click)
    monkeypatch.setattr(envmod.time, "sleep", lambda *_: None)


def test_click_retries_until_the_destination_appears(monkeypatch):
    sc = _Scorer(arrive_on=3)
    _patch_click(monkeypatch, sc)
    ok = _click_until_text("Game.exe", _Env(sc), (10, 10), "Seeded", sc,
                           settle=0.01, retries=6, content_size=(1536, 864))
    assert ok
    assert sc.clicks == 3, "should stop as soon as it lands, not keep clicking"


def test_no_click_at_all_when_already_at_the_destination(monkeypatch):
    """Arrival is checked BEFORE clicking.

    Clicking first and checking after re-clicks a coordinate that is only
    meaningful on the screen we started from: menu_skip's (459,780) is PLAY
    on the main menu but something else on the New Run page. A first click
    that WORKED and merely confirmed slowly then got "retried" straight back
    off the destination — the loop undoing its own success. Measured: the
    smoke reported "never reached 'Seeded'" while the game had in fact
    started a run.
    """
    sc = _Scorer(arrive_on=0)          # already there
    _patch_click(monkeypatch, sc)
    ok = _click_until_text("Game.exe", _Env(sc), (10, 10), "Seeded", sc,
                           settle=0.01, retries=6, content_size=(1536, 864))
    assert ok
    assert sc.clicks == 0, \
        "must not click when the destination is already showing"


def test_click_reports_failure_when_it_never_arrives(monkeypatch):
    sc = _Scorer(arrive_on=99)
    _patch_click(monkeypatch, sc)
    ok = _click_until_text("Game.exe", _Env(sc), (10, 10), "Seeded", sc,
                           settle=0.01, retries=4, content_size=(1536, 864))
    assert not ok, "a step that never reaches its destination must say so"
    assert sc.clicks == 4


def test_menu_skip_clicks_declare_where_they_land():
    cfg = parse_game_config(yaml.safe_load(DRIVER.read_text(encoding="utf-8")),
                            source_path=DRIVER)
    for step in cfg.menu_skip:
        if step.spec.click is None:
            continue
        assert step.spec.expect_text or step.spec.expect_absent, \
            f"cold-start click {step.spec.click} declares no destination"


def test_a_still_visible_overlay_means_not_arrived(monkeypatch):
    """`expect_absent` reports "not there yet" while the overlay is up.

    Balatro's New Run panel leaves the blind screen legible behind it, so a
    positive anchor was already satisfied when the step began — and the loop,
    which checks BEFORE clicking so a retry cannot undo its own success, then
    skipped the click and called the reset done with the panel still open.
    """
    calls = {"n": 0}

    class _Scorer:
        clicks = 0

        def check_text(self, frame, pattern, rect=None):
            # the panel's own text disappears after the second click
            if pattern == "Base Difficulty":
                return self.clicks < 2
            return True

    sc = _Scorer()
    _patch_click(monkeypatch, sc)
    ok = _click_until_text("Game.exe", _Env(sc), (10, 10), None, sc,
                           settle=0.01, retries=6, content_size=(1536, 864),
                           absent="Base Difficulty")
    assert ok
    assert sc.clicks == 2, \
        f"must keep clicking while the overlay is up, clicked {sc.clicks}"


@pytest.mark.parametrize("resize_during", ["settle", "press"])
def test_pad_menu_press_handles_a_live_client_resize(monkeypatch, resize_during):
    from PIL import Image
    import coding4game_gui.benchmark.windows.env as envmod

    class ResizingEnv:
        size = (120, 80)
        renders = 0
        presses = 0
        released = False

        def render(self):
            self.renders += 1
            if resize_during == "settle" and self.renders >= 2:
                self.size = (128, 72)
            color = 200 if self.presses else 0
            return Image.new("RGB", self.size, (color, color, color))

        def apply_state(self, pad, *, buttons, **kwargs):
            assert pad == 1
            if buttons:
                self.presses += 1
                if resize_during == "press":
                    self.size = (128, 72)
            else:
                self.released = True

    env = ResizingEnv()
    controller = object.__new__(envmod.ResetController)
    monkeypatch.setattr(controller, "_force_foreground", lambda target: True)
    monkeypatch.setattr(envmod.time, "sleep", lambda seconds: None)
    step = SimpleNamespace(pad=1, spec=SimpleNamespace(
        expect_text=None, expect_absent=None, buttons=["A"],
        left_stick=(0, 0), right_stick=(0, 0),
        left_trigger=0, right_trigger=0, duration_seconds=.12))

    controller._press_until_screen_changes(env, step)

    assert env.presses == 1
    assert env.released
    assert env.size == (128, 72)


def test_resize_before_a_swallowed_press_does_not_skip_its_retry(monkeypatch):
    from PIL import Image
    import coding4game_gui.benchmark.windows.env as envmod

    class ResizingEnv:
        renders = 0
        presses = 0
        releases = 0

        def render(self):
            self.renders += 1
            size = (120, 80) if self.renders == 1 else (128, 72)
            color = 200 if self.presses >= 2 else 0
            return Image.new("RGB", size, (color, color, color))

        def apply_state(self, pad, *, buttons, **kwargs):
            if buttons:
                self.presses += 1
            else:
                self.releases += 1

    env = ResizingEnv()
    controller = object.__new__(envmod.ResetController)
    monkeypatch.setattr(controller, "_force_foreground", lambda target: True)
    monkeypatch.setattr(envmod.time, "sleep", lambda seconds: None)
    step = SimpleNamespace(pad=1, spec=SimpleNamespace(
        expect_text=None, expect_absent=None, buttons=["A"],
        left_stick=(0, 0), right_stick=(0, 0),
        left_trigger=0, right_trigger=0, duration_seconds=.12))

    controller._press_until_screen_changes(env, step)

    assert env.presses == env.releases == 2
