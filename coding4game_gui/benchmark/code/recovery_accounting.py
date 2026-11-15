"""Reconstruct conservative development accounting from host-owned evidence.

This module does not reconnect processes or replay operations. The recovery
controller must first establish that the previous owner and requests are no
longer live. Unsettled reservations remain potentially billable unknowns.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


COMPONENTS = ("input_tokens", "cached_input_tokens", "uncached_input_tokens",
              "output_tokens")
_EVENTS = {"request", "reservation", "usage", "request_not_sent",
           "finalization_warning_injected", "finalization_warning_rearmed"}
_FIELDS = ("event", "request_id", "token_accounting", "token_reservation",
           "usage", "charged_tokens", "token_breakdown")


class RecoveryAccountingError(ValueError):
    """Durable evidence cannot establish a safe cumulative accounting state."""


def _count(value, label):
    if type(value) is not int or value < 0:
        raise RecoveryAccountingError(f"Invalid recovery counter: {label}")
    return value


def read_gateway_journal(path):
    """Read complete JSONL records and identify a crash-truncated final record.

    Keep historical bytes immutable. A later controller must append into a new
    segment when a truncated tail exists, rather than joining text onto it.
    """
    path = Path(path)
    rows, digest, size, truncated = [], hashlib.sha256(), 0, False
    with path.open("rb") as stream:
        for line in stream:
            digest.update(line)
            size += len(line)
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError) as error:
                if line.endswith(b"\n"):
                    raise RecoveryAccountingError("Malformed durable gateway record") from error
                truncated = True
                continue
            if not isinstance(row, dict):
                raise RecoveryAccountingError("Gateway record must be an object")
            # Request bodies and response chunks can contain large image data.
            # They remain in the hashed source, not in this accounting view.
            if row.get("event") in _EVENTS:
                rows.append({key: row[key] for key in _FIELDS if key in row})
    return rows, {"path": str(path.resolve()), "bytes": size,
                  "sha256": digest.hexdigest(), "truncated_tail": truncated}


def reconstruct_usage(records, *, accounting="usage", minimum=None):
    """Fold request identities once, retaining every ambiguous accepted request.

    Repeated identical usage evidence is idempotent; conflicting settlement is
    rejected. A reservation without settlement or explicit not-sent evidence is
    conservatively unknown, even if no connected response was recorded.
    """
    if accounting not in {"usage", "strict"}:
        raise RecoveryAccountingError("Unsupported original accounting mode")
    requests, order, warning = {}, [], False
    for row in records:
        event = row.get("event")
        if event == "finalization_warning_injected":
            warning = True
        elif event == "finalization_warning_rearmed":
            warning = False
        if event not in {"request", "reservation", "usage", "request_not_sent"}:
            continue
        request_id = row.get("request_id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise RecoveryAccountingError("Missing durable request identity")
        if request_id not in requests:
            requests[request_id] = {}
            order.append(request_id)
        state = requests[request_id]
        if event == "request":
            if state.get("requested"):
                raise RecoveryAccountingError("Repeated request admission identity")
            state["requested"] = True
        elif event == "reservation":
            if not state.get("requested") or "reservation" in state:
                raise RecoveryAccountingError("Invalid request reservation sequence")
            if row.get("token_accounting") != accounting:
                raise RecoveryAccountingError("Original accounting mode changed")
            state["reservation"] = (_count(row.get("token_reservation"), "reservation")
                                    if accounting == "strict" else 0)
        elif event == "request_not_sent":
            if "usage" in state:
                raise RecoveryAccountingError("Settled request also marked not sent")
            state["not_sent"] = True
        else:
            if "reservation" not in state or state.get("not_sent"):
                raise RecoveryAccountingError("Settlement without an admitted request")
            settlement = {key: row.get(key) for key in
                          ("usage", "charged_tokens", "token_breakdown")}
            if "usage" in state and state["usage"] != settlement:
                raise RecoveryAccountingError("Conflicting request settlement")
            state["usage"] = settlement

    result = {"tokens_used": 0, "tokens_reported": 0, "unknown_usage_requests": 0,
              "token_breakdown": dict.fromkeys(COMPONENTS, 0),
              "token_breakdown_unknown_requests": dict.fromkeys(COMPONENTS, 0),
              "last_settled_request_tokens": None,
              "finalization_warning_injected": warning, "request_in_flight": False,
              "unreconciled_request_ids": []}
    for request_id in order:
        state = requests[request_id]
        if "reservation" not in state or state.get("not_sent"):
            continue
        usage = state.get("usage")
        if usage is None or usage["usage"] == "unknown":
            if usage is None:
                result["unreconciled_request_ids"].append(request_id)
            charge = state["reservation"]
            if usage is not None and accounting == "strict":
                if _count(usage["charged_tokens"], "unknown charge") != charge:
                    raise RecoveryAccountingError("Unknown reservation charge changed")
            result["tokens_used"] += charge
            result["unknown_usage_requests"] += 1
            for name in COMPONENTS:
                result["token_breakdown_unknown_requests"][name] += 1
            continue
        known = _count(usage["usage"], "confirmed usage")
        if _count(usage["charged_tokens"], "confirmed charge") != known:
            raise RecoveryAccountingError("Confirmed usage and charge disagree")
        components = usage["token_breakdown"]
        if not isinstance(components, dict):
            raise RecoveryAccountingError("Missing token component evidence")
        result["tokens_used"] += known
        result["tokens_reported"] += known
        if known:
            result["last_settled_request_tokens"] = known
        for name in COMPONENTS:
            value = components.get(name)
            if value is None:
                result["token_breakdown_unknown_requests"][name] += 1
            else:
                result["token_breakdown"][name] += _count(value, name)
    result["usage_unknown"] = bool(result["unknown_usage_requests"])
    if minimum is not None:
        for key in ("tokens_used", "tokens_reported", "unknown_usage_requests"):
            if result[key] < _count(minimum[key], key):
                raise RecoveryAccountingError("Journal is below a durable accounting high-water mark")
        for key in ("token_breakdown", "token_breakdown_unknown_requests"):
            for name in COMPONENTS:
                if result[key][name] < _count(minimum[key][name], name):
                    raise RecoveryAccountingError("Journal is below a component high-water mark")
        minimum_warning = minimum.get("finalization_warning_injected", False)
        if type(minimum_warning) is not bool:
            raise RecoveryAccountingError("Invalid finalization warning evidence")
        result["finalization_warning_injected"] |= minimum_warning
    return result


def recover_deadline(started_unix, wall_seconds, *, now_unix, monotonic):
    """Translate the original absolute deadline without granting another period."""
    if wall_seconds is None:
        return float("inf")
    values = (started_unix, wall_seconds, now_unix, monotonic)
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) for v in values) or wall_seconds <= 0:
        raise RecoveryAccountingError("Invalid original deadline evidence")
    if now_unix < started_unix:
        raise RecoveryAccountingError("Host clock moved before original start")
    remaining = started_unix + wall_seconds - now_unix
    if remaining <= 0:
        raise RecoveryAccountingError("Original development deadline exhausted")
    return monotonic + remaining
