"""Shared Windows benchmark tic-rate configuration."""
from __future__ import annotations
import math


DEFAULT_TICS_PER_SECOND = 10
STEP_BUDGET_TOLERANCE_TICS = 2
MAX_ACTION_TICS = 1000


def resolve_passive_score_interval_tics(runtime: dict | None) -> int | None:
    """Validate an optional host-only held-action scoring capture interval."""
    value = (runtime or {}).get("passive_score_interval_tics")
    if value is not None and (type(value) is not int or not 1 <= value <= MAX_ACTION_TICS):
        raise ValueError("runtime.passive_score_interval_tics must be an integer from 1 to 1000")
    return value


def resolve_max_action_tics(runtime: dict | None) -> int:
    """Return the scenario's per-action cap, preserving the 1000-tic default."""
    value = (runtime or {}).get("max_action_tics", MAX_ACTION_TICS)
    if type(value) is not int or not 1 <= value <= MAX_ACTION_TICS:
        raise ValueError(
            f"runtime.max_action_tics must be an integer from 1 to {MAX_ACTION_TICS}"
        )
    return value


def validate_action_tics(tics: int, max_action_tics: int = MAX_ACTION_TICS) -> None:
    """Reject invalid pulse lengths before input or budget truncation."""
    if type(tics) is not int or tics <= 0:
        raise ValueError("ACT tics must be a positive integer")
    if tics > max_action_tics:
        raise ValueError(f"ACT tics must not exceed {max_action_tics}; shorten the action")


def resolve_start_settle_seconds(start_check: dict | None) -> float:
    """Validate the calibrated live readiness dwell before policy handoff."""
    if start_check is not None and not isinstance(start_check, dict):
        raise ValueError("startup.start_check must be a mapping")
    if (start_check or {}).get("kind") not in (None, "text", "image", "scorer"):
        raise ValueError("unsupported startup.start_check.kind")
    value = (start_check or {}).get("settle_seconds", 0.0)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise ValueError("startup.start_check.settle_seconds must be finite and nonnegative")
    return float(value)


def resolve_tics_per_second(runtime: dict | None) -> int:
    """Return the configured positive integer tic rate.

    Missing values retain the historical Windows benchmark contract:
    10 tics per second, or 0.1 seconds per tic.
    """
    value = (runtime or {}).get(
        "tics_per_second", DEFAULT_TICS_PER_SECOND)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            "runtime.tics_per_second must be a positive integer"
        )
    return value


def format_seconds_per_tic(tics_per_second: int) -> str:
    """Human-readable reciprocal for prompt and diagnostic text."""
    return f"{1.0 / tics_per_second:.6g}"
