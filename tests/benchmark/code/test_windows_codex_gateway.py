import http.client
import json
import threading
import time

import pytest

from coding4game_gui.benchmark.code.model_gateway import ModelGateway
from coding4game_gui.benchmark.code.model_gateway import RetryableBeforeSendError


class FakeResponse:
    def __init__(self, body, *, status=200, headers=None, chunks=None):
        self.status = status
        self.headers = headers or {"content-type": "application/json"}
        self._chunks = chunks or [body]

    def iter_bytes(self):
        yield from self._chunks

    def close(self):
        pass


class FakeTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, json.loads(body), timeout))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class FakeSleep:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def test_chat_upstream_routes_through_chat_completions_and_reports_mode(tmp_path):
    upstream = FakeTransport([FakeResponse(b'{"usage":{"total_tokens":4}}')])
    with ModelGateway(
        "https://provider.invalid/v1", "secret", "kimi-k3", None, None,
        tmp_path / "chat.jsonl", transport=upstream,
        strict_total_enforcement=False, upstream_api="chat",
        provider_model="openai/kimi-k3",
    ) as gateway:
        assert _post(gateway, "/v1/responses", {"input": "hello"})[0] == 200
        assert gateway.status()["upstream_api"] == "chat"
    assert upstream.calls[0][1] == "https://provider.invalid/v1/chat/completions"


def test_usage_breakdown_accumulates_once_and_preserves_unknowns(tmp_path):
    usage = {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 80},
             "output_tokens": 7, "total_tokens": 107}
    event = b'data: ' + json.dumps({"type": "response.completed", "response": {"usage": usage}}).encode() + b'\n\n'
    upstream = FakeTransport([
        FakeResponse(b'', headers={"content-type": "text/event-stream"}, chunks=[event[:20], event[20:]]),
        FakeResponse(b'{"usage":{"total_tokens":11}}'), FakeResponse(b'{}')])
    log = tmp_path / 'usage.jsonl'
    with ModelGateway('https://provider.invalid/v1', 'secret', 'm', None, None,
                      log, transport=upstream, strict_total_enforcement=False) as gateway:
        for _ in range(3):
            assert _post(gateway, '/v1/responses', {'input': 'hello'})[0] == 200
        state = gateway.status()
        assert state['tokens_reported'] == 118
        assert state['token_breakdown'] == dict(input_tokens=100, cached_input_tokens=80,
                                               uncached_input_tokens=20, output_tokens=7)
        assert set(state['token_breakdown_unknown_requests'].values()) == {2}
        assert state['unknown_usage_requests'] == 1
        assert 'cached_input_tokens=80 (incomplete: 2 requests)' in state['budget_message']
    records = [json.loads(line) for line in log.read_text().splitlines()]
    charges = [r for r in records if r['event'] == 'usage']
    assert len(charges) == 3
    assert charges[0]['token_breakdown'] == state['token_breakdown']
    assert charges[1]['token_breakdown']['cached_input_tokens'] is None


def test_request_planning_estimate_uses_latest_settled_full_usage(tmp_path):
    upstream = FakeTransport([
        FakeResponse(b'{"usage":{"input_tokens":20,"output_tokens":5}}'),
        FakeResponse(b'{"usage":{"input_tokens":30,"input_tokens_details":{"cached_tokens":24},"output_tokens":10}}'),
    ])
    with ModelGateway('https://provider.invalid/v1', 'secret', 'm', 200, 60,
                      tmp_path / 'planning.jsonl', transport=upstream,
                      strict_total_enforcement=False) as gateway:
        initial = gateway.status()
        assert initial['last_settled_request_tokens'] is None
        assert initial['estimated_model_requests_remaining'] is None
        assert initial['model_request_estimate_basis'] is None

        assert _post(gateway, '/v1/responses', {'input': 'first'})[0] == 200
        first = gateway.status()
        assert first['last_settled_request_tokens'] == 25
        assert first['estimated_model_requests_remaining'] == 7

        assert _post(gateway, '/v1/responses', {'input': 'second'})[0] == 200
        second = gateway.status()
        assert second['tokens_remaining'] == 135
        assert second['last_settled_request_tokens'] == 40
        assert second['estimated_model_requests_remaining'] == 3
        assert second['model_request_estimate_basis'] == 'latest_settled_response_usage'


def test_request_planning_estimate_is_null_when_usage_becomes_unknown(tmp_path):
    upstream = FakeTransport([
        FakeResponse(b'{"usage":{"total_tokens":25}}'),
        FakeResponse(b'{}'),
    ])
    with ModelGateway('https://provider.invalid/v1', 'secret', 'm', 200, 60,
                      tmp_path / 'planning-unknown.jsonl', transport=upstream,
                      strict_total_enforcement=False) as gateway:
        assert _post(gateway, '/v1/responses', {'input': 'known'})[0] == 200
        assert _post(gateway, '/v1/responses', {'input': 'unknown'})[0] == 200
        state = gateway.status()
        assert state['last_settled_request_tokens'] == 25
        assert state['usage_complete'] is False
        assert state['tokens_remaining'] is None
        assert state['estimated_model_requests_remaining'] is None
        assert state['model_request_estimate_basis'] == 'latest_settled_response_usage'


def test_forwarded_budget_snapshot_preserves_unknown_usage(tmp_path):
    upstream = FakeTransport([
        FakeResponse(b'{"usage":{"total_tokens":25}}'),
        FakeResponse(b'{}'),
        FakeResponse(b'{"usage":{"total_tokens":5}}'),
    ])
    with ModelGateway('https://provider.invalid/v1', 'secret', 'm', 200, 60,
                      tmp_path / 'forwarded-unknown.jsonl', transport=upstream,
                      strict_total_enforcement=False) as gateway:
        for text in ('known', 'unknown', 'after unknown'):
            assert _post(gateway, '/v1/responses', {'input': text})[0] == 200
    instruction = upstream.calls[2][3]['instructions']
    snapshot = json.loads(instruction.split(
        'Host model budget snapshot before this request: ', 1)[1].splitlines()[0])
    assert snapshot['token_limit'] == 200
    assert snapshot['tokens_reported'] == 25
    assert snapshot['tokens_remaining'] is None
    assert snapshot['usage_complete'] is False
    assert snapshot['unknown_usage_requests'] == 1
    assert snapshot['last_settled_request_tokens'] == 25
    assert snapshot['estimated_model_requests_remaining'] is None
    assert 'not permission to assume unlimited resources' in instruction


@pytest.mark.parametrize('protocol,usage,expected', [
    ('responses', {'input_tokens': 20, 'output_tokens': 3}, {'input_tokens': 20, 'output_tokens': 3}),
    ('responses', {'input_tokens': 20, 'input_tokens_details': {'cached_tokens': 30}}, {'input_tokens': 20}),
    ('anthropic', {'input_tokens': 10, 'cache_read_input_tokens': 50,
                   'cache_creation_input_tokens': 5, 'output_tokens': 3},
     dict(input_tokens=65, cached_input_tokens=50, uncached_input_tokens=15, output_tokens=3)),
])
def test_provider_usage_components(protocol, usage, expected):
    from coding4game_gui.benchmark.code.model_gateway import _usage_components
    assert _usage_components({'usage': usage}, protocol) == expected


