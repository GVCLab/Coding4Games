"""Validation and content-addressed storage for frozen WSL dependencies."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


FORMAT = "c4g-wsl-env-lock-v1"
MAX_REQUIREMENTS = 128
MAX_WHEELS = 512
MAX_ASSETS = 256
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
MAX_PLAN_BYTES = 4 * 1024 * 1024 * 1024

_HASH = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_EXTRA = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_VERSION = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.!+_-]*[A-Za-z0-9])?")
_PYTHON_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}")
_PLATFORM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_REQUIREMENT = re.compile(
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?:\[(?P<extras>[A-Za-z0-9._-]+(?:,[A-Za-z0-9._-]+)*)\])?"
    r"==(?P<version>[A-Za-z0-9](?:[A-Za-z0-9.!+_-]*[A-Za-z0-9])?)"
)


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{label} has unsupported or missing fields")
    return value


def _digest(value, label="SHA-256"):
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")
    return value


def _distribution_name(value):
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise ValueError("invalid distribution name")
    return re.sub(r"[-_.]+", "-", value).lower()


def _version(value):
    if not isinstance(value, str) or len(value) > 128 or _VERSION.fullmatch(value) is None:
        raise ValueError("invalid distribution version")
    return value


def _requirement(value):
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError("invalid requirement")
    match = _REQUIREMENT.fullmatch(value)
    if match is None:
        raise ValueError("requirements must be exact named versions")
    name = _distribution_name(match.group("name"))
    extras = match.group("extras")
    suffix = ""
    if extras is not None:
        normalized = []
        for extra in extras.split(","):
            if _EXTRA.fullmatch(extra) is None:
                raise ValueError("invalid requirement extra")
            item = re.sub(r"[-_.]+", "-", extra).lower()
            if item in normalized:
                raise ValueError("duplicate requirement extra")
            normalized.append(item)
        suffix = "[" + ",".join(sorted(normalized)) + "]"
    return f"{name}{suffix}=={_version(match.group('version'))}"


def _public_https_url(value, label):
    if (not isinstance(value, str) or len(value) > 2048 or "\\" in value
            or any(character.isspace() or ord(character) < 32 for character in value)):
        raise ValueError(f"invalid {label} URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid {label} URL") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError(f"invalid {label} URL")
    host = parsed.hostname.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is None and (host == "localhost" or host.endswith(".localhost")
                            or "." not in host or all(
                                part.isdigit() for part in host.split("."))):
        raise ValueError(f"{label} URL must use a public host")
    if address is not None and not address.is_global:
        raise ValueError(f"{label} URL must use a public host")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"invalid {label} URL")
    return value


def _safe_relative_path(value, label="asset path"):
    if (not isinstance(value, str) or not value or len(value) > 512
            or "\\" in value or "\x00" in value):
        raise ValueError(f"invalid {label}")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(
            part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"invalid {label}")
    return value


def _guest_source_path(value):
    if (not isinstance(value, str) or not value or len(value) > 512
            or "\\" in value or "\x00" in value):
        raise ValueError("invalid guest source path")
    path = PurePosixPath(value)
    if (not path.is_absolute() or value != path.as_posix()
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise ValueError("invalid guest source path")
    return value


def _asset(value, *, locked):
    if locked:
        _object(value, {"path", "url", "sha256", "bytes"}, "asset")
        url = value["url"]
        if url is not None:
            url = _public_https_url(url, "asset")
        result = {"path": _safe_relative_path(value["path"]), "url": url,
                  "sha256": _digest(value["sha256"], "asset SHA-256")}
    else:
        if not isinstance(value, dict) or not {"path", "sha256"} <= set(value) \
                or not set(value) <= {"path", "url", "source_path", "sha256"} \
                or ("url" in value) == ("source_path" in value):
            raise ValueError("asset requires exactly one source")
        result = {"path": _safe_relative_path(value["path"]),
                  "sha256": _digest(value["sha256"], "asset SHA-256")}
        if "url" in value:
            result["url"] = _public_https_url(value["url"], "asset")
        else:
            result["source_path"] = _guest_source_path(value["source_path"])
    if locked:
        size = value["bytes"]
        if type(size) is not int or not 0 < size <= MAX_ARTIFACT_BYTES:
            raise ValueError("invalid asset size")
        result["bytes"] = size
    return result


def _reject_path_collisions(paths):
    seen = set()
    for value in paths:
        folded = value.casefold()
        parts = PurePosixPath(folded).parts
        if folded in seen or any("/".join(parts[:index]) in seen
                                 for index in range(1, len(parts))):
            raise ValueError("duplicate or colliding artifact path")
        if any(other.startswith(folded + "/") for other in seen):
            raise ValueError("duplicate or colliding artifact path")
        seen.add(folded)


def normalize_environment(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or not set(value) <= {"requirements", "indexes", "assets"}:
        raise ValueError("environment has unsupported fields")
    raw_requirements = value.get("requirements", [])
    raw_indexes = value.get("indexes", ["https://pypi.org/simple"])
    raw_assets = value.get("assets", [])
    if not isinstance(raw_requirements, list) or len(raw_requirements) > MAX_REQUIREMENTS:
        raise ValueError("invalid requirements list")
    if not isinstance(raw_indexes, list) or not 1 <= len(raw_indexes) <= 8:
        raise ValueError("invalid indexes list")
    if not isinstance(raw_assets, list) or len(raw_assets) > MAX_ASSETS:
        raise ValueError("invalid assets list")
    requirements = [_requirement(item) for item in raw_requirements]
    indexes = [_public_https_url(item, "index") for item in raw_indexes]
    assets = [_asset(item, locked=False) for item in raw_assets]
    names = [item.split("[", 1)[0].split("==", 1)[0] for item in requirements]
    if len(set(names)) != len(names):
        raise ValueError("duplicate requirement")
    if len(set(indexes)) != len(indexes):
        raise ValueError("duplicate index")
    _reject_path_collisions(item["path"] for item in assets)
    return {"requirements": requirements, "indexes": indexes, "assets": assets}


def validate_lock(lock, baseline_sha256=None):
    keys = {"format", "baseline_sha256", "python", "platform", "requirements",
            "indexes", "wheels", "assets"}
    _object(lock, keys, "dependency lock")
    if lock["format"] != FORMAT:
        raise ValueError("unsupported dependency lock format")
    baseline = _digest(lock["baseline_sha256"], "baseline SHA-256")
    if baseline_sha256 is not None and baseline != _digest(
            baseline_sha256, "expected baseline SHA-256"):
        raise ValueError("dependency lock baseline does not match")
    if not isinstance(lock["python"], str) or _PYTHON_VERSION.fullmatch(lock["python"]) is None:
        raise ValueError("invalid Python version")
    if not isinstance(lock["platform"], str) or _PLATFORM.fullmatch(lock["platform"]) is None:
        raise ValueError("invalid platform tag")
    environment = normalize_environment({"requirements": lock["requirements"],
                                         "indexes": lock["indexes"], "assets": []})
    raw_wheels, raw_assets = lock["wheels"], lock["assets"]
    if (not isinstance(raw_wheels, list) or len(raw_wheels) > MAX_WHEELS
            or (environment["requirements"] and not raw_wheels)):
        raise ValueError("invalid wheels list")
    if not isinstance(raw_assets, list) or len(raw_assets) > MAX_ASSETS:
        raise ValueError("invalid assets list")
    wheels = []
    for raw in raw_wheels:
        _object(raw, {"filename", "name", "version", "sha256", "bytes"}, "wheel")
        filename = _safe_relative_path(raw["filename"], "wheel filename")
        if "/" in filename or not filename.endswith(".whl"):
            raise ValueError("invalid wheel filename")
        size = raw["bytes"]
        if type(size) is not int or not 0 < size <= MAX_ARTIFACT_BYTES:
            raise ValueError("invalid wheel size")
        wheels.append({"filename": filename, "name": _distribution_name(raw["name"]),
                       "version": _version(raw["version"]),
                       "sha256": _digest(raw["sha256"], "wheel SHA-256"), "bytes": size})
    assets = [_asset(item, locked=True) for item in raw_assets]
    _reject_path_collisions([item["filename"] for item in wheels])
    _reject_path_collisions(item["path"] for item in assets)
    wheel_names = [item["name"] for item in wheels]
    if len(set(wheel_names)) != len(wheel_names):
        raise ValueError("duplicate locked distribution")
    versions = {item["name"]: item["version"] for item in wheels}
    for requirement in environment["requirements"]:
        requested, version = requirement.rsplit("==", 1)
        requested = requested.split("[", 1)[0]
        if versions.get(requested) != version:
            raise ValueError("locked wheel does not match requested version")
    total = sum(item["bytes"] for item in wheels) + sum(item["bytes"] for item in assets)
    if total > MAX_PLAN_BYTES:
        raise ValueError("dependency lock exceeds total artifact limit")
    result = {"format": FORMAT, "baseline_sha256": baseline, "python": lock["python"],
              "platform": lock["platform"], "requirements": environment["requirements"],
              "indexes": environment["indexes"], "wheels": wheels, "assets": assets}
    return result


def lock_id(lock):
    canonical = json.dumps(validate_lock(lock), sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ArtifactCache:
    """A host-owned SHA-256 blob store with atomic, streaming writes."""

    def __init__(self, root):
        self.root = Path(root)
        self.blobs = self.root / "blobs"

    def path(self, digest):
        return self.blobs / _digest(digest, "artifact SHA-256")

    def contains(self, digest, size):
        digest = _digest(digest, "artifact SHA-256")
        if type(size) is not int or not 0 < size <= MAX_ARTIFACT_BYTES:
            raise ValueError("invalid artifact size")
        path = self.path(digest)
        if not path.exists():
            return False
        if not path.is_file() or path.stat().st_size != size:
            raise ValueError("cached artifact size mismatch")
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != digest:
            raise ValueError("cached artifact hash mismatch")
        return True

    def put(self, digest, size, chunks):
        digest = _digest(digest, "artifact SHA-256")
        if type(size) is not int or not 0 < size <= MAX_ARTIFACT_BYTES:
            raise ValueError("invalid artifact size")
        if self.contains(digest, size):
            return self.path(digest)
        self.blobs.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp",
                                                  dir=self.blobs)
        temporary = Path(temporary)
        hasher, written = hashlib.sha256(), 0
        try:
            with os.fdopen(descriptor, "wb") as stream:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise ValueError("artifact chunks must be bytes")
                    written += len(chunk)
                    if written > size:
                        raise ValueError("artifact size mismatch")
                    hasher.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if written != size:
                raise ValueError("artifact size mismatch")
            if hasher.hexdigest() != digest:
                raise ValueError("artifact hash mismatch")
            os.replace(temporary, self.path(digest))
            if not self.contains(digest, size):
                raise ValueError("cached artifact verification failed")
            return self.path(digest)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
