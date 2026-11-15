import copy
import hashlib
import http.client
import json

import pytest

from coding4game_gui.benchmark.code.model_gateway import ModelGateway


SESSION = "4cc847b0-5661-4658-aaf2-f22d49d2f728"
USER_ID = json.dumps({"device_id": "private-device-marker", "account_uuid": "private-account-marker", "session_id": SESSION})


class Response:
    def __init__(self, value, status=200):
        self.status = status
        self.headers = {"content-type": "application/json"}
        self.body = json.dumps(value).encode()

    def iter_bytes(self):
        yield self.body

    def close(self):
        pass


def post(gateway, payload):
    connection = http.client.HTTPConnection("127.0.0.1", gateway.port, timeout=10)
    try:
        connection.request("POST", "/v1/messages", json.dumps(payload),
                           {"Content-Type": "application/json", "x-api-key": gateway.token,
                            "anthropic-beta": "context-management-2025-06-27"})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def native_request():
    return {
        "model": "claude-opus-5-5", "max_tokens": 64,
        "system": [{"type": "text", "text": "Keep the existing development task."}],
        "metadata": {"user_id": USER_ID}, "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
        "tools": [{"name": name, "input_schema": {"type": "object"}} for name in ["Bash", "status"]],
        "messages": [
            {"role": "user", "content": f"Continue the original session {SESSION}."},
            {"role": "system", "content": "Original native instruction."},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "Preserved original reasoning.", "signature": "original-signature"},
                {"type": "tool_use", "id": "call_a", "name": "status", "input": {}},
                {"type": "tool_use", "id": "call_b", "name": "Bash", "input": {"command": "ls"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_a", "content": "ready"},
                {"type": "tool_result", "tool_use_id": "call_b", "content": "policy.py"},
            ]},
            {"role": "system", "content": "Original later instruction."},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call_c", "name": "Bash", "input": {"command": "pwd"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_c", "content": "/workspace"}]},
        ],
    }


def test_opt_in_metadata_repair_preserves_native_history_and_original_accounting(tmp_path):
    forwarded = []

    def upstream(method, url, headers, body, timeout):
        request = json.loads(body)
        forwarded.append((url, headers, request))
        if request.get("metadata", {}).get("user_id") != SESSION:
            return Response({"error": {"message": "Invalid message sequence"}}, 400)
        return Response({"usage": {"input_tokens": 10, "output_tokens": 2}, "content": []})

    payload = native_request()
    original = copy.deepcopy(payload)
    log = tmp_path / "gateway.jsonl"
    gateway = ModelGateway("https://provider.invalid/claude-aws", "host-secret", "claude-opus-5-5", 62, None, log,
                           transport=upstream, protocol="anthropic", strict_total_enforcement=False,
                           anthropic_user_id_mode="session_uuid", anthropic_session_id=SESSION)
    gateway.restore_usage({"tokens_used": 50, "tokens_reported": 50, "unknown_usage_requests": 2,
                               "token_breakdown": {"input_tokens": 40, "cached_input_tokens": 0, "uncached_input_tokens": 40, "output_tokens": 10},
                               "token_breakdown_unknown_requests": dict.fromkeys(["input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens"], 2),
                          "last_settled_request_tokens": 50, "finalization_warning_injected": True})
    with gateway:
        assert post(gateway, payload)[0] == 200
        state = gateway.status()
        assert state["tokens_reported"] == 62 and state["unknown_usage_requests"] == 2
        assert state["tokens_remaining"] is None
        assert post(gateway, payload)[0] == 403
    assert len(forwarded) == 1 and payload == original
    url, headers, request = forwarded[0]
    assert url == "https://provider.invalid/claude-aws/v1/messages"
    assert headers["x-api-key"] == "host-secret"
    assert headers["anthropic-beta"] == "context-management-2025-06-27"
    assert request["metadata"] == {"user_id": SESSION}
    for key in ["messages", "tools", "thinking", "output_config", "max_tokens"]:
        assert request[key] == original[key]
    assert request["system"][:1] == original["system"]
    assert len(request["system"]) == 2
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    transform = next(row for row in records if row.get("event") == "request_compatibility_transform")
    assert transform["transformation"] == "anthropic_metadata_user_id_to_session_uuid"
    assert transform["original_user_id_sha256"] == hashlib.sha256(USER_ID.encode()).hexdigest()
    assert "private-device-marker" not in log.read_text(encoding="utf-8")
    assert "private-account-marker" not in log.read_text(encoding="utf-8")


def test_default_metadata_behavior_remains_unchanged(tmp_path):
    seen = []

    def upstream(method, url, headers, body, timeout):
        seen.append(json.loads(body))
        return Response({"error": {"message": "Invalid message sequence"}}, 400)

    with ModelGateway("https://provider.invalid", "secret", "claude-opus-5-5", 100, 30, tmp_path / "default.jsonl",
                      transport=upstream, protocol="anthropic", strict_total_enforcement=False) as gateway:
        assert post(gateway, native_request())[0] == 400
        assert gateway.status()["unknown_usage_requests"] == 1
    assert seen[0]["metadata"] == {"user_id": USER_ID}


