"""Mock the Direct provider transport and its accounting-visible retries."""
from __future__ import annotations

import types

import litellm
import pytest

from coding4game_gui.benchmark.direct import provider


def load_config(tmp_path, yaml_text):
    path = tmp_path / "provider.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    provider._load_config(path)


def test_flag_forced_when_config_omits_it(tmp_path):
    """A config WITHOUT the setting must still route via chat/completions."""
    load_config(tmp_path, "model_list:\n  - model_name: gpt-5.5\n"
               "    litellm_params:\n      model: openai/gpt-5.5\n")
    assert litellm.use_chat_completions_url_for_anthropic_messages is True


def test_flag_set_when_config_requests_it(tmp_path):
    load_config(tmp_path, "litellm_settings:\n"
               "  use_chat_completions_url_for_anthropic_messages: true\n")
    assert litellm.use_chat_completions_url_for_anthropic_messages is True


def test_explicit_opt_out_is_honored(tmp_path):
    """An explicit `false` must still win — escape hatch for real Responses use."""
    load_config(tmp_path, "litellm_settings:\n"
               "  use_chat_completions_url_for_anthropic_messages: false\n")
    assert litellm.use_chat_completions_url_for_anthropic_messages is False


def test_direct_uses_anthropic_provider_route(tmp_path, monkeypatch):
    load_config(
        tmp_path,
        "model_list:\n"
        "  - model_name: claude-test\n"
        "    litellm_params:\n"
        "      model: anthropic/claude-test\n"
        "      api_base: https://example.invalid/claude\n"
        "      api_key: test-key\n"
    )
    seen = {}

    async def fake_acreate(**kwargs):
        seen.update(kwargs)
        return {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ACT 1 MOVE_LEFT"}],
            "model": "claude-test",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 4},
        }

    monkeypatch.setattr(litellm.anthropic_interface.messages, "acreate", fake_acreate)
    response = provider.request_message(
        model="claude-test", system="Return one ACT line.", messages=[], max_tokens=32)

    assert response.content[0].text == "ACT 1 MOVE_LEFT"
    assert response.usage.input_tokens == 12
    assert seen["model"] == "anthropic/claude-test"
    assert seen["api_base"] == "https://example.invalid/claude"
    assert seen["timeout"] == 240.0


def test_none_signature_thinking_block_is_dropped_but_text_and_usage_survive():
    """OpenAI reasoning has no Anthropic signature; it must not kill ACT text."""
    response = provider._to_message({
        "id": "msg_reasoning",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "private", "signature": None},
            {"type": "text", "text": "ACT 8 RIGHT_TRIGGER"},
        ],
        "model": "gpt-5.6-terra",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 321, "output_tokens": 17},
    })

    assert [block.type for block in response.content] == ["text"]
    assert response.content[0].text == "ACT 8 RIGHT_TRIGGER"
    assert response.usage.input_tokens == 321
    assert response.usage.output_tokens == 17