def test_unlimited_usage_has_finite_transport_timeout_and_explicit_nulls(tmp_path):
    upstream = FakeTransport([FakeResponse(b'{"usage":{"total_tokens":9999999999}}') for _ in range(2)])
    with ModelGateway('https://provider.invalid/v1', 'secret', 'm', None, None,
                      tmp_path / 'unlimited.jsonl', transport=upstream,
                      strict_total_enforcement=False) as gateway:
        gateway._started_at -= 1000000000
        for _ in range(2):
            assert _post(gateway, '/v1/responses', {'input': 'hello'})[0] == 200
        state = gateway.status()
        assert state['tokens_reported'] == 19999999998
        assert state['token_limit'] is None
        assert state['tokens_remaining'] is None
        assert state['last_settled_request_tokens'] == 9999999999
        assert state['estimated_model_requests_remaining'] is None
        assert state['deadline_remaining_seconds'] is None
        assert not state['revoked']
        assert 'unlimited' in state['budget_message']
        json.dumps(state, allow_nan=False)
        gateway.revoke('submission_selected')
        assert _post(gateway, '/v1/responses', {'input': 'stop'})[0] == 403
    assert all(0 < call[-1] <= 1800 for call in upstream.calls)


def test_unlimited_missing_usage_warns_and_continues(tmp_path):
    upstream = FakeTransport([FakeResponse(b'{}') for _ in range(3)] +
                             [FakeResponse(b'{"usage":{"total_tokens":17}}')])
    with ModelGateway('https://provider.invalid/v1', 'secret', 'm', None, None,
                      tmp_path / 'unknown.jsonl', transport=upstream,
                      strict_total_enforcement=False) as gateway:
        for _ in range(4):
            assert _post(gateway, '/v1/responses', {'input': 'hello'})[0] == 200
        state = gateway.status()
        assert state['stop_reason'] is None and not state['revoked']
        assert state['unknown_usage_requests'] == 3
        assert state['tokens_reported'] == 17 and not state['usage_complete']
        assert state['tokens_remaining'] is None
        assert state['unknown_usage_tolerance'] is None
        assert 'exact total cost is unknown' in state['budget_message']
        gateway.revoke('submission_selected')
        assert _post(gateway, '/v1/responses', {'input': 'stop'})[0] == 403
    assert len(upstream.calls) == 4
    with pytest.raises(ValueError, match='unlimited tokens'):
        ModelGateway('https://provider.invalid/v1', 'secret', 'm', None, None, tmp_path / 'strict')


def test_unlimited_unknown_usage_still_enforces_deadline(tmp_path):
    upstream = FakeTransport([FakeResponse(b'{}')])
    with ModelGateway('https://provider.invalid/v1', 'secret', 'm', None, 60,
                      tmp_path/'deadline.jsonl', transport=upstream,
                      strict_total_enforcement=False) as gateway:
        assert _post(gateway, '/v1/responses', {'input':'hello'})[0] == 200
        gateway._started_at -= 61
        assert _post(gateway, '/v1/responses', {'input':'stop'})[0] == 403
        assert gateway.status()['stop_reason'] == 'deadline_exhausted'
    assert len(upstream.calls) == 1


def test_large_finite_deadline_never_overflows_socket_timeout(tmp_path):
    import socket
    def transport(method, url, headers, body, timeout):
        with socket.socket() as sock:
            sock.settimeout(timeout)
        return FakeResponse(b'{"usage":{"total_tokens":1}}')
    with ModelGateway('https://provider.invalid/v1', 'secret', 'm', 10, 1000000000,
                      tmp_path / 'large.jsonl', transport=transport,
                      strict_total_enforcement=False) as gateway:
        assert _post(gateway, '/v1/responses', {'input': 'hello'})[0] == 200


def _post(gateway, path, body, *, token=None):
    connection = http.client.HTTPConnection("127.0.0.1", gateway.port, timeout=3)
    credential = token or gateway.token
    headers = {"Authorization": f"Bearer {credential}",
               "Content-Type": "application/json"}
    connection.request("POST", path, json.dumps(body), headers)
    response = connection.getresponse()
    payload = response.read()
    return response.status, dict(response.getheaders()), payload


@pytest.mark.parametrize("protocol,path,request_body,response,later_response", [
    ("responses", "/v1/responses",
     {"input": "work", "instructions": "Original instructions."},
     b'{"usage":{"total_tokens":70},"output":[]}',
     b'{"usage":{"total_tokens":5},"output":[]}'),
    ("anthropic", "/v1/messages",
     {"system": [{"type": "text", "text": "Original instructions."}],
      "messages": [{"role": "user", "content": "work"},
                   {"role": "system", "content":
                    "<system-reminder><total_tokens>15000000 tokens left"
                    "</total_tokens></system-reminder>"}]},
     b'{"usage":{"input_tokens":68,"output_tokens":2},"content":[]}',
     b'{"usage":{"input_tokens":4,"output_tokens":1},"content":[]}'),
])
def test_finite_budget_injects_one_finalization_warning_with_thirty_percent_remaining(
        tmp_path, protocol, path, request_body, response, later_response):
    upstream = FakeTransport([
        FakeResponse(response),
        FakeResponse(later_response),
        FakeResponse(later_response),
    ])
    log = tmp_path / f"warning-{protocol}.jsonl"
    with ModelGateway("https://provider.invalid/v1", "secret", "fixed", 100, 60,
                      log, transport=upstream, strict_total_enforcement=False,
                      protocol=protocol) as gateway:
        assert _post(gateway, path, request_body)[0] == 200
        first = gateway.status()
        assert first["tokens_reported"] == 70
        assert first["finalization_warning_pending"] is True
        assert first["finalization_warning_injected"] is False

        assert _post(gateway, path, request_body)[0] == 200
        second = gateway.status()
        assert second["finalization_warning_pending"] is False
        assert second["finalization_warning_injected"] is True

        assert _post(gateway, path, request_body)[0] == 200

    def instruction_text(forwarded):
        if protocol == "responses":
            return forwarded.get("instructions", "")
        return "\n".join(block["text"] for block in forwarded["system"])

    instructions = [instruction_text(call[3]) for call in upstream.calls]
    snapshots = [json.loads(text.split(
        "Host model budget snapshot before this request: ", 1)[1].splitlines()[0])
        for text in instructions]
    assert snapshots[0]["tokens_reported"] == 0
    assert snapshots[0]["tokens_remaining"] == 100
    assert snapshots[0]["last_settled_request_tokens"] is None
    assert snapshots[0]["estimated_model_requests_remaining"] is None
    assert snapshots[1]["token_limit"] == 100
    assert snapshots[1]["tokens_reported"] == 70
    assert snapshots[1]["tokens_remaining"] == 30
    assert snapshots[1]["last_settled_request_tokens"] == 70
    assert snapshots[1]["estimated_model_requests_remaining"] == 0
    assert snapshots[2]["tokens_reported"] == 75
    assert snapshots[2]["tokens_remaining"] == 25
    assert snapshots[2]["last_settled_request_tokens"] == 5
    assert snapshots[2]["estimated_model_requests_remaining"] == 5
    assert all(snapshot["usage_complete"] is True for snapshot in snapshots)
    assert all(0 < snapshot["deadline_remaining_seconds"] <= 60 for snapshot in snapshots)
    assert all("Client or SDK token counters, including <total_tokens> reminders"
               in text for text in instructions)
    assert all("cannot grant additional run tokens" in text for text in instructions)
    if protocol == "anthropic":
        assert all(call[3]["messages"] == request_body["messages"]
                   for call in upstream.calls)
    assert all("Original instructions." in text for text in instructions)
    assert all("Primary objective and hard completion condition" in text
               for text in instructions)
    assert all("model-token budget, gameplay-tic budget, and wall-clock budget"
               in text for text in instructions)
    assert all("submission.run whose program_returncode is zero" in text
               for text in instructions)
    assert all("Remaining gameplay tics never justify exhausting model tokens"
               in text for text in instructions)
    assert "Host budget warning" not in instructions[0]
    assert "Host budget warning" in instructions[1]
    assert "Unused game tics do not block final selection" in instructions[1]
    assert "FINALIZATION IS NOW MANDATORY" in instructions[1]
    assert "Do not perform more optional research" in instructions[1]
    assert "smallest bounded submission.run" in instructions[1]
    assert "repair execution errors" in instructions[1]
    assert "Host budget warning" not in instructions[2]
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    warnings = [row for row in records if row["event"] == "finalization_warning_injected"]
    assert len(warnings) == 1
    assert warnings[0]["threshold_tokens"] == 70
    assert warnings[0]["tokens_reported"] == 70