def test_compatibility_preserves_other_metadata_and_absent_user_id(tmp_path):
    seen = []

    def upstream(method, url, headers, body, timeout):
        seen.append(json.loads(body))
        return Response({"usage": {"input_tokens": 1, "output_tokens": 1}, "content": []})

    with ModelGateway("https://provider.invalid", "secret", "claude-opus-5-5", 100, 30, tmp_path / "other.jsonl",
                      transport=upstream, protocol="anthropic", strict_total_enforcement=False,
                      anthropic_user_id_mode="session_uuid", anthropic_session_id=SESSION) as gateway:
        payload = native_request()
        payload["metadata"]["retained_field"] = "retained"
        assert post(gateway, payload)[0] == 200
        payload.pop("metadata")
        assert post(gateway, payload)[0] == 200
    assert seen[0]["metadata"] == {"user_id": SESSION, "retained_field": "retained"}
    assert "metadata" not in seen[1]


@pytest.mark.parametrize("mode,protocol", [("invalid", "anthropic"), ("session_uuid", "responses")])
def test_invalid_compatibility_modes_fail_closed(tmp_path, mode, protocol):
    with pytest.raises(ValueError, match="metadata|user_id"):
        ModelGateway("https://provider.invalid", "secret", "model", 100, 30, tmp_path / "invalid.jsonl",
                     protocol=protocol, anthropic_user_id_mode=mode, anthropic_session_id=SESSION)


def test_compatibility_rejects_another_session_without_sending_or_charging(tmp_path):
    def upstream(*args):
        pytest.fail("A mismatched native session must never reach the provider")

    with ModelGateway("https://provider.invalid", "secret", "claude-opus-5-5", 100, 30, tmp_path / "mismatch.jsonl",
                      transport=upstream, protocol="anthropic", strict_total_enforcement=False,
                      anthropic_user_id_mode="session_uuid", anthropic_session_id=SESSION) as gateway:
        payload = native_request()
        payload["metadata"]["user_id"] = json.dumps({"session_id": "00000000-0000-4000-8000-000000000000"})
        status, body = post(gateway, payload)
        assert status == 400 and b"metadata_session_mismatch" in body
        assert gateway.status()["unknown_usage_requests"] == 0
        assert gateway.status()["tokens_reported"] == 0


@pytest.mark.parametrize("session", [None, "invalid", SESSION.upper()])
def test_compatibility_requires_canonical_original_uuid(tmp_path, session):
    with pytest.raises(ValueError, match="session"):
        ModelGateway("https://provider.invalid", "secret", "claude-opus-5-5", 100, 30, tmp_path / "invalid-session.jsonl",
                     protocol="anthropic", strict_total_enforcement=False,
                     anthropic_user_id_mode="session_uuid", anthropic_session_id=session)


def test_explicit_http_400_policy_preserves_error_and_blocks_later_paid_requests(tmp_path):
    calls = []
    error = {"error": {"type": "<nil>", "message": "Invalid message sequence"}}

    def upstream(*args):
        calls.append(args)
        return Response(error, 400)

    with ModelGateway("https://provider.invalid", "secret", "claude-opus-5-5", 100, 30,
                      tmp_path / "stop-400.jsonl", protocol="anthropic", transport=upstream,
                      strict_total_enforcement=False, stop_on_http_400=True) as gateway:
        status, body = post(gateway, native_request())
        assert status == 400 and json.loads(body) == error
        assert gateway.status()["stop_reason"] == "upstream_http_400"
        assert gateway.status()["unknown_usage_requests"] == 1
        assert post(gateway, native_request())[0] == 403
    assert len(calls) == 1


def test_http_400_policy_default_retains_native_compatibility_retries(tmp_path):
    calls = []

    def upstream(*args):
        calls.append(args)
        return Response({"error": {"message": "Unsupported beta"}}, 400)

    with ModelGateway("https://provider.invalid", "secret", "claude-opus-5-5", 100, 30,
                      tmp_path / "default-400.jsonl", protocol="anthropic", transport=upstream,
                      strict_total_enforcement=False) as gateway:
        assert post(gateway, native_request())[0] == 400
        assert post(gateway, native_request())[0] == 400
    assert len(calls) == 2


def test_http_400_policy_keeps_transient_502_recovery_available(tmp_path):
    calls = []

    def upstream(*args):
        calls.append(args)
        return (Response({"error": {"message": "Bad gateway"}}, 502) if len(calls) == 1 else
                Response({"usage": {"input_tokens": 10, "output_tokens": 2}, "content": []}))

    with ModelGateway("https://provider.invalid", "secret", "claude-opus-5-5", 100, 30,
                      tmp_path / "transient-502.jsonl", protocol="anthropic", transport=upstream,
                      strict_total_enforcement=False, stop_on_http_400=True) as gateway:
        assert post(gateway, native_request())[0] == 502
        assert not gateway.status()["revoked"]
        assert post(gateway, native_request())[0] == 200
        assert gateway.status()["tokens_reported"] == 12
        assert gateway.status()["unknown_usage_requests"] == 1
    assert len(calls) == 2
