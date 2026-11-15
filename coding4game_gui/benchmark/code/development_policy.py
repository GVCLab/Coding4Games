"""Audited, method-neutral development review instructions; not extra resources."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


def review_fraction(value):
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= 1):
        raise ValueError("Minimum development fraction must be finite and greater than zero through one")
    return float(value)


def review_notice(fraction, *, tic_budget, token_limit):
    """Describe the human's selection guideline without granting an allowance."""
    fraction = review_fraction(fraction)
    if fraction is None:
        return ""
    percent = f"{100 * fraction:g}%"
    thresholds = [f"cumulative development gameplay reaches {math.ceil(tic_budget * fraction)} "
                  f"tics ({percent} of the original {tic_budget})"]
    if token_limit is not None:
        thresholds.insert(0, f"confirmed model-token usage reaches {math.ceil(token_limit * fraction)} "
                          f"tokens ({percent} of the original {token_limit})")
    return (
        "\n\nHuman-approved development review policy: continue meaningful development and "
        "validation until either " + " or ".join(thresholds) + ". While every listed threshold "
        "is unmet, do not voluntarily finalize solely because a minimal fallback runs or "
        "historical usage is unknown. Preserve a runnable fallback early. Do not issue "
        "pointless waits or purposeless experiments merely to consume resources. Once any "
        "listed threshold is met, voluntary selection is allowed, not required. This changes "
        "the voluntary selection guideline only: original budget ceilings, deadline, safety "
        "and infrastructure failure handling still apply. Unknown usage remains potentially "
        "billable; the token threshold uses the confirmed subtotal and does not establish an "
        "exact remaining token allowance. No independent final-test feedback or strategy is "
        "supplied by this notice."
    )


def load_budget_notice(args, root, raw):
    """Require a host grant bound to the closed configuration before adding a notice."""
    value = getattr(args, "recovery_budget_notice", None)
    if not value:
        return None
    path = Path(value)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("Recovery budget notice must be a regular host JSON file of at most 16384 bytes")
    payload = path.read_bytes()
    if len(payload) > 16384:
        raise ValueError("Recovery budget notice exceeds 16384 bytes")
    try:
        grant = json.loads(payload)
    except (ValueError, UnicodeError) as error:
        raise ValueError("Invalid recovery budget notice JSON") from error
    fields = {"kind", "run_dir", "source_config_sha256", "minimum_development_fraction",
              "authorization_basis"}
    terminal_notice = isinstance(grant, dict) and grant.get("kind") == "c4g-recovery-budget-notice-v2"
    if terminal_notice:
        fields.add("require_registered_terminal_evaluation")
    if not isinstance(grant, dict) or set(grant) != fields:
        raise ValueError("Recovery budget notice has unexpected or missing fields")
    if (grant["kind"] not in {"c4g-recovery-budget-notice-v1", "c4g-recovery-budget-notice-v2"}
            or not isinstance(grant["run_dir"], str) or Path(grant["run_dir"]).resolve() != root
            or grant["source_config_sha256"] != hashlib.sha256(raw).hexdigest()
            or not isinstance(grant["authorization_basis"], str)
            or not grant["authorization_basis"].strip()):
        raise ValueError("Recovery budget notice does not match the original configuration or human authorization")
    if review_fraction(grant["minimum_development_fraction"]) is None:
        raise ValueError("Recovery budget notice requires a minimum development fraction")
    if terminal_notice and grant["require_registered_terminal_evaluation"] is not True:
        raise ValueError("Terminal evaluation notice requires an explicit true requirement")
    return {"authorization": grant, "authorization_sha256": hashlib.sha256(payload).hexdigest(),
            "authorization_bytes": len(payload)}
