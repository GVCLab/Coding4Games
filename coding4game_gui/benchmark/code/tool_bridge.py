"""Authenticated loopback bridge from boxed programs to trusted game tools."""
from __future__ import annotations

import hashlib
import json
import secrets
import socket
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

from coding4game_gui.runtime_client.client import STEP_TOOL, SOURCE_PATH_SCHEMA


# Rejection cleanup is independent of the accepted JSON size limit. Even a
# malicious Content-Length cannot make a rejected request drain without bounds.
_REJECT_DRAIN_BYTES = 1_048_576
_REJECT_DRAIN_SECONDS = 1.0


DEVELOPMENT_TOOLS = [
    {"name": "game.start", "description": "Start a fresh development episode.",
     "inputSchema": {"type": "object", "properties": {
         "max_tics": {"type": ["integer", "null"], "minimum": 1},
         "mode": {"enum": ["stepped"]}}, "required": ["mode"],
         "additionalProperties": False}},
    {"name": "game.observe", "description": "Capture the current game observation.",
     "inputSchema": {"type": "object", "properties": {
         "episode_id": {"type": "string"}}, "additionalProperties": False}},
    STEP_TOOL,
    {"name": "game.stop", "description": "Stop and close the current episode.",
     "inputSchema": {"type": "object", "properties": {
         "episode_id": {"type": "string"}}, "additionalProperties": False}},
    {"name": "bench.status", "description": "Read development budget and session status.",
     "inputSchema": {"type": "object", "additionalProperties": False}},
    {"name": "bench.replay", "description": "Read recorded episode evidence without playing.",
     "inputSchema": {"type": "object", "properties": {
         "uid": {"type": "string"}, "start": {"type": ["integer", "string"]},
         "end": {"type": ["integer", "string"]},
         "every": {"type": "integer", "minimum": 1}},
         "required": ["uid", "start", "end"], "additionalProperties": False}},
    {"name": "submission.save", "description": "Save an immutable executable candidate checkpoint without ending development.",
     "inputSchema": {"type": "object", "properties": {
         "path": SOURCE_PATH_SCHEMA, "entrypoint": {"type": "object"},
         "description": {"type": "string"},
         "dependencies": {"type": ["object", "null"], "description":
             "Optional dependency declaration. On WSL2, environment may contain exact "
             "requirements (name==version strings), HTTPS indexes, and assets with "
             "url, lowercase SHA-256, and bundle-relative path. The host freezes it "
             "into the saved candidate; callers must not provide environment_lock."}},
         "required": ["path", "entrypoint"], "additionalProperties": False}},
    {"name": "submission.run", "description": "Evaluate one saved executable candidate in a fresh episode within the development box and shared budget.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"},
         "max_tics": {"type": ["integer", "null"], "minimum": 1}},
         "required": ["id"], "additionalProperties": False}},
    {"name": "submission.finish", "description": "Select the final saved program and end development at any time; unused game tics remain unspent.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
         "required": ["id"], "additionalProperties": False}},
]

FINAL_NAMES = {"game.observe", "game.step", "game.stop", "bench.status"}


def tools_for_phase(phase: str, *, max_action_tics: int = 1000) -> list[dict]:
    if phase not in {"development", "final_test"}:
        raise ValueError("phase must be development or final_test")
    tools = DEVELOPMENT_TOOLS if phase == "development" else [
        item for item in DEVELOPMENT_TOOLS if item["name"] in FINAL_NAMES]
    # Prevent callers from mutating the canonical schema, then narrow the
    # globally supported schema to the selected scenario's configured cap.
    tools = json.loads(json.dumps(tools))
    from coding4game_gui.benchmark.windows.timing import resolve_max_action_tics
    cap = resolve_max_action_tics({"max_action_tics": max_action_tics})
    step = next(item for item in tools if item["name"] == "game.step")
    props = step["inputSchema"]["properties"]
    props["tics"]["maximum"] = cap
    for shape in props["actions"]["items"]["oneOf"]:
        shape["properties"]["tics"]["maximum"] = cap
    if cap != 1000:
        step["description"] += f" This scenario permits at most {cap} tics per action."
    return tools


