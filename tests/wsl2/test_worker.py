import base64
import io
import socket
import struct
import sys
import threading
import time

import pytest

from coding4game_gui.wsl2 import worker
from coding4game_gui.wsl2.worker import MAX_BYTES, decode, recv


def test_control_socket_accepts_dispatch_after_connect_timeout_elapsed():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0)); listener.listen(1)
        result = {}
        def client():
            with socket.create_connection(listener.getsockname(), timeout=.03) as sock:
                sock.settimeout(None)
                result["value"] = recv(sock)
        thread = threading.Thread(target=client); thread.start()
        connection, _ = listener.accept()
        with connection:
            time.sleep(.08)
            payload = b'{"op":"poll","args":{}}'
            connection.sendall(struct.pack("!I", len(payload)) + payload)
        thread.join(1)
        assert not thread.is_alive()
        assert result["value"] == {"op": "poll", "args": {}}


class FakeControlSocket:
    def __init__(self): self.replies=[]
    def __enter__(self): return self
    def __exit__(self,*exc): pass
    def settimeout(self,value): pass


def run_requests(monkeypatch,tmp_path,requests):
    control=FakeControlSocket(); pending=iter(requests)
    original_stat=worker.Path.stat
    class SafeRootStat:
        def __init__(self,value): self.st_uid=1000; self.st_mode=value.st_mode & ~0o077
    def stat(path,*args,**kwargs):
        value=original_stat(path,*args,**kwargs)
        return SafeRootStat(value) if path==tmp_path else value
    monkeypatch.setattr(worker.Path,"stat",stat)
    monkeypatch.setattr(type(tmp_path),"is_mount",lambda path:True)
    monkeypatch.setattr(worker.socket,"create_connection",lambda *a,**k:control)
    monkeypatch.setattr(worker,"recv",lambda sock:next(pending))
    monkeypatch.setattr(worker,"send",lambda sock,value:control.replies.append(value))
    monkeypatch.setattr(worker.os,"getuid",lambda:1000,raising=False)
    monkeypatch.setattr(worker.os,"readlink",lambda path:"mnt:[2]")
    monkeypatch.setattr(sys,"argv",["worker.py","51001",str(tmp_path)])
    monkeypatch.setattr(sys,"stdin",io.StringIO("test-token\n"))
    with pytest.raises(StopIteration): worker.main()
    return control.replies[1:]


def test_chunked_artifact_write_and_read_round_trip(monkeypatch,tmp_path):
    payload=b"chunked artifact"
    replies=run_requests(monkeypatch,tmp_path,[
        {"op":"write_file_chunk","args":{"path":"deps/wheel.whl","offset":0,
                                             "data":base64.b64encode(payload).decode()}},
        {"op":"read_file_chunk","args":{"path":"deps/wheel.whl","offset":2,"size":5}},
    ])
    assert replies==[
        {"ok":True,"value":{"bytes":len(payload)}},
        {"ok":True,"value":{"bytes":len(payload),"data":base64.b64encode(payload[2:7]).decode()}},
    ]


def test_connection_refresh_preserves_transcript_and_rejects_other_files(monkeypatch,tmp_path):
    folder=tmp_path/".claude"; folder.mkdir()
    history=folder/"history.jsonl"; history.write_bytes(b"original session history")
    settings=folder/"settings.json"; settings.write_bytes(b"old settings")
    encode=lambda value:base64.b64encode(value).decode()
    replies=run_requests(monkeypatch,tmp_path,[
        {"op":"refresh_connection","args":{"name":".claude","files":{
            "settings.json":encode(b"fresh scoped configuration")}}},
        {"op":"refresh_connection","args":{"name":".claude","files":{
            "history.jsonl":encode(b"replacement history")}}},
        {"op":"stage","args":{"name":".claude","files":{
            "settings.json":encode(b"ordinary staging must reject an existing directory")}}},
    ])
    assert replies[0]["ok"] is True
    assert replies[1]["ok"] is False and replies[2]["ok"] is False
    assert settings.read_bytes()==b"fresh scoped configuration"
    assert history.read_bytes()==b"original session history"


@pytest.mark.parametrize("op,args",[
    ("read_file_chunk",{"path":"missing","offset":0,"size":1}),
    ("read_file_chunk",{"path":"file","offset":True,"size":1}),
    ("read_file_chunk",{"path":"file","offset":-1,"size":1}),
    ("read_file_chunk",{"path":"file","offset":0,"size":0}),
    ("read_file_chunk",{"path":"file","offset":0,"size":1024*1024+1}),
    ("write_file_chunk",{"path":"file","offset":True,"data":"eA=="}),
    ("write_file_chunk",{"path":"file","offset":-1,"data":"eA=="}),
    ("write_file_chunk",{"path":"file","offset":0,"data":""}),
    ("write_file_chunk",{"path":"file","offset":2*1024**3,"data":"eA=="}),
    ("write_file_chunk",{"path":"../escape","offset":0,"data":"eA=="}),
])
def test_chunked_artifact_rejects_bad_types_bounds_and_paths(monkeypatch,tmp_path,op,args):
    (tmp_path/"file").write_bytes(b"x")
    reply=run_requests(monkeypatch,tmp_path,[{"op":op,"args":args}])[0]
    assert reply["ok"] is False
    assert "ValueError" in reply["error"]