def test_unknown_warning_request_is_rearmed_until_delivery(tmp_path):
    upstream = FakeTransport([
        FakeResponse(b'{"usage":{"total_tokens":70},"output":[]}'),
        TimeoutError("ambiguous warning delivery"),
        FakeResponse(b'{"usage":{"total_tokens":5},"output":[]}'),
    ])
    log = tmp_path / "warning-rearm.jsonl"
    with ModelGateway("https://provider.invalid/v1", "secret", "fixed", 100, 60,
                      log, transport=upstream, strict_total_enforcement=False) as gateway:
        assert _post(gateway, "/v1/responses", {"input": "work"})[0] == 200
        assert _post(gateway, "/v1/responses", {"input": "finalize"})[0] == 502
        state = gateway.status()
        assert state["finalization_warning_injected"] is False
        assert state["finalization_warning_pending"] is True
        assert _post(gateway, "/v1/responses", {"input": "retry"})[0] == 200
        assert gateway.status()["finalization_warning_injected"] is True

    assert "Host budget warning" not in upstream.calls[0][3].get("instructions", "")
    assert "Host budget warning" in upstream.calls[1][3]["instructions"]
    assert "Host budget warning" in upstream.calls[2][3]["instructions"]
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [row["reason"] for row in records
            if row["event"] == "finalization_warning_rearmed"] == [
                "upstream_outcome_unknown"]


def test_gateway_authenticates_fixes_provider_and_accounts_certified_tokens(tmp_path):
    upstream = FakeTransport([FakeResponse(json.dumps({
        "id": "resp_fake", "usage": {"total_tokens": 9}, "output": []
    }).encode())])
    log = tmp_path / "gateway.jsonl"
    with ModelGateway("https://provider.invalid/api/v1", "host-secret", "fixed-model",
                      token_limit=20, deadline=60, log_path=log,
                      transport=upstream, input_token_bound=lambda _request: 4) as gateway:
        status, _headers, payload = _post(gateway, "/v1/responses", {
            "model": "sandbox-choice", "input": "hello", "max_output_tokens": 10,
        })
        assert status == 200 and json.loads(payload)["id"] == "resp_fake"
        assert gateway.status()["tokens_used"] == 9
        assert gateway.status()["strict_total_enforcement"] is True
        assert gateway.model == "fixed-model"

    method, url, headers, request, _timeout = upstream.calls[0]
    assert (method, url, request["model"]) == (
        "POST", "https://provider.invalid/api/v1/responses", "fixed-model")
    assert headers["Authorization"] == "Bearer host-secret"
    assert all(gateway.token not in value for value in headers.values())
    audit = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert audit[0]["request"]["input"] == "hello"
    assert "host-secret" not in log.read_text(encoding="utf-8")
    assert gateway.token not in log.read_text(encoding="utf-8")


def test_anthropic_gateway_maps_adaptive_thinking_for_non_claude_model(tmp_path):
    upstream = FakeTransport([FakeResponse(
        b'{"usage":{"input_tokens":3,"output_tokens":2},"content":[]}')])
    with ModelGateway("https://provider.invalid/v1", "secret", "qwen3.8-flash",
                      100, 60, tmp_path / "gateway.jsonl", transport=upstream,
                      strict_total_enforcement=False, protocol="anthropic") as gateway:
        status, _headers, _payload = _post(gateway, "/v1/messages", {
            "model": "sandbox-choice",
            "messages": [{"role": "user", "content": "hello"}],
            "thinking": {"type": "adaptive"},
        })
        assert status == 200

    assert upstream.calls[0][3]["thinking"] == {"type": "auto"}


def test_anthropic_gateway_preserves_native_claude_adaptive_thinking(tmp_path):
    upstream = FakeTransport([FakeResponse(
        b'{"usage":{"input_tokens":3,"output_tokens":2},"content":[]}')])
    with ModelGateway("https://provider.invalid/v1", "secret", "claude-opus-5",
                      100, 60, tmp_path / "gateway.jsonl", transport=upstream,
                      strict_total_enforcement=False, protocol="anthropic") as gateway:
        status, _headers, _payload = _post(gateway, "/v1/messages", {
            "messages": [{"role": "user", "content": "hello"}],
            "thinking": {"type": "adaptive"},
        })
        assert status == 200

    assert upstream.calls[0][3]["thinking"] == {"type": "adaptive"}


def test_responses_gateway_drops_unsupported_glm_reasoning_summary(tmp_path):
    upstream = FakeTransport([FakeResponse(
        b'{"usage":{"input_tokens":3,"output_tokens":2},"output":[]}')])
    with ModelGateway("https://provider.invalid/v1", "secret", "glm-5.3-flash",
                      100, 60, tmp_path / "gateway.jsonl", transport=upstream,
                      strict_total_enforcement=False) as gateway:
        status, _headers, _payload = _post(gateway, "/v1/responses", {
            "input": "hello",
            "reasoning": {"effort": "medium", "summary": "auto"},
            "tools": [
                {"type": "web_search", "external_web_access": True},
                {"type": "function", "name": "game_step"},
                {"type": "namespace", "name": "mcp__c4g_game", "tools": []},
            ],
        })
        assert status == 200

    assert upstream.calls[0][3]["reasoning"] == {"effort": "medium"}
    assert upstream.calls[0][3]["tools"] == [
        {"type": "function", "name": "game_step"},
        {"type": "namespace", "name": "mcp__c4g_game", "tools": []},
    ]
    assert "If apply_patch is absent from the actual tool list" in (
        upstream.calls[0][3]["instructions"])
    assert "do not invoke it through a nested shell" in (
        upstream.calls[0][3]["instructions"])


