import json

from coding4game_gui.benchmark.code.chat_responses_adapter import (
    make_chat_responses_transport,
)


class Event:
    def __init__(self, value):
        self.value = value

    def model_dump(self, *, exclude_none):
        assert exclude_none
        return self.value


def test_chat_adapter_uses_litellm_bridge_and_returns_responses_sse():
    calls = []

    def responses_call(**kwargs):
        calls.append(kwargs)
        return iter([
            Event({"type": "response.output_text.delta", "delta": "ok"}),
            Event({"type": "response.completed", "response": {
                "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}}}),
        ])

    transport = make_chat_responses_transport(
        api_base="https://provider.invalid/v1", api_key="secret",
        provider_model="openai/kimi-k3", responses_call=responses_call)
    request = {"model": "kimi-k3", "input": "hello", "stream": True,
               "prompt_cache_key": "local-only", "client_metadata": {"x": 1}}
    response = transport("POST", "ignored", {}, json.dumps(request).encode(), 12)

    assert response.status == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert b"response.completed" in b"".join(response.iter_bytes())
    assert calls[0]["model"] == "openai/kimi-k3"
    assert calls[0]["use_chat_completions_api"] is True
    assert calls[0]["drop_params"] is True
    assert calls[0]["num_retries"] == 0
    assert "prompt_cache_key" not in calls[0]
    assert "client_metadata" not in calls[0]


def test_chat_adapter_flattens_namespaces_and_drops_responses_only_features():
    calls = []

    def responses_call(**kwargs):
        calls.append(kwargs)
        return Event({"id": "response", "usage": {"total_tokens": 1}})

    transport = make_chat_responses_transport(
        api_base="https://provider.invalid/v1", api_key="secret",
        provider_model="openai/kimi-k3", responses_call=responses_call)
    request = {
        "model": "kimi-k3", "input": "hello", "stream": False,
        "reasoning": {"summary": "auto"}, "include": ["reasoning.encrypted_content"],
        "tools": [
            {"type": "web_search"},
            {"type": "namespace", "name": "mcp__game", "tools": [
                {"type": "function", "name": "status", "parameters": {"type": "object"}},
            ]},
        ],
    }
    response = transport("POST", "ignored", {}, json.dumps(request).encode(), 12)
    assert response.status == 200
    assert calls[0]["tools"] == [
        {"type": "function", "name": "mcp__game__status",
         "parameters": {"type": "object"}},
    ]
    assert "reasoning" not in calls[0]
    assert "include" not in calls[0]


def test_chat_adapter_proxies_custom_tools_and_drops_tool_search():
    calls = []

    def responses_call(**kwargs):
        calls.append(kwargs)
        return iter([
            Event({
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "fc_1", "type": "function_call",
                    "call_id": "call_1", "name": "apply_patch",
                    "arguments": "",
                },
            }),
            Event({
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_1", "output_index": 0,
                "delta": '{"input":"*** Begin Patch',
            }),
            Event({
                "type": "response.function_call_arguments.done",
                "item_id": "fc_1", "output_index": 0,
                "arguments": '{"input":"*** Begin Patch\\n*** End Patch"}',
            }),
            Event({
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "fc_1", "type": "function_call", "status": "completed",
                    "call_id": "call_1", "name": "apply_patch",
                    "arguments": '{"input":"*** Begin Patch\\n*** End Patch"}',
                },
            }),
        ])

    transport = make_chat_responses_transport(
        api_base="https://provider.invalid/v1", api_key="secret",
        provider_model="openai/glm-5.3-flash", responses_call=responses_call)
    request = {
        "model": "glm-5.3-flash", "stream": True,
        "input": [
            {"type": "custom_tool_call", "call_id": "old_call",
             "name": "apply_patch", "input": "old patch"},
            {"type": "custom_tool_call_output", "call_id": "old_call",
             "output": "Done!"},
        ],
        "tools": [
            {"type": "custom", "name": "apply_patch",
             "description": "Apply a patch", "format": {"type": "grammar"}},
            {"type": "tool_search", "name": "tool_search",
             "parameters": {"type": "object"}},
        ],
    }
    response = transport("POST", "ignored", {}, json.dumps(request).encode(), 12)
    events = [json.loads(chunk.removeprefix(b"data: "))
              for chunk in response.iter_bytes()]

    assert calls[0]["tools"] == [{
        "type": "function", "name": "apply_patch",
        "description": "Apply a patch",
        "parameters": {
            "type": "object", "properties": {"input": {"type": "string"}},
            "required": ["input"], "additionalProperties": False,
        },
    }]
    assert calls[0]["input"] == [
        {"type": "function_call", "call_id": "old_call",
         "name": "apply_patch", "arguments": '{"input":"old patch"}'},
        {"type": "function_call_output", "call_id": "old_call", "output": "Done!"},
    ]
    assert [event["type"] for event in events] == [
        "response.output_item.added",
        "response.custom_tool_call_input.delta",
        "response.output_item.done",
    ]
    assert events[0]["item"]["type"] == "custom_tool_call"
    assert events[1]["delta"] == "*** Begin Patch\n*** End Patch"
    assert events[2]["item"] == {
        "id": "ctc_1", "type": "custom_tool_call", "status": "completed",
        "call_id": "call_1", "name": "apply_patch",
        "input": "*** Begin Patch\n*** End Patch",
    }


def test_chat_adapter_synthesizes_message_lifecycle_before_text_delta():
    def responses_call(**_kwargs):
        return iter([Event({
            "type": "response.output_text.delta", "item_id": "msg_1",
            "output_index": 0, "content_index": 0, "delta": "OK",
        })])

    transport = make_chat_responses_transport(
        api_base="https://provider.invalid/v1", api_key="secret",
        provider_model="openai/glm-5.3-flash", responses_call=responses_call)
    response = transport("POST", "ignored", {}, json.dumps({
        "model": "glm-5.3-flash", "input": "hello", "stream": True,
    }).encode(), 12)
    events = [json.loads(chunk.removeprefix(b"data: "))
              for chunk in response.iter_bytes()]

    assert [event["type"] for event in events] == [
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
    ]
    assert events[0]["item"]["id"] == "msg_1"


def test_chat_adapter_synthesizes_reasoning_part_before_summary_delta():
    def responses_call(**_kwargs):
        return iter([
            Event({
                "type": "response.output_item.added", "output_index": 0,
                "item": {"id": "rs_1", "type": "reasoning", "status": "in_progress"},
            }),
            Event({
                "type": "response.reasoning_summary_text.delta", "item_id": "rs_1",
                "output_index": 0, "summary_index": 0, "delta": "thinking",
            }),
        ])

    transport = make_chat_responses_transport(
        api_base="https://provider.invalid/v1", api_key="secret",
        provider_model="openai/glm-5.3-flash", responses_call=responses_call)
    response = transport("POST", "ignored", {}, json.dumps({
        "model": "glm-5.3-flash", "input": "hello", "stream": True,
    }).encode(), 12)
    events = [json.loads(chunk.removeprefix(b"data: "))
              for chunk in response.iter_bytes()]

    assert [event["type"] for event in events] == [
        "response.output_item.added",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_text.delta",
    ]
    assert events[0]["item"]["summary"] == []
    assert events[1]["part"] == {"type": "summary_text", "text": ""}
