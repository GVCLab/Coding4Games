import copy
import http.client
import hashlib
import json

import pytest

from coding4game_gui.benchmark.code.anthropic_replay import (
    ToolInputReplay, ToolUseCollector, sources_from_records, sources_from_journals,
)
from coding4game_gui.benchmark.code.model_gateway import ModelGateway


SESSION = "00000000-0000-4000-8000-000000000001"
MODEL = "claude-opus-5-5"
CALL = {"type": "tool_use", "id": "native-call", "name": "Bash",
        "input": {"command": "cd /workspace/dev && python3 policy.py", "timeout": 1000}}


def native_payload():
    replay = copy.deepcopy(CALL)
    replay["input"]["command"] = "python3 policy.py"
    return {"model": MODEL, "max_tokens": 64, "metadata": {"user_id": SESSION},
            "thinking": {"type": "adaptive"}, "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
            "messages": [{"role": "user", "content": "Continue."},
                         {"role": "assistant", "content": [
                             {"type": "thinking", "thinking": "Native reasoning", "signature": "preserved-signature"}, replay]},
                         {"role": "user", "content": [
                             {"type": "tool_result", "tool_use_id": CALL["id"], "content": "Finished."}]}]}


class Response:
    def __init__(self, value, status=200, stream=False):
        self.status = status
        self.headers = {"content-type": "text/event-stream" if stream else "application/json"}
        self.body = (b"".join(b"data: " + json.dumps(event).encode() + b"\r\n\r\n" for event in value)
                     if stream else json.dumps(value).encode())

    def iter_bytes(self):
        for index in range(0, len(self.body), 7):
            yield self.body[index:index + 7]

    def close(self):
        pass


def post(gateway, value):
    connection = http.client.HTTPConnection("127.0.0.1", gateway.port, timeout=10)
    try:
        connection.request("POST", "/v1/messages", json.dumps(value),
                           {"x-api-key": gateway.token, "content-type": "application/json"})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def gateway(tmp_path, transport, **options):
    return ModelGateway("https://provider.invalid", "host-key", MODEL, 100000, 30,
                        tmp_path / "gateway.jsonl", protocol="anthropic", transport=transport,
                        strict_total_enforcement=False,
                        anthropic_user_id_mode="session_uuid", anthropic_session_id=SESSION, **options)


def events(call=CALL, *, complete=True):
    block = copy.deepcopy(call)
    block["input"] = {}
    result = [{"type": "message_start", "message": {"model": MODEL, "usage": {"input_tokens": 5, "output_tokens": 0}}},
              {"type": "content_block_start", "index": 0, "content_block": block}]
    value = json.dumps(call["input"])
    result += [{"type": "content_block_delta", "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": value[:9]}},
               {"type": "content_block_delta", "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": value[9:]}}]
    result += [{"type": "content_block_stop", "index": 0},
               {"type": "message_delta", "usage": {"output_tokens": 2}}]
    if complete:
        result.append({"type": "message_stop"})
    return result


def test_recovered_native_command_is_restored_without_changing_any_other_field(tmp_path):
    sent = []

    def upstream(method, url, headers, body, timeout):
        value = json.loads(body)
        sent.append(value)
        if value["messages"][1]["content"][1]["input"] != CALL["input"]:
            return Response({"error": {"message": "Invalid message sequence"}}, 400)
        return Response({"type": "message", "model": MODEL, "content": [],
                         "usage": {"input_tokens": 5, "output_tokens": 2}})

    payload = native_payload()
    with gateway(tmp_path, upstream, anthropic_tool_replay_mode="restore_leading_cd",
                 anthropic_tool_replay_sources=[{"request_id": "old-native-response", "tool_use": CALL}]) as current:
        assert post(current, payload)[0] == 200
        assert current.status()["tokens_reported"] == 7
        assert current.status()["unknown_usage_requests"] == 0
    expected = copy.deepcopy(payload)
    expected["messages"][1]["content"][1] = CALL
    assert "Host model budget snapshot" in sent[0].pop("system")
    assert sent == [expected]
    assert payload == native_payload()
    rows = [json.loads(line) for line in (tmp_path / "gateway.jsonl").read_text().splitlines()]
    changes = [r for r in rows if r.get("transformation") == "anthropic_restore_leading_cd_tool_input"]
    assert len(changes) == 1 and changes[0]["source_request_id"] == "old-native-response"
    assert "command" not in changes[0]