def test_shared_async_transport_retries_past_five_quick_failures(monkeypatch):
    calls = []
    sleeps = []
    events = []

    async def recover_on_sixth_attempt(**kwargs):
        calls.append(kwargs)
        if len(calls) < 6:
            raise RuntimeError(
                "upstream provider error: All eligible accounts are at "
                "per-account concurrency limit")
        return {
            "id": "msg_recovered", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "ok"}], "model": "m",
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    async def no_wait(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(provider.random, "uniform", lambda lo, _hi: lo)
    monkeypatch.setattr(provider.asyncio, "sleep", no_wait)
    monkeypatch.setattr(
        litellm.anthropic_interface.messages, "acreate",
        recover_on_sixth_attempt)

    response = provider.request_message(
        model="retry-test", messages=[], max_tokens=8,
        retry_observer=events.append)

    assert response.content[0].text == "ok"
    assert len(calls) == 6
    assert all(call["num_retries"] == 0 for call in calls)
    assert all(call["timeout"] == 240.0 for call in calls)
    assert sleeps == [0.8, 1.6, 3.2, 6.4, 12.8]
    assert [e["event"] for e in events].count("retry") == 5
    assert all(e["max_attempts"] is None for e in events)
    assert events[-1]["event"] == "success"


def test_shared_transport_uses_wall_clock_retry_window(monkeypatch):
    clock = types.SimpleNamespace(now=100.0)
    calls = []
    events = []

    monkeypatch.setattr(provider.time, "monotonic", lambda: clock.now)
    async def advance(seconds):
        clock.now += seconds

    monkeypatch.setattr(provider.asyncio, "sleep", advance)
    monkeypatch.setattr(
        provider, "_retry_delay", lambda _exc, _n: 60.0)

    async def fail(timeout):
        calls.append(timeout)
        raise RuntimeError("upstream provider error: concurrency limit")

    with pytest.raises(RuntimeError, match="concurrency limit"):
        provider.asyncio.run(provider.request_with_retry_async(
            fail, timeout=240, retry_window_s=120,
            observer=events.append))

    assert len(calls) == 3
    assert calls[0] == pytest.approx(120.0)
    assert calls[-1] == pytest.approx(0.001)
    assert clock.now - 100.0 == pytest.approx(119.999)
    assert events[-1]["event"] == "give_up"
    assert events[-1]["reason"] == "deadline"
    assert events[-1]["max_attempts"] is None


def test_explicit_retry_count_remains_an_optional_safety_cap(monkeypatch):
    calls = []
    events = []
    async def no_wait(_seconds):
        pass

    monkeypatch.setattr(provider.asyncio, "sleep", no_wait)

    async def fail(_timeout):
        calls.append(1)
        raise RuntimeError("connection error")

    with pytest.raises(RuntimeError, match="connection error"):
        provider.asyncio.run(provider.request_with_retry_async(
            fail, timeout=90, max_retries=4, retry_window_s=1200,
            observer=events.append))

    assert len(calls) == 5
    assert events[-1]["event"] == "give_up"
    assert events[-1]["reason"] == "exhausted"
    assert events[-1]["max_attempts"] == 5


def test_shared_transport_does_not_retry_permanent_error(monkeypatch):
    calls = []

    async def fail(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("Missing credentials: set OPENAI_API_KEY")

    monkeypatch.setattr(litellm.anthropic_interface.messages, "acreate", fail)

    with pytest.raises(RuntimeError, match="Missing credentials"):
        provider.request_message(model="auth-test", messages=[], max_tokens=8)
    assert len(calls) == 1


def test_transient_status_can_come_from_exception_response():
    error = RuntimeError("opaque provider failure")
    error.response = types.SimpleNamespace(status_code=429, headers={})
    assert provider.is_transient_error(error) is True
    assert provider.is_transient_error(RuntimeError(
        "All eligible accounts are at per-account concurrency limit")) is True


def test_shared_transport_honors_retry_after(monkeypatch):
    calls = []
    sleeps = []

    class RetryAfterError(RuntimeError):
        headers = {"Retry-After": "7"}

    async def flaky(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RetryAfterError("rate limit")
        return {
            "id": "msg_retry_after", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "ok"}], "model": "m",
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    async def no_wait(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(provider.asyncio, "sleep", no_wait)
    monkeypatch.setattr(litellm.anthropic_interface.messages, "acreate", flaky)
    response = provider.request_message(model="retry-after-test", messages=[], max_tokens=8)

    assert response.content[0].text == "ok"
    assert len(calls) == 2
    assert sleeps == [7.0]


def test_caller_deadline_shortens_default_retry_window(monkeypatch):
    calls = []
    events = []
    clock = types.SimpleNamespace(now=10.0)
    monkeypatch.setattr(provider.time, "monotonic", lambda: clock.now)
    async def advance(seconds):
        clock.now += seconds

    monkeypatch.setattr(provider.asyncio, "sleep", advance)
    monkeypatch.setattr(provider, "_retry_delay", lambda _exc, _n: 5.0)

    async def fail(timeout):
        calls.append(timeout)
        raise RuntimeError("connection error")

    with pytest.raises(RuntimeError, match="connection error"):
        provider.asyncio.run(provider.request_with_retry_async(
            fail, timeout=90, max_retries=4, deadline=12,
            observer=events.append))

    assert len(calls) == 2
    assert calls == pytest.approx([2.0, 0.001])
    assert clock.now == pytest.approx(11.999)
    assert events[-1]["event"] == "give_up"
    assert events[-1]["reason"] == "deadline"
