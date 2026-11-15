import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coding4game_gui.wsl2.agent_upgrade import FORMAT, load_agent_upgrade


@pytest.fixture
def authorized_upgrade(tmp_path):
    root = tmp_path / "run"
    folder = root / "agent_upgrades" / "update"
    folder.mkdir(parents=True)
    payload = bytearray(64)
    payload[:6] = b"\x7fELF\x02\x01"
    payload[18:20] = (62).to_bytes(2, "little")
    artifact = folder / "claude"
    artifact.write_bytes(payload)
    original = {"mode": "develop", "agent": "claude", "model": "original-model",
                "profile": "original-profile", "tic_budget": 10000, "token_limit": 20000000,
                "wall_seconds": None, "model_budget": {"tokens_used": 123, "tokens_reported": 123,
                "unknown_usage_requests": 2}, "game_budget": {"tics_used": 45}}
    snapshot = folder / "config.before.json"
    snapshot.write_text(json.dumps(original), encoding="utf-8")
    record = {"format": FORMAT, "agent": "claude", "version": "2.1.319", "from_version": "2.1.274",
              "executable": "/opt/c4g-tools/claude", "baseline_executable_sha256": "a" * 64,
              "artifact": artifact.relative_to(root).as_posix(), "sha256": hashlib.sha256(payload).hexdigest(),
              "bytes": len(payload), "source_config": snapshot.relative_to(root).as_posix(),
              "source_config_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
              "session_id": "original-session", "authorization_basis": "The user requested a CLI update.",
              "official_release": "https://github.com/anthropics/claude-code/releases/tag/v2.1.319"}
    config = dict(original, agent_upgrade=record)
    profile = SimpleNamespace(agent=lambda name: "/opt/c4g-tools/claude")
    return root, config, profile, artifact


def test_upgrade_preserves_allowances_unknown_usage_and_original_evidence(authorized_upgrade):
    root, config, profile, _ = authorized_upgrade
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = load_agent_upgrade(root, config, profile, "original-session")
    assert result.record["version"] == "2.1.319"
    assert config["model_budget"] == {"tokens_used": 123, "tokens_reported": 123, "unknown_usage_requests": 2}
    assert config["game_budget"]["tics_used"] == 45
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("field,value", [
    ("version", "2.1.200"), ("sha256", "0" * 64), ("session_id", "different-session"),
    ("source_config_sha256", "0" * 64), ("artifact", "../outside"),
    ("authorization_basis", ""), ("executable", "/bin/sh"), ("agent", "codex"),
])
def test_upgrade_rejects_unbound_or_corrupt_authorization(authorized_upgrade, field, value):
    root, config, profile, _ = authorized_upgrade
    config["agent_upgrade"][field] = value
    with pytest.raises(ValueError):
        load_agent_upgrade(root, config, profile, "original-session")


@pytest.mark.parametrize("field,value", [("token_limit", 30000000), ("tic_budget", 20000),
                                         ("model", "replacement-model"), ("wall_seconds", 3600)])
def test_upgrade_cannot_replace_original_identity_or_limits(authorized_upgrade, field, value):
    root, config, profile, _ = authorized_upgrade
    config[field] = value
    with pytest.raises(ValueError, match="identity or allowance"):
        load_agent_upgrade(root, config, profile, "original-session")


def test_upgrade_cannot_reset_historical_unknown_requests(authorized_upgrade):
    root, config, profile, _ = authorized_upgrade
    config["model_budget"]["unknown_usage_requests"] = 0
    with pytest.raises(ValueError, match="consumed model resources"):
        load_agent_upgrade(root, config, profile, "original-session")


def test_upgrade_cannot_reset_gameplay(authorized_upgrade):
    root, config, profile, _ = authorized_upgrade
    config["game_budget"]["tics_used"] = 0
    with pytest.raises(ValueError, match="consumed gameplay"):
        load_agent_upgrade(root, config, profile, "original-session")


def test_binary_rechecked_after_authorization_before_install(authorized_upgrade):
    root, config, profile, artifact = authorized_upgrade
    verified = load_agent_upgrade(root, config, profile, "original-session")
    artifact.write_bytes(b"changed after preview")
    with pytest.raises(ValueError, match="changed before installation"):
        verified.install_input()


def test_previously_updated_run_cannot_silently_fall_back_to_old_cli(authorized_upgrade):
    root, config, profile, _ = authorized_upgrade
    config.pop("agent_upgrade")
    config["runtime_role"] = {"agent_upgrade": {"version": "2.1.319"}}
    with pytest.raises(ValueError, match="silent CLI downgrade"):
        load_agent_upgrade(root, config, profile, "original-session")


def test_upgrade_retains_original_binary_and_allowance_through_verified_endpoint_migration(authorized_upgrade):
    from coding4game_gui.wsl2.route_provenance import ENDPOINT_GRANT

    root, config, profile, artifact = authorized_upgrade
    record = config["agent_upgrade"]
    before_path = root / record["source_config"]
    original = json.loads(before_path.read_bytes())
    original["upstream_url"] = config["upstream_url"] = "https://provider.invalid/old-prefix"
    before_path.write_text(json.dumps(original))
    record["source_config_sha256"] = hashlib.sha256(before_path.read_bytes()).hexdigest()
    receipt_path = root / "accepted-request.json"
    receipt_path.write_text(json.dumps({
        "http_status": 200, "generation_accepted": True, "complete_stream_verified": True,
        "stream_errors": [], "serving_model": config["model"], "original_run": str(root),
        "original_session": "original-session", "upstream_endpoint": "https://provider.invalid/v1/messages",
        "native_history_modified": False,
    }))
    raw = json.dumps(config).encode()
    grant = {
        "kind": ENDPOINT_GRANT, "run_dir": str(root), "source_config_sha256": hashlib.sha256(raw).hexdigest(),
        "from_upstream_url": config["upstream_url"], "to_upstream_url": "https://provider.invalid",
        **{key: config.get(key) for key in ("agent", "model", "provider_model", "upstream_api")},
        "authorization_basis": "The user authorized this accepted same-provider endpoint.",
        "accepted_request_receipt": receipt_path.name,
        "accepted_request_receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
    }
    segment = root / "recovery/segments/endpoint"
    segment.mkdir(parents=True)
    (segment / "config.before.json").write_bytes(raw)
    grant_raw = json.dumps(grant).encode()
    (segment / "route-authorization.json").write_bytes(grant_raw)
    migration = {"authorization": grant, "authorization_sha256": hashlib.sha256(grant_raw).hexdigest(),
                 "authorization_bytes": len(grant_raw)}
    (segment / "recovery.json").write_text(json.dumps({
        "source_config_sha256": hashlib.sha256(raw).hexdigest(), "route_migration": migration}))
    config["upstream_url"] = grant["to_upstream_url"]
    config["upstream_route_migrations"] = [migration | {"segment": segment.relative_to(root).as_posix()}]
    verified = load_agent_upgrade(root, config, profile, "original-session")
    assert verified.artifact == artifact and verified.record["version"] == "2.1.319"
    assert config["model_budget"]["tokens_reported"] == 123
    (segment / "route-authorization.json").write_text("{}")
    with pytest.raises(ValueError, match="identity or allowance"):
        load_agent_upgrade(root, config, profile, "original-session")
