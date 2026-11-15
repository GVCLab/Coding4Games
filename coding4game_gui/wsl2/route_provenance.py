"""Validate explicit host route grants and their preserved configuration lineage."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit


HOSTNAME_GRANT = "c4g-recovery-route-authorization-v1"
ENDPOINT_GRANT = "c4g-recovery-endpoint-authorization-v1"
GRANT_FIELDS = {"kind", "run_dir", "source_config_sha256", "from_upstream_url",
                "to_upstream_url", "agent", "model", "provider_model", "upstream_api",
                "authorization_basis"}


def _reference(root, value):
    if not isinstance(value, str):
        raise ValueError("Route provenance requires a run-local evidence reference")
    name = PurePosixPath(value)
    if name.is_absolute() or ".." in name.parts or "\\" in value:
        raise ValueError("Route provenance must remain inside the original run")
    path = root / value
    if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
        raise ValueError("Route provenance requires a regular original-run evidence file")
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Route provenance evidence exceeds its byte limit")
    return path


def validate_route_grant(grant, root, raw, config, target, *, session_id=None):
    """Permit one explicit hostname change or one accepted same-origin endpoint change."""
    fields = GRANT_FIELDS | ({"accepted_request_receipt", "accepted_request_receipt_sha256"}
                            if isinstance(grant, dict) and grant.get("kind") == ENDPOINT_GRANT else set())
    if not isinstance(grant, dict) or set(grant) != fields:
        raise ValueError("Recovery route authorization has unexpected or missing fields")
    if (grant["kind"] not in {HOSTNAME_GRANT, ENDPOINT_GRANT}
            or not isinstance(grant["run_dir"], str) or Path(grant["run_dir"]).resolve() != root
            or grant["source_config_sha256"] != hashlib.sha256(raw).hexdigest()):
        raise ValueError("Recovery route authorization does not match the original run/configuration")
    expected = {key: config.get(key) for key in ("agent", "model", "provider_model", "upstream_api")}
    expected.update(from_upstream_url=config.get("upstream_url"), to_upstream_url=target)
    if any(grant[key] != value for key, value in expected.items()):
        raise ValueError("Recovery route authorization does not match the original model and requested route")
    if not isinstance(grant["authorization_basis"], str) or not grant["authorization_basis"].strip():
        raise ValueError("Recovery route authorization requires an explicit operator authorization basis")
    before, after = urlsplit(grant["from_upstream_url"]), urlsplit(target)
    for route in (before, after):
        if (route.scheme != "https" or not route.hostname or route.username or route.password
                or route.query or route.fragment):
            raise ValueError("Authorized recovery origins must use HTTPS without credentials, query or fragment")
    same_port = (before.port or 443) == (after.port or 443)
    if grant["kind"] == HOSTNAME_GRANT:
        if before.hostname == after.hostname or before.path != after.path or not same_port:
            raise ValueError("Recovery route authorization may change only the HTTPS hostname")
        return
    if before.hostname != after.hostname or not same_port or before.path == after.path:
        raise ValueError("Endpoint authorization may change only the same-origin HTTPS endpoint path")
    for path in (before.path, after.path):
        if (not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*/?", path)
                or any(part in {".", ".."} for part in path.split("/"))):
            raise ValueError("Endpoint authorization requires a canonical endpoint path")
    receipt_path = _reference(root, grant["accepted_request_receipt"])
    receipt_raw = receipt_path.read_bytes()
    if hashlib.sha256(receipt_raw).hexdigest() != grant["accepted_request_receipt_sha256"]:
        raise ValueError("Accepted endpoint receipt changed")
    receipt = json.loads(receipt_raw)
    base = target.rstrip("/")
    endpoint = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    if (config.get("agent") != "claude" or receipt.get("http_status") != 200
            or receipt.get("generation_accepted") is not True
            or receipt.get("complete_stream_verified") is not True
            or receipt.get("stream_errors") != []
            or receipt.get("serving_model") != config.get("model")
            or not isinstance(receipt.get("original_run"), str)
            or Path(receipt["original_run"]).resolve() != root
            or receipt.get("upstream_endpoint") != endpoint
            or not session_id or receipt.get("original_session") != session_id
            or receipt.get("native_history_modified") is not False):
        raise ValueError("Endpoint authorization requires accepted complete-request evidence for the original session/model")


def route_identity_preserved(root, before, current, *, session_id):
    """Accept a changed route only through complete, byte-verified migration evidence."""
    if before.get("upstream_url") == current.get("upstream_url"):
        return True
    root = Path(root).resolve()
    previous = before.get("upstream_route_migrations", [])
    migrations = current.get("upstream_route_migrations", [])
    if (not isinstance(previous, list) or not isinstance(migrations, list)
            or migrations[:len(previous)] != previous or len(migrations) <= len(previous)):
        return False
    route = before.get("upstream_url")
    history = list(previous)
    for record in migrations[len(previous):]:
        if not isinstance(record, dict) or set(record) != {
                "authorization", "authorization_sha256", "authorization_bytes", "segment"}:
            return False
        segment = record["segment"]
        snapshot_path = _reference(root, segment + "/config.before.json")
        snapshot_raw = snapshot_path.read_bytes()
        snapshot = json.loads(snapshot_raw)
        grant = record["authorization"]
        if (snapshot.get("upstream_url") != route
                or snapshot.get("upstream_route_migrations", []) != history):
            return False
        grant_path = _reference(root, segment + "/route-authorization.json")
        grant_raw = grant_path.read_bytes()
        if (len(grant_raw) != record["authorization_bytes"]
                or hashlib.sha256(grant_raw).hexdigest() != record["authorization_sha256"]
                or json.loads(grant_raw) != grant):
            return False
        evidence = json.loads(_reference(root, segment + "/recovery.json").read_bytes())
        if (evidence.get("source_config_sha256") != hashlib.sha256(snapshot_raw).hexdigest()
                or evidence.get("route_migration") != {key: value for key, value in record.items() if key != "segment"}):
            return False
        validate_route_grant(grant, root, snapshot_raw, snapshot, grant["to_upstream_url"],
                             session_id=session_id)
        route = grant["to_upstream_url"]
        history.append(record)
    return route == current.get("upstream_url")
