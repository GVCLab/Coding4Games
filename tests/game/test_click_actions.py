"""Balatro's raw-pad action table and cold path stay wired correctly.

These run anywhere (no game, no Windows): they cover the wiring that is easy
to get silently wrong — a raw action must map to a physical pad state, and
every token the scenario advertises must actually exist in the driver YAML.
"""
from __future__ import annotations

from pathlib import Path, PureWindowsPath

import yaml

from coding4game_gui.benchmark.windows.games_config import parse_game_config
from coding4game_gui.benchmark.windows.verbs import build_action_table

REPO = Path(__file__).resolve().parents[2]
DRIVER = REPO / "games" / "balatro.yaml"


def _driver():
    return parse_game_config(yaml.safe_load(DRIVER.read_text(encoding="utf-8")),
                             source_path=DRIVER)


def test_balatro_declares_direct_launch_without_save_restore():
    cfg = _driver()
    assert cfg.auto_launch is not None
    launch = PureWindowsPath(cfg.auto_launch.launch_path)
    assert launch.is_absolute() and launch.name.lower() == "balatro.exe"
    assert cfg.save_state is None


def test_every_scenario_token_exists_in_the_driver():
    from coding4game_gui.benchmark.spec import load_game_spec

    catalog = {a["name"] for a in load_game_spec("balatro")["action_catalog"]}
    table = set(build_action_table(_driver().actions))
    assert catalog == table


def test_menu_skip_is_recorded_pad_only():
    """The cold path is a recorded raw-pad trajectory, separate from policy."""
    cfg = _driver()
    assert cfg.menu_skip, "balatro needs a cold-start path"
    assert any(s.spec.buttons for s in cfg.menu_skip), "cold path has no pad input"
    for step in cfg.menu_skip:
        assert step.spec.click is None and step.spec.click_text is None, \
            f"mouse step in raw-pad cold path: {step.spec.name}"
        assert step.spec.duration_seconds > 0, \
            f"step has no duration: {step.spec.name}"
