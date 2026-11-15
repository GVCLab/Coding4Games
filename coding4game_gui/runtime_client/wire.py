"""Bounded JSON framing shared by the host and the standalone sandbox worker.

Never use pickle/multiprocessing.recv across this trust boundary.
"""
from __future__ import annotations

import json
import socket
import struct
import time

MAX_MESSAGE = 96 * 1024 * 1024


def _read(sock, count, deadline):
    chunks = bytearray()
    while len(chunks) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("sandbox response deadline exceeded")
        sock.settimeout(remaining)
        data = sock.recv(min(count - len(chunks), 1024 * 1024))
        if not data:
            raise EOFError("sandbox connection closed")
        chunks.extend(data)
    return bytes(chunks)


def receive(sock, timeout=60.0):
    deadline = time.monotonic() + timeout
    length = struct.unpack("!I", _read(sock, 4, deadline))[0]
    if not 0 < length <= MAX_MESSAGE:
        raise ValueError("sandbox message exceeds protocol limit")
    value = json.loads(_read(sock, length, deadline),
                       parse_constant=lambda s: (_ for _ in ()).throw(
                           ValueError(f"non-finite JSON: {s}")))
    if not isinstance(value, dict):
        raise ValueError("sandbox message must be a JSON object")
    return value


def send(sock, value, timeout=60.0):
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if not 0 < len(payload) <= MAX_MESSAGE:
        raise ValueError("sandbox message exceeds protocol limit")
    sock.settimeout(timeout)
    sock.sendall(struct.pack("!I", len(payload)) + payload)
