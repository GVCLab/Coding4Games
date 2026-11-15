"""Preserve verified upstream Bash inputs across native leading-cd normalization."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


class ToolUseCollector:
    """Collect Bash calls only from a complete, error-free upstream response."""

    def __init__(self, model):
        self.model = model
        self.started = self.complete = self.failed = False
        self.blocks = {}
        self.fragments = {}

    def observe(self, value):
        if not isinstance(value, dict):
            return
        kind = value.get("type")
        if kind == "error":
            self.failed = True
        elif kind == "message":
            if self.started or value.get("model") != self.model:
                self.failed = True
            self.started = self.complete = True
            self.blocks = {i: copy.deepcopy(block) for i, block in enumerate(value.get("content", []))
                           if isinstance(block, dict) and block.get("type") == "tool_use"
                           and block.get("name") == "Bash"}
        elif kind == "message_start":
            if self.started or value.get("message", {}).get("model") != self.model:
                self.failed = True
            self.started = True
        elif kind == "content_block_start":
            block = value.get("content_block", {})
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                index = value.get("index")
                if index in self.blocks:
                    self.failed = True
                self.blocks[index] = copy.deepcopy(block)
        elif kind == "content_block_delta":
            index, delta = value.get("index"), value.get("delta", {})
            if index in self.blocks and delta.get("type") == "input_json_delta":
                fragment = delta.get("partial_json")
                if not isinstance(fragment, str):
                    self.failed = True
                else:
                    self.fragments.setdefault(index, []).append(fragment)
        elif kind == "message_stop":
            self.complete = True

    def calls(self):
        if not self.started or not self.complete or self.failed:
            return []
        result, seen = [], set()
        for index, block in self.blocks.items():
            if index in self.fragments:
                try:
                    block = copy.deepcopy(block)
                    block["input"] = json.loads("".join(self.fragments[index]))
                except (ValueError, TypeError):
                    return []
            if (not isinstance(block.get("id"), str) or not block["id"]
                    or block["id"] in seen or not isinstance(block.get("input"), dict)
                    or not isinstance(block["input"].get("command"), str)):
                return []
            seen.add(block["id"])
            result.append(block)
        return result


class ToolInputReplay:
    """Restore only a captured Bash command whose leading cd was removed."""

    def __init__(self, sources=()):
        self.sources = {}
        for source in sources:
            self.remember(source["tool_use"], source["request_id"])

    def remember(self, call, request_id):
        if (call.get("type") != "tool_use" or call.get("name") != "Bash"
                or not isinstance(request_id, str) or not request_id
                or not isinstance(call.get("id"), str) or not call["id"]
                or not isinstance(call.get("input"), dict)
                or not isinstance(call["input"].get("command"), str)):
            raise ValueError("Invalid original Bash tool-call provenance")
        source = {"tool_use": copy.deepcopy(call), "request_id": request_id}
        previous = self.sources.get(call["id"])
        if previous is not None:
            original = previous["tool_use"]
            if original["name"] != call["name"] or original["input"] != call["input"]:
                raise ValueError("Conflicting original Bash inputs for one tool-use ID")
        else:
            self.sources[call["id"]] = source

    @staticmethod
    def _removed_cd(original, replay):
        if not isinstance(replay, str) or not replay or not original.endswith(replay):
            return False
        prefix = original[:-len(replay)]
        try:
            tokens = shlex.split(prefix)
        except ValueError:
            return False
        if len(tokens) == 4 and tokens[:2] == ["cd", "--"]:
            tokens = ["cd", *tokens[2:]]
        return (len(tokens) == 3 and tokens[0] == "cd" and tokens[-1] == "&&"
                and PurePosixPath(tokens[1]).is_absolute()
                and "\n" not in tokens[1] and "\x00" not in tokens[1])

    def normalize(self, request):
        changes, seen = [], set()
        for message in request.get("messages", []):
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                source = self.sources.get(block.get("id"))
                if source is None:
                    continue
                if block["id"] in seen or message.get("role") != "assistant":
                    raise ValueError("Original tool-use ID was duplicated or moved to another role")
                seen.add(block["id"])
                original = source["tool_use"]
                if block.get("name") != original["name"] or not isinstance(block.get("input"), dict):
                    raise ValueError("Original tool-use identity changed")
                previous, incoming = original["input"], block["input"]
                if incoming == previous:
                    continue
                expected = copy.deepcopy(previous)
                expected["command"] = incoming.get("command")
                if incoming != expected or not self._removed_cd(previous["command"], incoming.get("command")):
                    raise ValueError("Tool replay differs beyond verified leading-cd normalization")
                before = _digest(incoming)
                block["input"] = copy.deepcopy(previous)
                changes.append({"transformation": "anthropic_restore_leading_cd_tool_input",
                                "tool_use_id": block["id"], "source_request_id": source["request_id"],
                                "original_input_sha256": before, "restored_input_sha256": _digest(previous)})
        return changes


def sources_from_records(records, model):
    """Recover original calls from durable native journals, excluding partial streams."""
    grouped = {}
    for record in records:
        request_id = record.get("request_id")
        if not request_id:
            continue
        current = grouped.setdefault(request_id, {"chunks": []})
        if record.get("event") == "attempt":
            current["status"] = record.get("upstream_status")
        elif record.get("event") == "response_chunk":
            current["chunks"].append(bytes.fromhex(record["data_hex"]))
    replay = ToolInputReplay()
    for request_id, response in grouped.items():
        if response.get("status") != 200:
            continue
        raw = b"".join(response["chunks"])
        collector = ToolUseCollector(model)
        try:
            full = json.loads(raw)
        except (ValueError, UnicodeError):
            for line in raw.splitlines():
                if line.startswith(b"data:"):
                    try:
                        collector.observe(json.loads(line[5:].strip()))
                    except (ValueError, UnicodeError):
                        collector.failed = True
        else:
            collector.observe(full)
        for call in collector.calls():
            replay.remember(call, request_id)
    return list(replay.sources.values())


def sources_from_journals(sources, model):
    """Read original response bytes separately from the intentionally compact ledger."""
    records = []
    for source in sources:
        digest, size = hashlib.sha256(), 0
        with Path(source["path"]).open("rb") as stream:
            for line in stream:
                digest.update(line)
                size += len(line)
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    if line.endswith(b"\n"):
                        raise ValueError("Malformed native tool replay provenance")
                    continue
                if value.get("event") in {"attempt", "response_chunk"}:
                    records.append(value)
        if digest.hexdigest() != source["sha256"] or size != source["bytes"]:
            raise ValueError("Native gateway evidence changed during tool replay recovery")
    return sources_from_records(records, model)
