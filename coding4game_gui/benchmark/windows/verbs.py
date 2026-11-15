"""Bench action tokens -> one combined gamepad state.

The bench exposes UPPERCASE tokens (parse_acts uppercases whatever the player
writes); driver YAML `actions:` keys are lowercase verbs. Combination:
button union (first-seen order), stick component-wise sum clamped to [-1, 1],
trigger max. Per-verb duration_seconds is IGNORED here — the bench controls
hold time via tics."""
from __future__ import annotations

from coding4game_gui.benchmark.windows.games_config import ActionSpec


def build_action_table(actions: dict[str, ActionSpec]) -> dict[str, ActionSpec]:
    return {name.upper(): spec for name, spec in actions.items()}


def combine_states(specs: list[ActionSpec]) -> dict:
    buttons: list[str] = []
    ls, rs = [0.0, 0.0], [0.0, 0.0]
    lt = rt = 0.0
    for s in specs:
        for b in s.buttons:
            if b not in buttons:
                buttons.append(b)
        ls[0] += s.left_stick[0]
        ls[1] += s.left_stick[1]
        rs[0] += s.right_stick[0]
        rs[1] += s.right_stick[1]
        lt = max(lt, s.left_trigger)
        rt = max(rt, s.right_trigger)

    def clamp(v: float) -> float:
        return max(-1.0, min(1.0, v))

    return {"buttons": buttons,
            "left_stick": (clamp(ls[0]), clamp(ls[1])),
            "right_stick": (clamp(rs[0]), clamp(rs[1])),
            "left_trigger": min(1.0, lt), "right_trigger": min(1.0, rt)}
