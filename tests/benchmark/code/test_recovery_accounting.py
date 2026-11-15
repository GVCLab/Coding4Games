import json

import pytest

from coding4game_gui.benchmark.code.recovery_accounting import (
    RecoveryAccountingError, read_gateway_journal, reconstruct_usage,
    recover_deadline,
)


def request(identity, *, strict=False):
    return [{"event": "request", "request_id": identity},
            {"event": "reservation", "request_id": identity,
             "token_accounting": "strict" if strict else "usage",
             "token_reservation": 50 if strict else None}]


def usage(identity, known=30):
    return {"event": "usage", "request_id": identity, "usage": known,
            "charged_tokens": known,
            "token_breakdown": {"input_tokens": 25, "cached_input_tokens": 5,
                                "uncached_input_tokens": 20, "output_tokens": 5}}


def test_repeated_reconstruction_does_not_double_count_settled_or_unknown_requests():
    records = request("settled") + [usage("settled"), usage("settled")]
    records += request("missing-response")
    records += request("settled-unknown") + [{
        "event": "usage", "request_id": "settled-unknown", "usage": "unknown",
        "charged_tokens": None, "token_breakdown": {}}]
    a = reconstruct_usage(records)
    assert a == reconstruct_usage(records, minimum=a)
    assert a["tokens_reported"] == a["tokens_used"] == 30
    assert a["unknown_usage_requests"] == 2
    assert a["unreconciled_request_ids"] == ["missing-response"]
    assert a["token_breakdown_unknown_requests"]["output_tokens"] == 2
    assert a["token_breakdown"]["cached_input_tokens"] == 5


def test_a_request_not_admitted_or_explicitly_not_sent_adds_no_charge():
    records = [{"event": "request", "request_id": "rejected"}]
    records += request("not-sent") + [{"event": "request_not_sent",
                                       "request_id": "not-sent"}]
    result = reconstruct_usage(records)
    assert result["tokens_used"] == result["unknown_usage_requests"] == 0


def test_strict_unknown_preserves_reservation_and_refuses_accounting_change():
    result = reconstruct_usage(request("ambiguous", strict=True), accounting="strict")
    assert result["tokens_used"] == 50 and result["tokens_reported"] == 0
    assert result["unknown_usage_requests"] == 1
    with pytest.raises(RecoveryAccountingError, match="mode changed"):
        reconstruct_usage(request("ambiguous", strict=True))


@pytest.mark.parametrize("tail", [
    [dict(usage("a"), usage=31, charged_tokens=31)],
    [{"event": "request_not_sent", "request_id": "a"}],
    [dict(usage("a"), charged_tokens=29)],
])
def test_conflicting_settlement_is_never_silently_reconciled(tail):
    with pytest.raises(RecoveryAccountingError):
        reconstruct_usage(request("a") + [usage("a")] + tail)


def test_missing_component_is_unknown_even_with_known_total():
    row = usage("a")
    row["token_breakdown"]["cached_input_tokens"] = None
    result = reconstruct_usage(request("a") + [row])
    assert result["tokens_reported"] == 30
    assert result["token_breakdown_unknown_requests"]["cached_input_tokens"] == 1


def test_high_water_mark_prevents_reset_after_a_missing_durable_settlement():
    minimum = reconstruct_usage(request("a") + [usage("a")])
    with pytest.raises(RecoveryAccountingError, match="high-water"):
        reconstruct_usage(request("a"), minimum=minimum)


def test_finalization_warning_state_is_preserved_and_rearming_is_ordered():
    records = [{"event": "finalization_warning_injected"},
               {"event": "finalization_warning_rearmed"}]
    assert reconstruct_usage(records)["finalization_warning_injected"] is False
    records.append({"event": "finalization_warning_injected"})
    assert reconstruct_usage(records)["finalization_warning_injected"] is True


def test_crash_tail_is_identified_without_rewriting_evidence(tmp_path):
    path = tmp_path / "model_gateway.jsonl"
    raw = b"".join((json.dumps(x) + "\n").encode() for x in request("a"))
    raw += b'{"event":"usage","request_id":"a"'
    path.write_bytes(raw)
    rows, source = read_gateway_journal(path)
    assert source["truncated_tail"] is True and source["bytes"] == len(raw)
    assert reconstruct_usage(rows)["unknown_usage_requests"] == 1
    assert path.read_bytes() == raw
    path.write_bytes(b"not-json\n" + raw)
    with pytest.raises(RecoveryAccountingError, match="Malformed"):
        read_gateway_journal(path)


def test_accounting_reader_keeps_payloads_out_of_its_recovery_view(tmp_path):
    path = tmp_path / "gateway.jsonl"
    rows = request("a") + [usage("a")]
    rows[0]["request"] = {"messages": "large private session context"}
    rows.append({"event": "response_chunk", "data": "large response data"})
    path.write_text("".join(json.dumps(x) + "\n" for x in rows))
    parsed, _ = read_gateway_journal(path)
    assert len(parsed) == 3 and "request" not in parsed[0]
    assert reconstruct_usage(parsed)["tokens_used"] == 30


def test_original_deadline_is_not_reset_on_repeated_resume():
    assert recover_deadline(100, 60, now_unix=140, monotonic=500) == 520
    assert recover_deadline(100, 60, now_unix=150, monotonic=900) == 910
    assert recover_deadline(100, None, now_unix=1000, monotonic=900) == float("inf")
    with pytest.raises(RecoveryAccountingError, match="exhausted"):
        recover_deadline(100, 60, now_unix=160, monotonic=900)
    with pytest.raises(RecoveryAccountingError, match="clock moved"):
        recover_deadline(100, 60, now_unix=99, monotonic=900)
