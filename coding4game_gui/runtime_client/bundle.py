"""Content-addressed multi-file submissions, with no host-side code loading."""
from __future__ import annotations

import base64
import re
from pathlib import PurePosixPath

MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_FILES = 4096
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)


def filename(name):
    if not isinstance(name, str) or not name or len(name) > 240:
        raise ValueError("invalid bundle filename")
    parts = name.split("/")
    if any(p in ("", ".", "..") or p.endswith((" ", "."))
           or _RESERVED.match(p) for p in parts):
        raise ValueError(f"unsafe bundle filename: {name!r}")
    if re.search(r'[\\:*?"<>|\x00-\x1f]', name) or PurePosixPath(name).is_absolute():
        raise ValueError(f"unsafe bundle filename: {name!r}")
    return name


def decode_files(encoded):
    if not isinstance(encoded, dict) or not 0 < len(encoded) <= MAX_FILES:
        raise ValueError("bundle must contain 1..4096 files")
    result, seen, total = {}, set(), 0
    for key, value in encoded.items():
        name = filename(key)
        folded = name.casefold()
        if folded in seen or not isinstance(value, str):
            raise ValueError("duplicate Windows filename or non-base64 content")
        seen.add(folded)
        data = base64.b64decode(value, validate=True)
        total += len(data)
        if total > MAX_BUNDLE_BYTES:
            raise ValueError("submission exceeds 64 MiB; use the pinned runtime for dependencies")
        result[name] = data
    for name in seen:
        parts = name.split("/")
        if any("/".join(parts[:n]) in seen for n in range(1, len(parts))):
            raise ValueError("file/directory collision in bundle")
    return result


def encode_files(files):
    return {filename(k): base64.b64encode(v).decode("ascii") for k, v in files.items()}
