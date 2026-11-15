import base64
import http.client
import io
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest
import cv2
import numpy as np

from coding4game_gui.runtime_client.client import Client
from coding4game_gui.runtime_client.client import mcp_main
from coding4game_gui.benchmark.code.tool_bridge import ToolServer
from coding4game_gui.benchmark.code.tool_bridge import tools_for_phase


def test_mcp_windows_pipe_is_utf8_even_without_utf8_environment():
    class UnicodeSession:
        def dispatch(self, *args, **kwargs):
            return {"end_reason": "unchanged — episode ended", "fixture": "中文"}

    with ToolServer(UnicodeSession()) as server:
        env = dict(os.environ, C4G_BRIDGE_URL=server.url,
                   C4G_BRIDGE_TOKEN=server.token, PYTHONIOENCODING="cp936")
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "bench.status", "arguments": {}}}
        process = subprocess.run(
            [sys.executable, "coding4game_gui/runtime_client/client.py", "--mcp"],
            input=(json.dumps(request) + "\n").encode(), env=env,
            capture_output=True, timeout=10)
    assert process.returncode == 0
    reply = json.loads(process.stdout.decode("utf-8"))
    assert "unchanged — episode ended" in reply["result"]["content"][0]["text"]


def test_mcp_stop_reaches_host_while_submission_waits(monkeypatch):
    started, stopped = threading.Event(), threading.Event()

    class WaitingSession:
        def dispatch(self, name, args, phase):
            if name == "submission.run":
                started.set()
                assert stopped.wait(2), "stop was blocked behind submission.run"
            elif name == "game.stop":
                assert started.wait(1)
                stopped.set()
            return {"done": True}

    source = io.StringIO("".join(json.dumps({
        "jsonrpc": "2.0", "id": index, "method": "tools/call",
        "params": {"name": name, "arguments": {}}}) + "\n"
        for index, name in [(1, "submission.run"), (2, "game.stop")]))
    output = io.StringIO()
    with ToolServer(WaitingSession()) as server:
        monkeypatch.setenv("C4G_BRIDGE_URL", server.url)
        monkeypatch.setenv("C4G_BRIDGE_TOKEN", server.token)
        mcp_main(stdin=source, stdout=output)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert {reply["id"] for reply in replies} == {1, 2}
    assert all("error" not in reply for reply in replies)


def test_mcp_rejects_overlapping_runs_without_queueing(monkeypatch):
    started, stopped = threading.Event(), threading.Event()
    calls = []

    class WaitingClient:
        def call(self, name, **kwargs):
            calls.append(name)
            if name == "submission.run":
                started.set()
                assert stopped.wait(2)
            else:
                stopped.set()
            return {"done": True}

    monkeypatch.setattr(Client, "from_env", lambda: WaitingClient())
    def requests():
        for index, name in [(1, "submission.run"), (2, "submission.run"), (3, "game.stop")]:
            if index == 2:
                assert started.wait(1)
            yield json.dumps({"jsonrpc": "2.0", "id": index, "method": "tools/call",
                              "params": {"name": name, "arguments": {}}}) + "\n"

    output = io.StringIO()
    mcp_main(stdin=requests(), stdout=output)
    replies = {r["id"]: r for r in map(json.loads, output.getvalue().splitlines())}
    assert "already pending" in replies[2]["error"]["message"]
    assert all("result" in replies[i] for i in (1, 3))
    assert calls.count("submission.run") == 1


class FakeSession:
    def __init__(self):
        self.calls = []
        self.release = threading.Event()

    def dispatch(self, name, args, phase="development"):
        self.calls.append((name, args, phase))
        if name == "submission.run" and args.get("wait"):
            self.release.wait(2)
        if name == "game.observe":
            ok, png = cv2.imencode(".png", np.zeros((4, 5, 3), dtype=np.uint8))
            assert ok
            return {"tic": 4, "observation": {
                "mimeType": "image/png",
                "data": base64.b64encode(png.tobytes()).decode("ascii")}}
        return {"called": name, "arguments": args}