def test_live_complete_stream_seeds_the_following_native_request(tmp_path):
    sent = []

    def upstream(method, url, headers, body, timeout):
        sent.append(json.loads(body))
        return Response(events(), stream=True) if len(sent) == 1 else Response(
            {"type": "message", "model": MODEL, "content": [], "usage": {"input_tokens": 6, "output_tokens": 1}})

    with gateway(tmp_path, upstream, anthropic_tool_replay_mode="restore_leading_cd") as current:
        first = {"model": MODEL, "max_tokens": 64, "metadata": {"user_id": SESSION},
                 "messages": [{"role": "user", "content": "Start."}]}
        assert post(current, first)[0] == 200
        assert post(current, native_payload())[0] == 200
        assert current.status()["tokens_reported"] == 14
    assert sent[1]["messages"][1]["content"][1]["input"] == CALL["input"]


def test_default_gateway_does_not_restore_inputs(tmp_path):
    sent = []

    def upstream(method, url, headers, body, timeout):
        sent.append(json.loads(body))
        return Response({"usage": {"input_tokens": 1, "output_tokens": 1}})

    with gateway(tmp_path, upstream) as current:
        assert post(current, native_payload())[0] == 200
    assert "Host model budget snapshot" in sent[0].pop("system")
    assert sent[0] == native_payload()


@pytest.mark.parametrize("mutation", ["command", "timeout", "name", "duplicate"])
def test_unrelated_input_changes_fail_before_paid_forwarding(tmp_path, mutation):
    def upstream(*args):
        pytest.fail("Unverified history changes must not reach the provider")

    payload = native_payload()
    call = payload["messages"][1]["content"][1]
    if mutation == "command":
        call["input"]["command"] = "rm policy.py"
    elif mutation == "timeout":
        call["input"]["timeout"] = 2000
    elif mutation == "name":
        call["name"] = "other"
    else:
        payload["messages"][1]["content"].append(copy.deepcopy(call))
    with gateway(tmp_path, upstream, anthropic_tool_replay_mode="restore_leading_cd",
                 anthropic_tool_replay_sources=[{"request_id": "old", "tool_use": CALL}]) as current:
        status, body = post(current, payload)
        assert status == 400 and b"tool_replay_mismatch" in body
        assert current.status()["tokens_reported"] == 0
        assert current.status()["unknown_usage_requests"] == 0


@pytest.mark.parametrize("complete,error,model", [(False, False, MODEL), (True, True, MODEL), (True, False, "other-model")])
def test_untrusted_streams_never_supply_original_inputs(complete, error, model):
    collector = ToolUseCollector(model)
    for value in events(complete=complete):
        collector.observe(value)
    if error:
        collector.observe({"type": "error"})
    assert collector.calls() == []


def test_durable_sources_require_complete_matching_model_response():
    response = Response(events(), stream=True)
    records = [{"event": "attempt", "request_id": "original", "upstream_status": 200}]
    records += [{"event": "response_chunk", "request_id": "original", "data_hex": chunk.hex()}
                for chunk in response.iter_bytes()]
    assert sources_from_records(records, MODEL) == [{"request_id": "original", "tool_use": CALL}]
    assert sources_from_records(records[:-3], MODEL) == []
    assert sources_from_records(records, "other-model") == []


def test_conflicting_original_ids_are_rejected():
    other = copy.deepcopy(CALL)
    other["input"]["command"] = "different"
    with pytest.raises(ValueError, match="Conflicting"):
        ToolInputReplay([{"request_id": "a", "tool_use": CALL}, {"request_id": "b", "tool_use": other}])


def test_native_journal_bytes_must_match_the_accounting_snapshot(tmp_path):
    path = tmp_path / "native.jsonl"
    raw = (json.dumps({"event": "attempt", "request_id": "a", "upstream_status": 200}) + "\n").encode()
    path.write_bytes(raw)
    source = {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    assert sources_from_journals([source], MODEL) == []
    path.write_bytes(raw + b"\n")
    with pytest.raises(ValueError):
        sources_from_journals([source], MODEL)


@pytest.mark.parametrize("original,replay,allowed", [
    ("cd '/workspace/some dir' && run", "run", True),
    ("cd -- /workspace && run", "run", True),
    ("cd relative && run", "run", False),
    ("cd /workspace; run", "run", False),
    ("cd /workspace && echo changed; run", "run", False),
])
def test_only_leading_absolute_cd_normalization_is_eligible(original, replay, allowed):
    assert ToolInputReplay._removed_cd(original, replay) is allowed
