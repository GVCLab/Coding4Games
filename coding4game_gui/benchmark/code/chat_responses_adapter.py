"""Adapt an OpenAI Responses request to a Chat Completions-only provider."""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any


class AdapterResponse:
    def __init__(self, status: int, headers: Mapping[str, str], chunks: Iterable[bytes]):
        self.status = status
        self.headers = dict(headers)
        self._chunks = chunks

    def iter_bytes(self):
        yield from self._chunks

    def close(self):
        close = getattr(self._chunks, "close", None)
        if callable(close):
            close()


def _serialized(value: Any) -> bytes:
    dump_json = getattr(value, "model_dump_json", None)
    if callable(dump_json):
        return dump_json(exclude_none=True).encode("utf-8")
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        value = dump(exclude_none=True)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _custom_arguments(value: Any) -> str:
    return json.dumps({"input": value if isinstance(value, str) else str(value)},
                      ensure_ascii=False, separators=(",", ":"))


def _custom_input(arguments: Any) -> str:
    if not isinstance(arguments, str):
        return str(arguments)
    try:
        value = json.loads(arguments)
    except (TypeError, ValueError):
        return arguments
    if isinstance(value, dict) and isinstance(value.get("input"), str):
        return value["input"]
    return arguments


def _normalize_input(value: Any, custom_tools: set[str]) -> Any:
    if not isinstance(value, list):
        return value
    normalized = []
    for item in value:
        if not isinstance(item, dict):
            normalized.append(item)
            continue
        item = dict(item)
        item_type = item.get("type")
        if item_type == "custom_tool_call":
            name = item.get("name")
            if isinstance(name, str):
                custom_tools.add(name)
            normalized.append({
                "type": "function_call",
                "call_id": item.get("call_id") or item.get("id"),
                "name": name,
                "arguments": _custom_arguments(item.get("input", "")),
            })
        elif item_type == "custom_tool_call_output":
            normalized.append({
                "type": "function_call_output",
                "call_id": item.get("call_id"),
                "output": item.get("output", ""),
            })
        else:
            normalized.append(item)
    return normalized


def _normalize_chat_request(request: dict[str, Any]) -> dict[str, Any]:
    """Translate Responses-only tools to the subset expressible by Chat."""
    context: dict[str, Any] = {
        "custom_tools": set(), "namespaces": {}, "custom_item_ids": set(),
        "message_item_ids": set(), "reasoning_summary_parts": set(),
    }
    tools = request.get("tools")
    if isinstance(tools, list):
        flattened = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_type = tool.get("type")
            if tool_type in {"web_search", "tool_search"}:
                continue
            if tool_type == "custom":
                name = tool.get("name")
                if not isinstance(name, str) or not name:
                    continue
                context["custom_tools"].add(name)
                flattened.append({
                    "type": "function",
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": {
                        "type": "object",
                        "properties": {"input": {"type": "string"}},
                        "required": ["input"],
                        "additionalProperties": False,
                    },
                })
                continue
            if tool_type != "namespace":
                flattened.append(tool)
                continue
            namespace = tool.get("name")
            children = tool.get("tools")
            if not isinstance(namespace, str) or not isinstance(children, list):
                continue
            for child in children:
                if not isinstance(child, dict) or child.get("type") != "function":
                    continue
                child = dict(child)
                name = child.get("name")
                if not isinstance(name, str):
                    continue
                flat_name = namespace + "__" + name
                child["name"] = flat_name
                context["namespaces"][flat_name] = (namespace, name)
                flattened.append(child)
        request["tools"] = flattened
    request["input"] = _normalize_input(request.get("input"), context["custom_tools"])
    # Compatible Chat endpoints vary widely in reasoning extensions. A Responses
    # summary request is not a Chat reasoning-effort value and commonly causes 400.
    request.pop("reasoning", None)
    request.pop("include", None)
    return context