def test_responses_gateway_preserves_reasoning_summary_for_other_models(tmp_path):
    upstream = FakeTransport([FakeResponse(
        b'{"usage":{"input_tokens":3,"output_tokens":2},"output":[]}')])
    with ModelGateway("https://provider.invalid/v1", "secret", "qwen3.8-flash",
                      100, 60, tmp_path / "gateway.jsonl", transport=upstream,
                      strict_total_enforcement=False) as gateway:
        status, _headers, _payload = _post(gateway, "/v1/responses", {
            "input": "hello",
            "reasoning": {"effort": "medium", "summary": "auto"},
            "tools": [{"type": "web_search", "external_web_access": True}],
        })
        assert status == 200

    assert upstream.calls[0][3]["reasoning"] == {
        "effort": "medium", "summary": "auto"}
    assert upstream.calls[0][3]["tools"] == [
        {"type": "web_search", "external_web_access": True}]


@pytest.mark.parametrize("last_usage", [7, 12])
def test_usage_mode_multiturn_settles_then_stops_at_threshold(tmp_path, last_usage):
    upstream = FakeTransport([FakeResponse(json.dumps({"usage": {"total_tokens": n}}).encode())
                              for n in (8, 5, last_usage)])
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 20, 60,
                      tmp_path / "usage.jsonl", transport=upstream,
                      strict_total_enforcement=False) as gateway:
        for expected in (8, 13, 13 + last_usage):
            assert _post(gateway, "/v1/responses", {"input": "hello"})[0] == 200
            state = gateway.status()
            assert state["tokens_used"] == state["tokens_reported"] == expected
            assert state["revoked"] is (expected >= 20)
            assert state["token_accounting"] == "usage"
            assert state["tokens_overshoot"] == max(0, expected - 20)
        status, _, body = _post(gateway, "/v1/responses", {"input": "stop"})
        assert status == 403
        error = json.loads(body)['error']
        assert error['code'] == 'token_budget_exhausted'
        assert str(13 + last_usage) in error['message'] and '20' in error['message']
        gateway.revoke()  # Cleanup must not hide the original reason.
        assert gateway.status()['stop_reason'] == 'token_budget_exhausted'
    assert len(upstream.calls) == 3
    assert all(call[1].endswith("/v1/responses") for call in upstream.calls)


@pytest.mark.parametrize("bad_usage", [None, True, -1, "7"])
def test_usage_mode_unknown_is_not_fabricated_as_zero_or_reservation(tmp_path, bad_usage):
    upstream = FakeTransport([FakeResponse(b'{"usage":{"total_tokens":8}}'),
                              FakeResponse(json.dumps({"usage": {"total_tokens": bad_usage}}).encode()),
                              FakeResponse(b'{"usage":{"total_tokens":9}}'),
                              FakeResponse(b'{}'),
                              FakeResponse(b'{"usage":{"total_tokens":10}}')])
    log = tmp_path / "unknown.jsonl"
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 200, 60,
                      log, transport=upstream, strict_total_enforcement=False) as gateway:
        assert _post(gateway, "/v1/responses", {"input": "first"})[0] == 200
        assert _post(gateway, "/v1/responses", {"input": "second"})[0] == 200
        state = gateway.status()
        assert state["tokens_used"] == state["tokens_reported"] == 8
        assert state["usage_unknown"] and not state["revoked"]
        assert state["unknown_usage_requests"] == 1
        assert state["tokens_remaining"] is None
        assert not state["request_in_flight"]
        assert _post(gateway, "/v1/responses", {"input": "recover"})[0] == 200
        assert gateway.status()['tokens_reported'] == 17
        assert gateway.status()['tokens_remaining'] is None
        assert _post(gateway, "/v1/responses", {"input": "unknown again"})[0] == 200
        state = gateway.status()
        assert state['unknown_usage_requests'] == 2
        assert not state['revoked'] and state['stop_reason'] is None
        assert state['unknown_usage_tolerance'] is None
        assert _post(gateway, "/v1/responses", {"input": "recover again"})[0] == 200
        assert gateway.status()['tokens_reported'] == 27
    records = [r for r in map(json.loads, log.read_text().splitlines()) if r["event"] == "usage"]
    assert len(records) == 5
    assert [r['charged_tokens'] for r in records if r['usage'] == 'unknown'] == [None, None]
    assert len(upstream.calls) == 5


def test_usage_mode_compaction_needs_no_artificial_output_reservation(tmp_path):
    upstream = FakeTransport([FakeResponse(b'{"usage":{"total_tokens":7}}')])
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 20, 60,
                      tmp_path / "compact.jsonl", transport=upstream,
                      strict_total_enforcement=False) as gateway:
        assert _post(gateway, "/v1/responses/compact", {"input": "long"})[0] == 200
        assert gateway.status()["tokens_used"] == 7
    assert "max_output_tokens" not in upstream.calls[0][3]


def test_usage_recovery_does_not_bypass_deadline(tmp_path):
    upstream = FakeTransport([FakeResponse(b'{}')])
    with ModelGateway('https://provider.invalid/v1', 'secret', 'm', 100, 60,
                      tmp_path/'deadline.jsonl', transport=upstream,
                      strict_total_enforcement=False) as gateway:
        _post(gateway, '/v1/responses', {'input':'first'})
        assert not gateway.status()['revoked']
        gateway._started_at = time.monotonic() - 61
        status, _, body = _post(gateway, '/v1/responses', {'input':'late'})
        assert status == 403
        assert json.loads(body)['error']['code'] == 'deadline_exhausted'
        assert '60' in json.loads(body)['error']['message']
        gateway.revoke()
        assert gateway.status()['stop_reason'] == 'deadline_exhausted'
    assert len(upstream.calls) == 1


def test_known_budget_threshold_still_binds_after_unknown_usage(tmp_path):
    upstream = FakeTransport([FakeResponse(b'{}'), FakeResponse(b'{"usage":{"total_tokens":25}}')])
    with ModelGateway('https://provider.invalid/v1','secret','m',20,60,
                      tmp_path/'recovery.jsonl',transport=upstream,strict_total_enforcement=False) as gateway:
        for _ in range(2):
            assert _post(gateway,'/v1/responses',{'input':'x'})[0] == 200
        status, _, body = _post(gateway,'/v1/responses',{'input':'stop'})
        assert status == 403
        assert json.loads(body)['error']['code'] == 'token_budget_exhausted'
        state = gateway.status()
        assert state['tokens_reported'] == 25 and state['tokens_remaining'] is None
        assert state['unknown_usage_requests'] == 1
        assert '25 / 20' in state['budget_message'] and 'unknown' in state['budget_message']
    assert len(upstream.calls) == 2


def test_real_failure_pattern_sse_eof_without_final_event_can_recover(tmp_path):
    partial = b'data: {"type":"response.in_progress","response":{"status":"in_progress"}}\n\n'
    upstream = FakeTransport([FakeResponse(b'',headers={'content-type':'text/event-stream'},chunks=[partial]),
                              FakeResponse(b'{"usage":{"total_tokens":9}}')])
    with ModelGateway('https://provider.invalid/v1','secret','m',100,60,
                      tmp_path/'eof.jsonl',transport=upstream,strict_total_enforcement=False) as gateway:
        assert _post(gateway,'/v1/responses',{'input':'x'})[2] == partial
        assert _post(gateway,'/v1/responses',{'input':'retry'})[0] == 200
        assert not gateway.status()['revoked']
        assert gateway.status()['usage_unknown']
        assert gateway.status()['tokens_reported'] == 9
    assert len(upstream.calls) == 2