def post(server, request_id, tool="bench.status", arguments=None, *, token=None):
    body = json.dumps({"request_id": request_id, "tool": tool,
                       "arguments": arguments or {}}).encode()
    request = urllib.request.Request(
        server.url, data=body, method="POST",
        headers={"Authorization": f"Bearer {token or server.token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_server_authenticates_dispatches_and_deduplicates(tmp_path):
    session = FakeSession()
    with ToolServer(session, token="secret") as server:
        status, first = post(server, "req-1", arguments={"detail": True})
        status2, duplicate = post(server, "req-1", arguments={"detail": True})
        denied, error = post(server, "req-2", token="wrong")

    assert status == status2 == 200
    assert first == duplicate
    assert session.calls == [("bench.status", {"detail": True}, "development")]
    assert denied == 401 and error["error"]["code"] == "unauthorized"


def test_client_reset_during_dispatch_does_not_emit_server_traceback(capsys):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = []

    class SlowSession:
        def dispatch(self, name, args, phase="development"):
            calls.append((name, args, phase))
            started.set()
            assert release.wait(2)
            return {"called": name}

    with ToolServer(SlowSession()) as server:
        handler = server._httpd.RequestHandlerClass
        original_finish = handler.finish

        def finish(self):
            try:
                original_finish(self)
            finally:
                finished.set()

        handler.finish = finish
        body = json.dumps({
            "request_id": "client-reset",
            "tool": "bench.status",
            "arguments": {},
        }).encode()
        request = (
            b"POST /call HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            + f"Authorization: Bearer {server.token}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        client = socket.create_connection(
            ("127.0.0.1", server._httpd.server_port), timeout=2)
        try:
            client.sendall(request)
            assert started.wait(1)
            linger = struct.pack("hh" if os.name == "nt" else "ii", 1, 0)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        finally:
            client.close()
        release.set()
        assert finished.wait(2)

    assert calls == [("bench.status", {}, "development")]
    assert "ConnectionResetError" not in capsys.readouterr().err


def test_request_id_reuse_and_eviction_fail_closed_without_reexecution():
    session = FakeSession()
    with ToolServer(session, cache_size=1) as server:
        assert post(server, "one", arguments={"x": 1})[0] == 200
        assert post(server, "one", arguments={"x": 2})[0] == 409
        assert post(server, "two")[0] == 200
        status, body = post(server, "one", arguments={"x": 1})

    assert status == 409 and body["error"]["code"] == "result_evicted"
    assert len(session.calls) == 2


def test_duplicate_inflight_waits_while_unrelated_request_can_proceed():
    session = FakeSession()
    results = []
    with ToolServer(session) as server:
        first = threading.Thread(target=lambda: results.append(
            post(server, "slow", "submission.run", {"wait": True})))
        first.start()
        while not session.calls:
            pass
        duplicate = threading.Thread(target=lambda: results.append(
            post(server, "slow", "submission.run", {"wait": True})))
        duplicate.start()
        assert post(server, "status", "bench.status")[0] == 200
        session.release.set()
        first.join(3); duplicate.join(3)

    assert len([call for call in session.calls if call[0] == "submission.run"]) == 1
    assert len(results) == 2 and results[0][1] == results[1][1]


def test_server_rejects_wrong_method_path_and_oversized_request():
    with ToolServer(FakeSession(), max_request_bytes=80) as server:
        request = urllib.request.Request(server.url, method="GET")
        with pytest.raises(urllib.error.HTTPError) as get_error:
            urllib.request.urlopen(request)
        wrong = urllib.request.Request(server.url.replace("/call", "/other"),
                                       data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as path_error:
            urllib.request.urlopen(wrong)
        status, body = post(server, "large", arguments={"data": "x" * 200})
    assert get_error.value.code == 405
    assert path_error.value.code == 404
    assert status == 413 and body["error"]["code"] == "request_too_large"


@pytest.mark.parametrize("path,token,limit,expected", [
    ("/call", "wrong", 65536, 401),
    ("/other", "secret", 65536, 404),
    ("/call", "secret", 80, 413),
])
def test_rejected_bodies_reliably_return_http_errors(path, token, limit, expected):
    session = FakeSession()
    failures = []
    with ToolServer(session, token="secret", max_request_bytes=limit) as server:
        for _ in range(120):
            connection = http.client.HTTPConnection(
                "127.0.0.1", server._httpd.server_port, timeout=3)
            try:
                connection.request("POST", path, b"x" * 32768,
                                   {"Authorization": f"Bearer {token}"})
                response = connection.getresponse()
                assert response.status == expected
                assert json.loads(response.read())["ok"] is False
            except OSError as error:
                failures.append(type(error).__name__)
            finally:
                connection.close()
    assert session.calls == []
    assert not failures, failures


@pytest.mark.parametrize("body_size,byte_limit,wait_limit", [
    (1, 1048576, 2.5),  # A stalled sender hits the one-second total deadline.
    (65536, 65536, 0.5),  # Drain stops at its byte cap, not Content-Length.
])
def test_rejected_incomplete_body_has_bounded_cleanup(
        monkeypatch, body_size, byte_limit, wait_limit):
    monkeypatch.setattr("coding4game_gui.benchmark.code.tool_bridge._REJECT_DRAIN_BYTES",
                        byte_limit)
    session = FakeSession()
    finished = threading.Event()
    with ToolServer(session) as server:
        handler = server._httpd.RequestHandlerClass
        original_finish = handler.finish

        def finish(self):
            try:
                original_finish(self)
            finally:
                finished.set()

        handler.finish = finish
        connection = http.client.HTTPConnection(
            "127.0.0.1", server._httpd.server_port, timeout=3)
        try:
            started = time.monotonic()
            connection.request("POST", "/call", b"x" * body_size, {
                "Authorization": "Bearer wrong", "Content-Length": str(10**9)})
            response = connection.getresponse()
            assert response.status == 401
            assert json.loads(response.read())["ok"] is False
            # Keep the client open: cleanup must not wait for the declared GB.
            assert finished.wait(wait_limit)
            assert time.monotonic() - started < 3
        finally:
            connection.close()
    assert session.calls == []


def test_rejection_cleanup_deadline_does_not_limit_accepted_dispatch(monkeypatch):
    monkeypatch.setattr("coding4game_gui.benchmark.code.tool_bridge._REJECT_DRAIN_SECONDS",
                        0.01)

    class SlowSession(FakeSession):
        def dispatch(self, *args, **kwargs):
            time.sleep(0.1)
            return super().dispatch(*args, **kwargs)

    session = SlowSession()
    with ToolServer(session) as server:
        assert post(server, "denied", token="wrong")[0] == 401
        assert post(server, "accepted")[0] == 200
    assert len(session.calls) == 1


def test_tool_listing_has_nine_development_tools_and_restricted_final_phase():
    development = {tool["name"] for tool in tools_for_phase("development")}
    final = {tool["name"] for tool in tools_for_phase("final_test")}
    assert development == {
        "game.start", "game.observe", "game.step", "game.stop",
        "bench.status", "bench.replay", "submission.save",
        "submission.run", "submission.finish"}
    assert final == {"game.observe", "game.step", "game.stop", "bench.status"}
    finish = next(tool for tool in tools_for_phase("development")
                  if tool["name"] == "submission.finish")
    assert "at any time" in finish["description"]
    assert "tic budget is exhausted" not in finish["description"]


def test_mcp_wire_schemas_are_accepted_by_claude_and_codex():
    source = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1,
                                    "method": "tools/list", "params": {}}) + "\n")
    output = io.StringIO()
    assert mcp_main(stdin=source, stdout=output) == 0
    tools = json.loads(output.getvalue())["result"]["tools"]
    assert any(tool["name"] == "game.step" for tool in tools)
    for tool in tools:
        schema = tool["inputSchema"]
        # Claude Code skips whole tools with top-level combinators.
        assert schema["type"] == "object"
        assert not {"oneOf", "anyOf", "allOf"}.intersection(schema), tool["name"]


def test_plain_client_calls_bridge_and_attaches_episode(monkeypatch):
    session = FakeSession()
    with ToolServer(session) as server:
        monkeypatch.setenv("C4G_BRIDGE_URL", server.url)
        monkeypatch.setenv("C4G_BRIDGE_TOKEN", server.token)
        monkeypatch.setenv("C4G_EPISODE_ID", "episode-7")
        result = Client.from_env().call("bench.status", detail=True)
        Client.from_env().call("game.stop")
    assert result["called"] == "bench.status"
    assert session.calls[0][1] == {"detail": True}
    assert session.calls[1][1] == {"episode_id": "episode-7"}


def test_seen_request_cap_fails_new_ids_without_forgetting_old_ones():
    session = FakeSession()
    with ToolServer(session, max_seen=1) as server:
        assert post(server, "one")[0] == 200
        status, body = post(server, "two")
        assert post(server, "one")[0] == 200
    assert status == 503 and body["error"]["code"] == "request_id_capacity"
    assert len(session.calls) == 1


def test_mcp_stdio_initializes_lists_and_returns_image_without_base64_text(monkeypatch):
    session = FakeSession()
    with ToolServer(session) as server:
        monkeypatch.setenv("C4G_BRIDGE_URL", server.url)
        monkeypatch.setenv("C4G_BRIDGE_TOKEN", server.token)
        source = io.StringIO("\n".join(json.dumps(item) for item in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "game.observe", "arguments": {}}},
        ]) + "\n")
        output = io.StringIO()
        assert mcp_main(stdin=source, stdout=output) == 0

    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [row["id"] for row in replies] == [1, 2, 3]
    assert len(replies[1]["result"]["tools"]) == 9
    content = replies[2]["result"]["content"]
    assert [part["type"] for part in content] == ["text", "image"]
    assert "data" not in content[0]["text"]
    assert content[1]["mimeType"] == "image/jpeg"