def test_chunked_artifact_requires_contiguous_append(monkeypatch,tmp_path):
    (tmp_path/"file").write_bytes(b"abc")
    reply=run_requests(monkeypatch,tmp_path,[{
        "op":"write_file_chunk","args":{"path":"file","offset":2,"data":"eA=="}}])[0]
    assert reply=={"ok":False,"error":"ValueError: artifact write offset mismatch"}
    assert (tmp_path/"file").read_bytes()==b"abc"


def test_regular_code_bundle_retains_independent_64_mib_limit(monkeypatch):
    assert MAX_BYTES==64*1024*1024
    assert decode({"policy.py":base64.b64encode(b"pass\n").decode()})=={"policy.py":b"pass\n"}
    class Oversized:
        def __len__(self): return MAX_BYTES+1
    monkeypatch.setattr(worker.base64,"b64decode",lambda *a,**k:Oversized())
    with pytest.raises(ValueError,match="bundle too large"): decode({"policy.py":"ignored"})


def test_cli_links_reject_root_bundle_but_allow_separate_source(monkeypatch,tmp_path):
    source=tmp_path/"submission"; source.mkdir()
    (source/"main.py").write_bytes(b"pass\n")
    helpers=tmp_path/".codex"/"tmp"; helpers.mkdir(parents=True)
    link=helpers/"apply_patch"; link.write_bytes(b"CLI helper link fixture")
    original_is_symlink=worker.Path.is_symlink
    monkeypatch.setattr(worker.Path,"is_symlink",
                        lambda path:path==link or original_is_symlink(path))
    rejected,saved=run_requests(monkeypatch,tmp_path,[
        {"op":"read_tree","args":{"path":"."}},
        {"op":"read_tree","args":{"path":"submission"}},
    ])
    assert rejected["ok"] is False
    assert ".codex/tmp/apply_patch" in rejected["error"]
    assert "dedicated source directory" in rejected["error"]
    assert saved=={"ok":True,"value":{"files":{"main.py":"cGFzcwo="}}}


def _poll_cli_trace(monkeypatch, tmp_path, *, streaming):
    """Exercise the real worker dispatch with drained, image-bearing CLI output."""
    from types import SimpleNamespace

    payload = (b'{"type":"item.completed","item":{"type":"mcp_tool_call",'
               b'"result":{"content":[{"type":"image","data":"'
               + b'A' * 64 + b'"}]}}}\n') * 2
    assert len(payload) // 2 <= 256 < len(payload)
    buffers = {11: payload, 12: b""}
    killed = []
    proc = SimpleNamespace(pid=42, stdout=SimpleNamespace(fileno=lambda: 11),
                           stderr=SimpleNamespace(fileno=lambda: 12))
    proc.poll = lambda: -9 if killed else (None if buffers[11] else 0)
    original_read_text = worker.Path.read_text
    monkeypatch.setattr(worker.Path, "read_text", lambda path, *a, **k:
                        "42 (cli) T" if str(path).replace("\\", "/") == "/proc/42/stat"
                        else original_read_text(path, *a, **k))
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(worker.os, "set_blocking", lambda *a: None, raising=False)
    monkeypatch.setattr(worker.os, "killpg", lambda pid, sig: killed.append((pid, sig)), raising=False)
    monkeypatch.setattr(worker.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(worker.uuid, "uuid4", lambda: SimpleNamespace(hex="cli-child"))
    monkeypatch.setattr(worker, "MAX_OUTPUT", 256)

    def read(fd, size):
        value, buffers[fd] = buffers[fd][:size], buffers[fd][size:]
        return value

    monkeypatch.setattr(worker.os, "read", read)
    args = {"argv": ["/opt/c4g-tools/codex"], "cwd": "."}
    if streaming:
        args["output_mode"] = "stream"
    replies = run_requests(monkeypatch, tmp_path, [
        {"op": "launch", "args": args},
        *[{"op": "poll", "args": {"handle": "cli-child", "max_events": 1}}
          for _ in range(3)],
    ])
    assert all(reply["ok"] for reply in replies), replies
    return payload, [reply["value"] for reply in replies[1:]], killed


def test_managed_cli_stream_survives_cumulative_image_output_limit(monkeypatch, tmp_path):
    payload, polls, killed = _poll_cli_trace(monkeypatch, tmp_path, streaming=True)
    assert not any(poll["output_overflow"] for poll in polls)
    assert killed == []
    assert b"".join(event["data"].encode() for poll in polls for event in poll["events"]) == payload
    assert all(sum(len(event["data"].encode()) for event in poll["events"]) <= 256
               for poll in polls)
    assert polls[-1]["output_complete"] is True


def test_submitted_program_output_retains_cumulative_limit(monkeypatch, tmp_path):
    _payload, polls, killed = _poll_cli_trace(monkeypatch, tmp_path, streaming=False)
    assert any(poll["output_overflow"] for poll in polls)
    assert killed


@pytest.mark.parametrize("mode", [None, True, "unlimited", ""])
def test_launch_rejects_unknown_output_modes_before_starting(monkeypatch, tmp_path, mode):
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    reply = run_requests(monkeypatch, tmp_path, [{"op": "launch", "args": {
        "argv": ["/bin/anything"], "cwd": ".", "output_mode": mode}}])[0]
    assert reply == {"ok": False, "error": "ValueError: invalid output mode"}