def test_usage_mode_inflight_is_visible_and_concurrent_calls_stop_after_settlement(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class BlockingTransport(FakeTransport):
        def __call__(self, *args):
            entered.set()
            assert release.wait(3)
            return super().__call__(*args)
    upstream = BlockingTransport([FakeResponse(b'{"usage":{"total_tokens":25}}')])
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 20, 60,
                      tmp_path / "concurrent.jsonl", transport=upstream,
                      strict_total_enforcement=False) as gateway:
        results = []
        workers = [threading.Thread(target=lambda: results.append(
            _post(gateway, "/v1/responses", {"input": "x"})[0])) for _ in range(2)]
        workers[0].start()
        try:
            assert entered.wait(2)
            state = gateway.status()
            assert state["request_in_flight"] and state["tokens_reported"] == 0
            workers[1].start()
        finally:
            release.set()
            for worker in workers:
                if worker.ident is not None:
                    worker.join(3)
        assert sorted(results) == [200, 403]
        assert not gateway.status()["request_in_flight"]
    assert len(upstream.calls) == 1


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("terminal", ["response.completed", "response.incomplete"])
def test_usage_mode_streaming_usage_is_settled_once(tmp_path, newline, terminal):
    event = {"type": terminal, "response": {"usage": {"input_tokens": 8, "output_tokens": 2}}}
    raw = b"data: " + json.dumps(event).encode() + newline * 2
    # A duplicate terminal event must not double charge a single request.
    raw += raw
    upstream = FakeTransport([FakeResponse(b"", headers={"content-type": "text/event-stream"},
                                           chunks=[bytes([n]) for n in raw])])
    log = tmp_path / "sse.jsonl"
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 100, 60,
                      log, transport=upstream, strict_total_enforcement=False) as gateway:
        assert _post(gateway, "/v1/responses", {"input": "x", "stream": True})[2] == raw
        state = gateway.status()
        assert state["tokens_used"] == state["tokens_reported"] == 10
        assert not state["request_in_flight"] and not state["revoked"]
    usage = [r for r in map(json.loads, log.read_text().splitlines()) if r["event"] == "usage"]
    assert len(usage) == 1


def test_usage_mode_interrupted_stream_and_transport_pause_without_false_charge(tmp_path):
    class Interrupted(FakeResponse):
        def iter_bytes(self):
            # Nonterminal usage is not a final bill, even if it is an integer.
            yield b'data: {"type":"response.created","response":{"usage":{"total_tokens":3}}}\n\n'
            raise OSError("synthetic stream interruption")
    for response in (Interrupted(b"", headers={"content-type": "text/event-stream"}),
                     TimeoutError("ambiguous provider acceptance")):
        upstream = FakeTransport([response, FakeResponse(b'{"usage":{"total_tokens":7}}')])
        with ModelGateway("https://provider.invalid/v1", "secret", "m", 100, 60,
                          tmp_path / "interrupted.jsonl", transport=upstream,
                          strict_total_enforcement=False) as gateway:
            _post(gateway, "/v1/responses", {"input": "x"})
            state = gateway.status()
            assert state["tokens_reported"] == 0  # Confirmed subtotal, not a claim of zero cost.
            assert state["usage_unknown"] and state["unknown_usage_requests"] == 1
            assert state["tokens_remaining"] is None and not state["revoked"]
            assert not state["request_in_flight"]
            assert len(upstream.calls) == 1  # No new host retry of an ambiguous request.
            assert _post(gateway, "/v1/responses", {"input": "recover"})[0] == 200
            assert gateway.status()['tokens_reported'] == 7
        assert len(upstream.calls) == 2


def test_usage_mode_repeated_ambiguous_transport_failures_remain_recoverable(tmp_path):
    upstream = FakeTransport([TimeoutError("possibly accepted first"),
                              TimeoutError("possibly accepted second"),
                              FakeResponse(b'{"usage":{"total_tokens":7}}')])
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 100, 60,
                      tmp_path / "repeated-timeout.jsonl", transport=upstream,
                      strict_total_enforcement=False) as gateway:
        for expected in (1, 2):
            status, _, body = _post(gateway, "/v1/responses", {"input": "retryable"})
            assert status == 502
            assert json.loads(body)['error']['code'] == 'upstream_usage_unknown'
            state = gateway.status()
            assert state['unknown_usage_requests'] == expected
            assert not state['revoked'] and state['stop_reason'] is None
        assert _post(gateway, "/v1/responses", {"input": "recovered"})[0] == 200
        assert gateway.status()['tokens_reported'] == 7
    assert len(upstream.calls) == 3


def test_usage_mode_cleanup_failure_cannot_duplicate_or_hide_known_bill(tmp_path):
    class BrokenCleanup(FakeResponse):
        def close(self):
            raise OSError("synthetic cleanup failure")
    upstream = FakeTransport([BrokenCleanup(b'{"usage":{"total_tokens":12}}'),
                              FakeResponse(b'{"usage":{"total_tokens":9}}')])
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 100, 60,
                      tmp_path / "cleanup_usage.jsonl", transport=upstream,
                      strict_total_enforcement=False) as gateway:
        for _ in range(2):
            assert _post(gateway, "/v1/responses", {"input": "x"})[0] == 200
        assert gateway.status()["tokens_reported"] == 21
        assert not gateway.status()["usage_unknown"]


def test_usage_mode_certified_not_sent_failures_have_known_zero_usage(tmp_path):
    upstream = FakeTransport([RetryableBeforeSendError("not sent"),
                              RetryableBeforeSendError("not sent")])
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 100, 60,
                      tmp_path / "not_sent.jsonl", transport=upstream,
                      strict_total_enforcement=False) as gateway:
        assert _post(gateway, "/v1/responses", {"input": "x"})[0] == 502
        state = gateway.status()
        assert not state["usage_unknown"] and not state["request_in_flight"]
        assert state["tokens_remaining"] == 100
    assert len(upstream.calls) == 2


def test_gateway_rejects_wrong_auth_routes_remote_inputs_and_unbounded_tokens(tmp_path):
    upstream = FakeTransport([])
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 20, 60,
                      tmp_path / "audit.jsonl", transport=upstream) as gateway:
        assert _post(gateway, "/v1/responses", {"input": "x", "max_output_tokens": 1},
                     token="wrong")[0] == 401
        assert _post(gateway, "/v1/chat/completions", {})[0] == 404
        status, _, body = _post(gateway, "/v1/responses", {
            "input": [{"type": "input_image", "image_url": "https://attacker.invalid/x"}],
            "max_output_tokens": 1,
        })
        assert status == 400 and json.loads(body)["error"]["code"] == "remote_reference_forbidden"
        status, _, body = _post(gateway, "/v1/responses", {
            "input": "x", "max_output_tokens": 1,
        })
        assert status == 503 and json.loads(body)["error"]["code"] == "budget_bound_unavailable"
    assert not upstream.calls


def test_gateway_forwards_compaction_and_streams_sse(tmp_path):
    chunks = [b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n',
              b'data: {"type":"response.completed","response":{"usage":{"total_tokens":7}}}\n\n']
    upstream = FakeTransport([FakeResponse(b"", headers={"content-type": "text/event-stream"},
                                           chunks=chunks)])
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 30, 60,
                      tmp_path / "audit.jsonl", transport=upstream,
                      input_token_bound=lambda _request: 3,
                      strict_total_enforcement=False) as gateway:
        status, headers, body = _post(gateway, "/v1/responses/compact", {"input": "long"})
        assert status == 200
        assert headers["Content-Type"] == "text/event-stream"
        assert body == b"".join(chunks)
        assert gateway.status()["tokens_used"] == 7
    assert upstream.calls[0][1] == "https://provider.invalid/v1/responses/compact"