def _digest(tool: str, arguments: dict) -> str:
    raw = json.dumps({"tool": tool, "arguments": arguments}, sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ToolServer:
    def __init__(self, session, port: int = 0, phase: str = "development",
                 token: str | None = None, max_request_bytes: int = 1_048_576,
                 cache_size: int = 256, max_seen: int = 100_000,
                 observation_size=(1280, 768)):
        self.max_action_tics = getattr(getattr(session, "game", None),
                                       "max_action_tics", 1000)
        self._phase_tools = {item["name"] for item in tools_for_phase(
            phase, max_action_tics=self.max_action_tics)}
        if any(type(value) is not int or value <= 0 for value in
               (max_request_bytes, cache_size, max_seen)):
            raise ValueError("server limits must be positive integers")
        self.session, self.port, self.phase = session, port, phase
        if tuple(observation_size) not in {(1280, 768), (768, 461), (1000, 1000)}:
            raise ValueError("unsupported observation size")
        self.observation_size = tuple(observation_size)
        self.token = token or secrets.token_urlsafe(32)
        self.max_request_bytes = max_request_bytes
        self.cache_size, self.max_seen = cache_size, max_seen
        self._guard = threading.Lock()
        self._seen: dict[str, str] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._cache: OrderedDict[str, tuple[int, dict]] = OrderedDict()
        self._httpd = self._thread = None

    @property
    def url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("tool server is not running")
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/call"

    def calls_settled(self) -> bool:
        """Report host execution completion, independently of CLI delivery."""
        with self._guard:
            return not self._inflight

    def _error(self, request_id, code, message, status):
        return status, {"ok": False, "request_id": request_id,
                        "error": {"code": code, "message": str(message)[:1000]}}

    def _execute(self, request_id: str, tool: str, arguments: dict) -> tuple[int, dict]:
        try:
            if self.observation_size != (1280, 768):
                from coding4game_gui.runtime_client.client import _viewport_arguments
                arguments = _viewport_arguments(tool, arguments, self.observation_size)
            result = self.session.dispatch(tool, arguments, phase=self.phase)
            if not isinstance(result, dict):
                raise TypeError("session dispatch result must be an object")
            if self.observation_size != (1280, 768):
                result = self._viewport_result(result)
            return 200, {"ok": True, "request_id": request_id, "result": result}
        except Exception as exc:  # structured boundary; never expose traceback
            return self._error(request_id, "dispatch_error",
                               f"{type(exc).__name__}: {exc}", 500)

    def _viewport_result(self, result):
        """Present one run-wide image space to MCP and executable clients alike."""
        import base64
        import cv2
        import numpy as np
        result = dict(result)
        result["observation_size"] = list(self.observation_size)
        if "observation" in result:
            observation = result["observation"]
            frame = cv2.imdecode(np.frombuffer(base64.b64decode(observation["data"]),
                                              np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError("invalid observation image")
            frame = cv2.resize(frame, self.observation_size, interpolation=cv2.INTER_AREA)
            ok, encoded = cv2.imencode(".png", frame)
            if not ok:
                raise ValueError("viewport encoding failed")
            result["observation"] = {"mimeType": "image/png",
                "data": base64.b64encode(encoded.tobytes()).decode("ascii")}
        if "frames" in result:
            result["frames"] = [self._viewport_result(frame) for frame in result["frames"]]
        return result

    def handle_call(self, payload) -> tuple[int, dict]:
        if not isinstance(payload, dict) or set(payload) != {
                "request_id", "tool", "arguments"}:
            return self._error(None, "invalid_request", "invalid request object", 400)
        request_id, tool, arguments = (payload["request_id"], payload["tool"],
                                       payload["arguments"])
        if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
            return self._error(None, "invalid_request", "invalid request_id", 400)
        if not isinstance(tool, str) or not isinstance(arguments, dict):
            return self._error(request_id, "invalid_request", "invalid tool or arguments", 400)
        if tool not in self._phase_tools:
            return self._error(request_id, "tool_not_allowed", "tool unavailable in this phase", 403)
        try:
            digest = _digest(tool, arguments)
        except (TypeError, ValueError):
            return self._error(request_id, "invalid_request", "arguments must be finite JSON", 400)

        leader = False
        with self._guard:
            previous = self._seen.get(request_id)
            if previous is not None and previous != digest:
                return self._error(request_id, "request_id_reused",
                                   "request_id was already used with another payload", 409)
            if request_id in self._cache:
                return self._cache[request_id]
            event = self._inflight.get(request_id)
            if previous is not None and event is None:
                return self._error(request_id, "result_evicted",
                                   "request result is no longer cached", 409)
            if previous is None:
                if len(self._seen) >= self.max_seen:
                    return self._error(request_id, "request_id_capacity",
                                       "request-id registry capacity reached", 503)
                self._seen[request_id] = digest
                event = self._inflight[request_id] = threading.Event()
                leader = True

        if not leader:
            event.wait()
            with self._guard:
                return self._cache.get(request_id) or self._error(
                    request_id, "result_evicted", "request result is no longer cached", 409)

        response = self._execute(request_id, tool, arguments)
        with self._guard:
            self._cache[request_id] = response
            self._cache.move_to_end(request_id)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
            event = self._inflight.pop(request_id)
            event.set()
        return response

    def __enter__(self):
        owner = self

        class Server(ThreadingHTTPServer):
            allow_reuse_address = False
            daemon_threads = True

            def server_bind(self):
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.socket.setsockopt(socket.SOL_SOCKET,
                                           socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        class Handler(BaseHTTPRequestHandler):
            server_version = "C4GToolBridge/1"
            sys_version = ""

            def _write(self, status, body):
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    # The boxed client can disappear while a long-running
                    # tool is still completing. The dispatch result remains
                    # cached for request-id idempotency; only this HTTP peer
                    # is gone, so never replay the tool or print a server
                    # traceback during normal session cancellation.
                    self.close_connection = True
                    return False
                return True

            def do_GET(self):
                self._reject(405, "method_not_allowed", "POST required")

            def _reject(self, status, code, message):
                self.close_connection = True
                try:
                    if not self._write(
                            status,
                            owner._error(None, code, message, status)[1]):
                        return
                    self.wfile.flush()
                    # Deliver the complete response before waiting on a client
                    # that may never finish uploading. Keep the read side open:
                    # closing with unread body bytes can reset Windows TCP.
                    self.connection.shutdown(socket.SHUT_WR)
                except OSError:
                    return  # The peer has already disconnected; never dispatch.
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                remaining = min(max(0, length), _REJECT_DRAIN_BYTES)
                deadline = time.monotonic() + _REJECT_DRAIN_SECONDS
                while remaining:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        break
                    self.connection.settimeout(timeout)
                    try:
                        chunk = self.rfile.read1(min(65536, remaining))
                    except OSError:
                        break
                    if not chunk:
                        break
                    remaining -= len(chunk)
                # This connection is closing. Its read timeout never applies to
                # accepted requests or long-running submission/game dispatches.

            def do_POST(self):
                if self.path != "/call":
                    self._reject(404, "not_found", "not found")
                    return
                auth = self.headers.get("Authorization", "")
                if not secrets.compare_digest(auth, f"Bearer {owner.token}"):
                    self._reject(401, "unauthorized", "unauthorized")
                    return
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    length = -1
                if length < 0:
                    self._reject(411, "length_required", "valid Content-Length required")
                    return
                if length > owner.max_request_bytes:
                    self._reject(413, "request_too_large", "request exceeds size limit")
                    return
                try:
                    payload = json.loads(self.rfile.read(length))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._write(400, owner._error(None, "invalid_json", "invalid JSON", 400)[1])
                    return
                self._write(*owner.handle_call(payload))

            def log_message(self, *_args):
                pass

        self._httpd = Server(("127.0.0.1", self.port), Handler, bind_and_activate=True)
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="c4g-tool-bridge", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = self._thread = None


__all__ = ["ToolServer", "tools_for_phase"]
