"""The OCR scorer's phase names default to brotato's, and every mismatch
fails SILENTLY.

Three settings pick phases by NAME, each defaulting to what brotato happens
to call things:

    terminal_phases -> ["death"]              episode never ends
    score_phases    -> ["combat"]             nothing ever scores
    rebase_phases   -> ["levelup", "shop"]    a spendable counter's reset
                                              is read as a loss

None of these raise when they miss. A scorer that reads no regions also
records no misses, so the ScorerLost guard stays quiet and the run simply
reports zero — which is indistinguishable from a policy that played badly.
Balatro burned two full episodes and a debugging session on exactly that.

These tests run everywhere: they only read scenario YAML.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coding4game_gui.benchmark.spec import load_game_spec

GAMES = (Path(__file__).resolve().parents[2] / "coding4game_gui"
         / "benchmark" / "scenarios")

DEFAULTS = {
    "terminal_phases": ["death"],
    "score_phases": ["combat"],
    "rebase_phases": ["levelup", "shop"],
}


def _ocr_scenarios():
    out = []
    for path in sorted(GAMES.glob("*.yaml")):
        spec = load_game_spec(path.stem)
        scorer = spec.get("scorer") or {}
        if scorer["kind"] == "ocr" and scorer.get("phases"):
            detection = spec.get("lifecycle", {}).get("terminal_detection", True)
            out.append((path.name, scorer, detection))
    return out


@pytest.mark.parametrize("name,scorer,terminal_detection", _ocr_scenarios())
def test_phase_vocabulary_resolves(name, scorer, terminal_detection):
    """Whatever a scenario relies on — declared or defaulted — must name
    phases it actually defines."""
    declared = set(scorer["phases"].keys())
    for key, fallback in DEFAULTS.items():
        if key == "terminal_phases" and terminal_detection is False:
            # Fixed-duration tasks do not use an intermediate terminal phase.
            continue
        if key == "rebase_phases" and not any(
                region.get("spendable")
                for region in (scorer.get("regions") or {}).values()):
            continue
        effective = scorer.get(key) or fallback
        unknown = [p for p in effective if p not in declared]
        assert not unknown, (
            f"{name}: {key} names undeclared phase(s) {unknown}; "
            f"declared phases are {sorted(declared)}. Left unfixed this is "
            f"silent — the scorer neither raises nor scores.")