def test_codex_shaped_request_gets_server_owned_output_cap_before_count_and_send(tmp_path):
    upstream = FakeTransport([
        FakeResponse(b'{"object":"response.input_tokens","input_tokens":3}'),
        FakeResponse(b'{"usage":{"total_tokens":7},"output":[]}'),
    ])
    with ModelGateway("https://provider.invalid/v1", "secret", "fixed", 5000, 60,
                      tmp_path / "audit.jsonl", transport=upstream,
                      count_input=True) as gateway:
        assert _post(gateway, "/v1/responses", {
            "input": [{"role": "user", "content": "hello"}], "stream": True,
        })[0] == 200
        assert gateway.status()["default_max_output_tokens"] == 4096
    count_request = upstream.calls[0][3]
    sent_request = upstream.calls[1][3]
    assert "max_output_tokens" not in count_request
    assert sent_request["max_output_tokens"] == 4096


def test_server_owned_output_cap_rejects_when_remaining_budget_is_too_small(tmp_path):
    upstream = FakeTransport([])
    with ModelGateway("https://provider.invalid/v1", "secret", "fixed", 100, 60,
                      tmp_path / "audit.jsonl", transport=upstream,
                      input_token_bound=lambda _request: 1,
                      default_max_output_tokens=100) as gateway:
        status, _, body = _post(gateway, "/v1/responses", {"input": "hello"})
        assert status == 429
        assert json.loads(body)["error"]["code"] == "token_budget_exhausted"
    assert not upstream.calls


def test_strict_mode_rejects_compaction_because_api_has_no_output_cap(tmp_path):
    upstream = FakeTransport([])
    with ModelGateway("https://provider.invalid/v1", "secret", "fixed", 100, 60,
                      tmp_path / "audit.jsonl", transport=upstream,
                      input_token_bound=lambda _request: 1) as gateway:
        status, _, body = _post(gateway, "/v1/responses/compact", {"input": "hello"})
        assert status == 503
        assert json.loads(body)["error"]["code"] == "budget_bound_unavailable"
    assert not upstream.calls


def test_sse_first_event_is_forwarded_before_upstream_finishes(tmp_path):
    first = b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
    second = b'data: {"type":"response.completed","response":{"usage":{"total_tokens":2}}}\n\n'
    yielded = threading.Event()
    release = threading.Event()

    class BlockingSSE(FakeResponse):
        def iter_bytes(self):
            yield first
            yielded.set()
            assert release.wait(2)
            yield second

    upstream = FakeTransport([BlockingSSE(
        b"", headers={"content-type": "text/event-stream"})])
    with ModelGateway("https://provider.invalid/v1", "secret", "fixed", 10, 60,
                      tmp_path / "audit.jsonl", transport=upstream,
                      input_token_bound=lambda _request: 1) as gateway:
        connection = http.client.HTTPConnection("127.0.0.1", gateway.port, timeout=2)
        connection.request("POST", "/v1/responses", json.dumps({
            "input": "hello", "max_output_tokens": 3,
        }), {"Authorization": f"Bearer {gateway.token}",
             "Content-Type": "application/json"})
        response = connection.getresponse()
        assert yielded.wait(1)
        assert response.read(len(first)) == first
        release.set()
        assert response.read() == second


def test_unknown_usage_retires_reservation_and_revokes(tmp_path):
    upstream = FakeTransport([FakeResponse(b'{"id":"no-usage"}')])
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 10, 60,
                      tmp_path / "audit.jsonl", transport=upstream,
                      input_token_bound=lambda _request: 2) as gateway:
        assert _post(gateway, "/v1/responses", {
            "input": "x", "max_output_tokens": 8,
        })[0] == 200
        status = gateway.status()
        assert status["usage_unknown"] is True
        assert status["revoked"] is True
        assert status["tokens_used"] == 10
        assert _post(gateway, "/v1/responses", {
            "input": "again", "max_output_tokens": 1,
        })[0] == 403


def test_large_inline_image_history_is_forwarded_and_accounted_once(tmp_path):
    upstream = FakeTransport([FakeResponse(
        b'{"usage":{"input_tokens":4,"output_tokens":2},"output":[]}')])
    image = "data:image/png;base64," + "A" * (17 * 1024 * 1024)
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 10, 60,
                      tmp_path / "audit.jsonl", transport=upstream,
                      input_token_bound=lambda _request: 2) as gateway:
        request = {"input": [{"role": "user", "content": [
            {"type": "input_image", "image_url": image}]}],
            "max_output_tokens": 5}
        assert _post(gateway, "/v1/responses", request)[0] == 200
        status = gateway.status()
        assert status["request_byte_limit"] == 64 * 1024 * 1024
        assert status["tokens_reported"] == status["tokens_used"] == 6
        assert status["unknown_usage_requests"] == 0
        assert _post(gateway, "/v1/responses", {
            "input": "next", "max_output_tokens": 5})[0] == 429
    assert len(upstream.calls) == 1
    assert upstream.calls[0][3]["input"][0]["content"][0]["image_url"] == image


def test_default_request_byte_limit_rejects_before_read_or_upstream(tmp_path):
    upstream = FakeTransport([])
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 50, 60,
                      tmp_path / "audit.jsonl", transport=upstream,
                      input_token_bound=lambda _request: 1) as gateway:
        connection = http.client.HTTPConnection("127.0.0.1", gateway.port, timeout=3)
        connection.request("POST", "/v1/responses", headers={
            "Authorization": f"Bearer {gateway.token}",
            "Content-Length": str(64 * 1024 * 1024 + 1)})
        response = connection.getresponse()
        assert response.status == 413
        assert json.loads(response.read())["error"]["code"] == "request_too_large"
        connection.close()
        assert gateway.status()["tokens_used"] == 0
    assert not upstream.calls


@pytest.mark.parametrize("model,limit,message", [
    ("m" * 128, 64, "normalized request exceeds byte limit"),
    ("m", 512, "host budget instruction would exceed request byte limit"),
])
def test_request_expansion_remains_subject_to_explicit_byte_limit(
        tmp_path, model, limit, message):
    upstream = FakeTransport([])
    with ModelGateway("https://provider.invalid/v1", "secret", model, 50, 60,
                      tmp_path / "audit.jsonl", transport=upstream,
                      input_token_bound=lambda _request: 1,
                      max_request_bytes=limit) as gateway:
        status, _headers, body = _post(gateway, "/v1/responses", {
            "input": "hello", "max_output_tokens": 1})
        assert status == 413
        assert json.loads(body)["error"]["message"] == message
        assert gateway.status()["tokens_used"] == 0
    assert not upstream.calls


def test_oversized_request_and_explicit_revoke_fail_before_upstream(tmp_path):
    upstream = FakeTransport([])
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 50, 60,
                      tmp_path / "audit.jsonl", transport=upstream,
                      input_token_bound=lambda _request: 1,
                      max_request_bytes=64) as gateway:
        assert _post(gateway, "/v1/responses", {
            "input": "x" * 200, "max_output_tokens": 1,
        })[0] == 413
        gateway.revoke()
        assert _post(gateway, "/v1/responses", {
            "input": "x", "max_output_tokens": 1,
        })[0] == 403
    assert not upstream.calls