def _response_item(item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if item.get("type") != "function_call":
        return item
    name = item.get("name")
    if name in context["custom_tools"]:
        call_id = item.get("call_id") or item.get("id") or "call_0"
        suffix = str(item.get("id") or call_id)
        if suffix.startswith("fc_"):
            suffix = suffix[3:]
        return {
            "id": "ctc_" + suffix,
            "type": "custom_tool_call",
            "status": item.get("status", "completed"),
            "call_id": call_id,
            "name": name,
            "input": _custom_input(item.get("arguments", "")),
        }
    namespace = context["namespaces"].get(name)
    if namespace:
        item = dict(item)
        item["namespace"], item["name"] = namespace
    return item


def _rewrite_response_event(value: Any, context: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            value = dump(exclude_none=True)
        else:
            value = json.loads(_serialized(value))
    value = dict(value)
    event_type = value.get("type")
    item = value.get("item")
    if (event_type == "response.output_item.added" and isinstance(item, dict)
            and item.get("type") == "reasoning"):
        item = dict(item)
        item.setdefault("summary", [])
        value["item"] = item
    if isinstance(item, dict) and item.get("type") == "function_call":
        name = item.get("name")
        value["item"] = _response_item(dict(item), context)
        if name in context["custom_tools"] and event_type == "response.output_item.added":
            context["custom_item_ids"].add(item.get("id"))
            value["item"]["input"] = ""
    if event_type == "response.function_call_arguments.delta":
        # JSON argument fragments cannot be losslessly exposed as free-form input.
        return None
    if event_type == "response.function_call_arguments.done":
        # LiteLLM's done event has no name, so match its item id to the custom id
        # convention used for Codex custom calls. This adapter currently exposes
        # one custom tool name (apply_patch) in production requests.
        if value.get("item_id") in context["custom_item_ids"]:
            value["type"] = "response.custom_tool_call_input.delta"
            value["delta"] = _custom_input(value.pop("arguments", ""))
            value["call_id"] = value.get("call_id") or str(value.get("item_id", "")).removeprefix("fc_")
            return value
    output = value.get("output")
    if isinstance(output, list):
        value["output"] = [
            _response_item(dict(entry), context) if isinstance(entry, dict) else entry
            for entry in output
        ]
    return value


def _default_responses_call(**kwargs):
    """LiteLLM bridge with Kimi-compatible tool-result names."""
    import litellm
    from litellm.responses.litellm_completion_transformation.handler import (
        LiteLLMCompletionResponsesConfig,
    )
    from litellm.responses.litellm_completion_transformation.streaming_iterator import (
        LiteLLMCompletionStreamingIterator,
    )
    from litellm.types.utils import ModelResponse

    model = kwargs.pop("model")
    request_input = kwargs.pop("input")
    stream = kwargs.get("stream")
    transport_keys = {
        "api_base", "api_key", "use_chat_completions_api", "drop_params",
        "num_retries", "timeout",
    }
    completion_options = {key: kwargs.pop(key) for key in list(kwargs)
                          if key in transport_keys}
    completion_options.pop("use_chat_completions_api", None)
    responses_request = dict(kwargs)
    completion_request = (
        LiteLLMCompletionResponsesConfig
        .transform_responses_api_request_to_chat_completion_request(
            model=model, input=request_input, responses_api_request=responses_request,
            stream=stream,
        )
    )
    call_names = {}
    for message in completion_request.get("messages", []):
        for call in message.get("tool_calls", []) or []:
            function = call.get("function") or {}
            if call.get("id") and function.get("name"):
                call_names[call["id"]] = function["name"]
    for message in completion_request.get("messages", []):
        if message.get("role") == "tool" and message.get("tool_call_id") in call_names:
            message["name"] = call_names[message["tool_call_id"]]

    result = litellm.completion(**completion_request, **completion_options)
    if isinstance(result, ModelResponse):
        return LiteLLMCompletionResponsesConfig.transform_chat_completion_response_to_responses_api_response(
            chat_completion_response=result, request_input=request_input,
            responses_api_request=responses_request,
        )
    return LiteLLMCompletionStreamingIterator(
        model=model, litellm_custom_stream_wrapper=result,
        request_input=request_input, responses_api_request=responses_request,
        custom_llm_provider="openai", litellm_metadata={},
    )


def make_chat_responses_transport(
    *,
    api_base: str,
    api_key: str,
    provider_model: str,
    responses_call: Callable[..., Any] | None = None,
):
    """Return the gateway transport contract backed by LiteLLM's bridge."""
    if responses_call is None:
        responses_call = _default_responses_call

    def transport(_method, _url, _headers, body, timeout):
        request = json.loads(body)
        stream = bool(request.get("stream"))
        request.pop("model", None)
        # Codex-local correlation fields are not Chat Completions parameters.
        request.pop("prompt_cache_key", None)
        request.pop("client_metadata", None)
        context = _normalize_chat_request(request)
        try:
            result = responses_call(
                model=provider_model,
                api_base=api_base,
                api_key=api_key,
                use_chat_completions_api=True,
                drop_params=True,
                num_retries=0,
                timeout=timeout,
                **request,
            )
        except Exception as error:
            status = getattr(error, "status_code", None)
            if not isinstance(status, int):
                raise
            headers = getattr(error, "headers", None) or {}
            payload = json.dumps({
                "error": {
                    "code": "chat_upstream_error",
                    "message": f"Chat Completions upstream returned HTTP {status}",
                }
            }, separators=(",", ":")).encode("utf-8")
            return AdapterResponse(status, {"content-type": "application/json", **dict(headers)}, [payload])
        if not stream:
            rewritten = _rewrite_response_event(result, context)
            return AdapterResponse(200, {"content-type": "application/json"},
                                   [_serialized(rewritten)])

        def chunks():
            for event in result:
                rewritten = _rewrite_response_event(event, context)
                if rewritten is not None:
                    if rewritten.get("type") == "response.reasoning_summary_text.delta":
                        part_key = (rewritten.get("item_id"),
                                    rewritten.get("summary_index", 0))
                        if part_key not in context["reasoning_summary_parts"]:
                            context["reasoning_summary_parts"].add(part_key)
                            part = {
                                "type": "response.reasoning_summary_part.added",
                                "item_id": rewritten.get("item_id"),
                                "output_index": rewritten.get("output_index", 0),
                                "summary_index": rewritten.get("summary_index", 0),
                                "part": {"type": "summary_text", "text": ""},
                            }
                            yield b"data: " + _serialized(part) + b"\n\n"
                    if rewritten.get("type") == "response.output_text.delta":
                        item_id = rewritten.get("item_id")
                        if item_id not in context["message_item_ids"]:
                            context["message_item_ids"].add(item_id)
                            output_index = rewritten.get("output_index", 0)
                            content_index = rewritten.get("content_index", 0)
                            added = {
                                "type": "response.output_item.added",
                                "output_index": output_index,
                                "item": {
                                    "id": item_id, "type": "message",
                                    "status": "in_progress", "role": "assistant",
                                    "content": [],
                                },
                            }
                            part = {
                                "type": "response.content_part.added",
                                "item_id": item_id, "output_index": output_index,
                                "content_index": content_index,
                                "part": {"type": "output_text", "text": "", "annotations": []},
                            }
                            yield b"data: " + _serialized(added) + b"\n\n"
                            yield b"data: " + _serialized(part) + b"\n\n"
                    yield b"data: " + _serialized(rewritten) + b"\n\n"

        return AdapterResponse(200, {"content-type": "text/event-stream"}, chunks())

    return transport