def test_client_script_keeps_stdout_json_only_for_mcp(tmp_path):
    # A notification needs no bridge and must produce no non-JSON chatter.
    script = os.path.abspath(
        "coding4game_gui/runtime_client/client.py")
    proc = subprocess.run(
        [sys.executable, script, "--mcp"],
        input='{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
        text=True, capture_output=True, timeout=5)
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_replay_frames_become_image_attachments_not_base64_text():
    from coding4game_gui.runtime_client.client import _mcp_content
    import base64
    import cv2
    import numpy as np
    _, encoded = cv2.imencode('.png', np.zeros((10, 10, 3), dtype=np.uint8))
    observation = {'mimeType': 'image/png', 'data': base64.b64encode(encoded).decode()}
    content = _mcp_content({'episode_id': 'e', 'frames': [
        {'tic': 0, 'observation': observation}, {'tic': 10, 'observation': observation}]})
    assert sum(item['type'] == 'image' for item in content) == 2
    assert all('data' not in item['text'] for item in content if item['type'] == 'text')


def test_already_encoded_content_is_not_double_wrapped():
    from coding4game_gui.runtime_client.client import _mcp_content
    content = [{'type': 'text', 'text': 'tic 0'},
               {'type': 'image', 'mimeType': 'image/jpeg', 'data': 'AA=='}]
    assert _mcp_content({'content': content}) == content
    assert _mcp_content({'content': content, 'isError': False}) == content


def test_oversized_content_returns_actionable_error():
    from coding4game_gui.runtime_client.client import _mcp_content, BridgeError, MAX_MCP_CONTENT_BYTES
    import pytest
    with pytest.raises(BridgeError, match='fewer replay frames'):
        _mcp_content({'content': [{'type': 'image', 'mimeType': 'image/jpeg',
                                   'data': 'A' * MAX_MCP_CONTENT_BYTES}]})