def test_concurrent_calls_cannot_overcommit_prospective_reservations(tmp_path):
    class BlockingTransport(FakeTransport):
        def __init__(self):
            super().__init__([FakeResponse(
                b'{"usage":{"total_tokens":6},"output":[]}')])
            self.entered = threading.Event()
            self.release = threading.Event()

        def __call__(self, *args):
            self.entered.set()
            assert self.release.wait(2)
            return super().__call__(*args)

    upstream = BlockingTransport()
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 10, 60,
                      tmp_path / "audit.jsonl", transport=upstream,
                      input_token_bound=lambda _request: 2) as gateway:
        results = []
        first = threading.Thread(target=lambda: results.append(_post(gateway, "/v1/responses", {
            "input": "first", "max_output_tokens": 5,
        })[0]))
        second = threading.Thread(target=lambda: results.append(_post(gateway, "/v1/responses", {
            "input": "second", "max_output_tokens": 5,
        })[0]))
        first.start()
        assert upstream.entered.wait(2)
        second.start()
        time.sleep(0.05)
        upstream.release.set()
        first.join(2); second.join(2)
        assert sorted(results) == [200, 429]
        assert gateway.status()["tokens_used"] == 6
    assert len(upstream.calls) == 1


def test_retry_attempts_are_audited_without_credentials(tmp_path):
    log = tmp_path / "audit.jsonl"
    upstream = FakeTransport([RetryableBeforeSendError("temporary"), FakeResponse(
        b'{"usage":{"total_tokens":2},"output":[]}')])
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 10, 60, log,
                      transport=upstream, input_token_bound=lambda _request: 1) as gateway:
        assert _post(gateway, "/v1/responses", {
            "input": "retry", "max_output_tokens": 3,
        })[0] == 200
    attempts = [row for row in map(json.loads, log.read_text().splitlines())
                if row["event"] == "attempt"]
    assert [(row["attempt"], row["status"]) for row in attempts] == [
        (1, "error"), (2, "connected")]
    assert "secret" not in log.read_text()


def test_rate_limit_waits_for_retry_after_then_recovers(tmp_path):
    log = tmp_path / "rate_limit.jsonl"
    sleep = FakeSleep()
    first = FakeResponse(
        b'{"error":{"code":"gateway_queue_full"}}', status=429,
        headers={"content-type": "application/json", "Retry-After": "7"},
    )
    upstream = FakeTransport([
        first,
        FakeResponse(b'{"usage":{"total_tokens":2},"output":[]}'),
    ])
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 10, 60, log,
                      transport=upstream, input_token_bound=lambda _request: 1,
                      sleep=sleep) as gateway:
        status, _, body = _post(gateway, "/v1/responses", {
            "input": "retry", "max_output_tokens": 3,
        })
        assert status == 200
        assert json.loads(body)["usage"]["total_tokens"] == 2
        assert gateway.status()["rate_limit_recovery_seconds"] == 180
    assert sleep.calls == [7]
    assert len(upstream.calls) == 2
    records = [json.loads(line) for line in log.read_text().splitlines()]
    backoff = next(row for row in records if row["event"] == "rate_limit_backoff")
    assert backoff["attempt"] == 1
    assert backoff["delay_seconds"] == 7
    assert backoff["source"] == "retry_after"


def test_rate_limit_exponential_backoff_stops_after_recovery_window(tmp_path):
    sleep = FakeSleep()
    errors = [FakeResponse(b'{"error":{"code":"gateway_queue_full"}}', status=429)
              for _ in range(3)]
    upstream = FakeTransport(errors)
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 10, 60,
                      tmp_path / "rate_limit.jsonl", transport=upstream,
                      input_token_bound=lambda _request: 1,
                      rate_limit_recovery_seconds=12, sleep=sleep) as gateway:
        status, _, body = _post(gateway, "/v1/responses", {
            "input": "retry", "max_output_tokens": 3,
        })
        assert status == 429
        assert json.loads(body)["error"]["code"] == "gateway_queue_full"
    assert sleep.calls == [5, 7]
    assert len(upstream.calls) == 3


def test_rate_limit_does_not_violate_retry_after_or_run_deadline(tmp_path):
    for deadline, recovery_seconds in ((60, 3), (2, 180)):
        sleep = FakeSleep()
        upstream = FakeTransport([FakeResponse(
            b'{"error":{"code":"gateway_queue_full"}}', status=429,
            headers={"Retry-After": "5"},
        )])
        with ModelGateway("https://provider.invalid/v1", "secret", "m", 10, deadline,
                          tmp_path / f"bounded-{deadline}.jsonl", transport=upstream,
                          input_token_bound=lambda _request: 1,
                          rate_limit_recovery_seconds=recovery_seconds,
                          sleep=sleep) as gateway:
            assert _post(gateway, "/v1/responses", {
                "input": "retry", "max_output_tokens": 3,
            })[0] == 429
        assert sleep.calls == []
        assert len(upstream.calls) == 1


def test_ambiguous_transport_failure_retires_budget_without_retry(tmp_path):
    upstream = FakeTransport([TimeoutError("possibly accepted"), FakeResponse(b"unused")])
    with ModelGateway("https://provider.invalid/v1", "secret", "m", 10, 60,
                      tmp_path / "audit.jsonl", transport=upstream,
                      input_token_bound=lambda _request: 2) as gateway:
        status, _, body = _post(gateway, "/v1/responses", {
            "input": "x", "max_output_tokens": 8,
        })
        assert status == 502
        assert json.loads(body)["error"]["code"] == "upstream_usage_unknown"
        assert gateway.status()["tokens_used"] == 10
        assert gateway.status()["usage_unknown"] is True
        assert gateway.status()["revoked"] is True
    assert len(upstream.calls) == 1


def test_provider_input_counter_counts_same_fixed_request_before_forwarding(tmp_path):
    upstream = FakeTransport([
        FakeResponse(b'{"object":"response.input_tokens","input_tokens":4}'),
        FakeResponse(b'{"usage":{"total_tokens":9},"output":[]}'),
    ])
    log = tmp_path / "audit.jsonl"
    with ModelGateway("https://provider.invalid/v1", "secret", "fixed", 20, 60, log,
                      transport=upstream, count_input=True) as gateway:
        assert _post(gateway, "/v1/responses", {
            "model": "ignored", "input": "hello", "tools": [{"type": "web_search"}],
            "stream": True, "max_output_tokens": 10,
        })[0] == 200
        assert gateway.status()["input_bound_source"] == "provider_input_tokens_endpoint"
    count_call, response_call = upstream.calls
    assert count_call[1] == "https://provider.invalid/v1/responses/input_tokens"
    assert count_call[3]["model"] == "fixed"
    assert count_call[3]["input"] == "hello"
    assert count_call[3]["tools"] == [{"type": "web_search"}]
    assert "Primary objective and hard completion condition" in count_call[3]["instructions"]
    assert response_call[3]["model"] == "fixed"
    assert response_call[3]["tools"] == count_call[3]["tools"]
    assert response_call[3]["instructions"] == count_call[3]["instructions"]
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(row["event"] == "input_token_count_attempt" and row["input_tokens"] == 4
               for row in records)


@pytest.mark.parametrize("http_status", [404, 405, 501])
def test_provider_input_counter_failure_is_fail_closed(tmp_path, http_status):
    upstream = FakeTransport([FakeResponse(b'private-provider-error-secret', status=http_status)])
    with ModelGateway("https://provider.invalid/v1", "secret", "fixed", 20, 60,
                      tmp_path / "audit.jsonl", transport=upstream,
                      count_input=True) as gateway:
        status, _, body = _post(gateway, "/v1/responses", {
            "input": "hello", "max_output_tokens": 10,
        })
        assert status == 503
        assert json.loads(body)["error"]["code"] == "budget_bound_unavailable"
        assert f"HTTP {http_status}" in json.loads(body)["error"]["message"]
        assert gateway.status()["tokens_used"] == 0
    assert len(upstream.calls) == 1
    assert upstream.calls[0][1].endswith("/responses/input_tokens")
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "private-provider-error-secret" not in audit
    assert "private-provider-error-secret" not in body.decode()
    attempt = next(row for row in map(json.loads, audit.splitlines())
                   if row["event"] == "input_token_count_attempt")
    assert attempt["http_status"] == http_status
    assert attempt["reason"] == "endpoint_unsupported"


def test_provider_input_counter_transient_failure_retains_bounded_retry(tmp_path):
    upstream = FakeTransport([
        FakeResponse(b'private-error', status=503),
        FakeResponse(b'{"input_tokens":4}'),
        FakeResponse(b'{"usage":{"total_tokens":9},"output":[]}'),
    ])
    with ModelGateway("https://provider.invalid/v1", "secret", "fixed", 20, 60,
                      tmp_path / "audit.jsonl", transport=upstream,
                      count_input=True) as gateway:
        assert _post(gateway, "/v1/responses", {
            "input": "hello", "max_output_tokens": 10})[0] == 200
        assert gateway.status()["tokens_used"] == 9
    assert len(upstream.calls) == 3
    assert all(call[1].endswith("/responses/input_tokens") for call in upstream.calls[:2])


def test_provider_input_counter_transport_failure_does_not_leak_exception(tmp_path):
    upstream = FakeTransport([OSError("private-transport-secret"),
                              OSError("private-transport-secret")])
    log = tmp_path / "audit.jsonl"
    with ModelGateway("https://provider.invalid/v1", "secret", "fixed", 20, 60,
                      log, transport=upstream, count_input=True) as gateway:
        status, _, body = _post(gateway, "/v1/responses", {
            "input": "hello", "max_output_tokens": 10})
        assert status == 503
        assert gateway.status()["tokens_used"] == 0
    assert len(upstream.calls) == 2
    assert "transport_error" in json.loads(body)["error"]["message"]
    assert "private-transport-secret" not in body.decode() + log.read_text()


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('known_usage', [False, True])
def test_cleanup_failure_cannot_skip_accounting_or_reopen_budget(
        tmp_path, streaming, known_usage):
    payload = {'id': 'fake', 'output': []}
    if known_usage:
        payload['usage'] = {'total_tokens': 9}
    body = json.dumps(payload).encode()
    if streaming:
        body = b'data: ' + json.dumps({
            'type': 'response.completed', 'response': payload}).encode() + b'\n\n'

    class BrokenCleanup(FakeResponse):
        def close(self):
            raise RuntimeError('synthetic upstream cleanup failure')

    response = BrokenCleanup(body, headers={
        'content-type': 'text/event-stream' if streaming else 'application/json'})
    upstream = FakeTransport([response, response])
    log = tmp_path / 'cleanup.jsonl'
    with ModelGateway('https://provider.invalid/v1', 'fake-key', 'm', 14, 60,
                      log, transport=upstream,
                      input_token_bound=lambda _request: 4) as gateway:
        request = {'input': 'x', 'max_output_tokens': 10}
        assert _post(gateway, '/v1/responses', request)[0] == 200
        # The next request serializes behind cleanup. No sleep or polling can
        # hide an uncharged request: it must be rejected before another send.
        status, _, _ = _post(gateway, '/v1/responses', request)
        assert status == (429 if known_usage else 403)
        state = gateway.status()
        assert state['tokens_used'] == (9 if known_usage else 14)
        assert state['usage_unknown'] is (not known_usage)
        assert state['revoked'] is (not known_usage)
    assert len(upstream.calls) == 1
    records = list(map(json.loads, log.read_text().splitlines()))
    assert len([r for r in records if r['event'] == 'usage']) == 1
    assert any(r['event'] == 'response_cleanup_error' for r in records)


@pytest.mark.parametrize('streaming', [False, True])
def test_accounting_precedes_slow_response_cleanup(tmp_path, streaming):
    entered, release = threading.Event(), threading.Event()
    body = b'{"usage":{"total_tokens":9},"output":[]}'
    if streaming:
        body = b'data: {"type":"response.completed","response":' + body + b'}\n\n'

    class SlowCleanup(FakeResponse):
        def close(self):
            entered.set()
            assert release.wait(3)

    upstream = FakeTransport([SlowCleanup(body, headers={
        'content-type': 'text/event-stream' if streaming else 'application/json'})])
    with ModelGateway('https://provider.invalid/v1', 'fake-key', 'm', 14, 60,
                      tmp_path / 'slow.jsonl', transport=upstream,
                      input_token_bound=lambda _request: 4) as gateway:
        connection = http.client.HTTPConnection('127.0.0.1', gateway.port, timeout=3)
        try:
            connection.request('POST', '/v1/responses', json.dumps({
                'input': 'x', 'max_output_tokens': 10}), {
                    'Authorization': 'Bearer ' + gateway.token,
                    'Content-Type': 'application/json'})
            response = connection.getresponse()
            assert response.read(len(body)) == body
            assert entered.wait(2)
            assert gateway.status()['tokens_used'] == 9
        finally:
            release.set()
            connection.close()


@pytest.mark.parametrize('streaming', [False, True])
def test_client_disconnect_still_charges_once_when_cleanup_also_fails(tmp_path, streaming):
    class DisconnectedWriter:
        def write(self, _data):
            raise BrokenPipeError('synthetic client disconnect')

        def flush(self):
            pass

    class Handler:
        wfile = DisconnectedWriter()

        def send_response(self, *args):
            pass

        def send_header(self, *args):
            pass

        def end_headers(self):
            pass

    class BrokenCleanup(FakeResponse):
        def close(self):
            raise RuntimeError('synthetic cleanup failure')

    body = b'{"usage":{"total_tokens":9},"output":[]}'
    if streaming:
        # Disconnect before a terminal usage event can be consumed.
        body = b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'
    log = tmp_path / 'disconnect.jsonl'
    with ModelGateway('https://provider.invalid/v1', 'fake-key', 'm', 14, 60,
                      log, transport=FakeTransport([])) as gateway:
        gateway._relay(Handler(), BrokenCleanup(body, headers={
            'content-type': 'text/event-stream' if streaming else 'application/json'}),
            14, 'synthetic-request')
        state = gateway.status()
        assert state['tokens_used'] == (14 if streaming else 9)
        assert state['usage_unknown'] is streaming
        assert state['revoked'] is streaming
    records = list(map(json.loads, log.read_text().splitlines()))
    assert len([r for r in records if r['event'] == 'usage']) == 1
