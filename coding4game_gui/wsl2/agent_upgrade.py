"""Install an explicitly authorized CLI update without changing a run's baseline."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid


FORMAT = "c4g-run-agent-upgrade-v1"
MAX_BINARY_BYTES = 512 * 1024 * 1024
IDENTITY_FIELDS = (
    "mode", "runtime_backend", "profile", "runtime_profile_sha256", "agent", "model",
    "scenario", "windows_input", "tic_budget", "token_limit", "max_tics", "wall_seconds",
    "original_started_unix", "token_accounting", "minimum_development_fraction",
    "reasoning_effort", "upstream_url", "upstream_api", "provider_model", "model_proxy",
    "scenario_protocol_sha256", "task_prompt_sha256",
)


def _digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _reference(root, relative):
    if not isinstance(relative, str):
        raise ValueError("Invalid agent upgrade evidence reference")
    name = PurePosixPath(relative)
    if name.is_absolute() or ".." in name.parts or "\\" in relative:
        raise ValueError("Agent upgrade evidence must remain inside the original run")
    path = root / relative
    if path.is_symlink() or root not in path.resolve().parents or not path.is_file():
        raise ValueError("Agent upgrade evidence must be a regular run-local file")
    return path


@dataclass(frozen=True)
class VerifiedAgentUpgrade:
    record: dict
    artifact: Path

    def install_input(self):
        payload = self.artifact.read_bytes()
        if (len(payload) != self.record["bytes"] or
                hashlib.sha256(payload).hexdigest() != self.record["sha256"]):
            raise ValueError("Agent upgrade binary changed before installation")
        return json.dumps(self.record).encode() + b"\n" + payload


def load_agent_upgrade(root, config, profile, session_id):
    """Validate host authorization, preserved identity and the exact executable bytes."""
    record = config.get("agent_upgrade")
    if record is None:
        if (config.get("runtime_role") or {}).get("agent_upgrade"):
            raise ValueError("Recorded agent upgrade authorization is missing; refusing a silent CLI downgrade")
        return None
    fields = {"format", "agent", "version", "from_version", "executable", "baseline_executable_sha256",
              "artifact", "sha256", "bytes", "source_config", "source_config_sha256",
              "session_id", "authorization_basis", "official_release"}
    if not isinstance(record, dict) or set(record) != fields or record.get("format") != FORMAT:
        raise ValueError("Invalid agent upgrade authorization")
    if (record["agent"] != "claude" or config.get("agent") != "claude" or
            record["executable"] != str(profile.agent("claude")) or
            record["executable"] != "/opt/c4g-tools/claude" or record["session_id"] != session_id):
        raise ValueError("Agent upgrade does not match the original CLI/session")
    for name in ("version", "from_version"):
        if not isinstance(record[name], str) or not re.fullmatch(r"\d+\.\d+\.\d+", record[name]):
            raise ValueError("Invalid agent upgrade version")
    if tuple(map(int, record["version"].split("."))) <= tuple(map(int, record["from_version"].split("."))):
        raise ValueError("Agent upgrade must select a newer CLI version")
    for name in ("sha256", "source_config_sha256", "baseline_executable_sha256"):
        if not isinstance(record[name], str) or not re.fullmatch(r"[0-9a-f]{64}", record[name]):
            raise ValueError("Invalid agent upgrade digest")
    if (type(record["bytes"]) is not int or not 20 <= record["bytes"] <= MAX_BINARY_BYTES or
            not isinstance(record["authorization_basis"], str) or not record["authorization_basis"].strip() or
            record["official_release"] != "https://github.com/anthropics/claude-code/releases/tag/v" + record["version"]):
        raise ValueError("Invalid agent upgrade provenance")
    root = Path(root).resolve()
    before_path = _reference(root, record["source_config"])
    if before_path.stat().st_size > 16 * 1024 * 1024 or _digest(before_path) != record["source_config_sha256"]:
        raise ValueError("Agent upgrade source configuration changed")
    before = json.loads(before_path.read_bytes())
    from .route_provenance import route_identity_preserved
    if (any(before.get(key) != config.get(key) for key in IDENTITY_FIELDS if key != "upstream_url")
            or not route_identity_preserved(root, before, config, session_id=session_id)):
        raise ValueError("Agent upgrade cannot change the original run identity or allowance")
    for key in ("tokens_used", "tokens_reported", "unknown_usage_requests"):
        if config["model_budget"][key] < before["model_budget"][key]:
            raise ValueError("Agent upgrade cannot discard consumed model resources")
    if config["game_budget"]["tics_used"] < before["game_budget"]["tics_used"]:
        raise ValueError("Agent upgrade cannot discard consumed gameplay")
    artifact = _reference(root, record["artifact"])
    if artifact.stat().st_size != record["bytes"] or _digest(artifact) != record["sha256"]:
        raise ValueError("Agent upgrade binary hash or byte count mismatch")
    with artifact.open("rb") as stream:
        header = stream.read(20)
    if header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != 62:
        raise ValueError("Agent upgrade requires an official Linux x64 ELF executable")
    return VerifiedAgentUpgrade(dict(record), artifact)


def prepare_agent_upgrade(run, binary, *, version, from_version, sha256,
                          baseline_sha256, authorization_basis, execute=False):
    """Record a paid-request-free update under the original exclusive profile lock."""
    import psutil
    from .native import WslNative
    from .profile import WslProfile
    from .runtime import WslRuntime

    run, binary = Path(run).resolve(), Path(binary).resolve()
    path = run / "config.json"
    raw = path.read_bytes()
    config = json.loads(raw)
    if (config.get("mode") != "develop" or config.get("runtime_backend") != "wsl2" or
            config.get("agent") != "claude" or config.get("status") not in {"failed", "incomplete"} or
            (run / "programs/final.json").exists()):
        raise ValueError("Agent upgrade requires a closed unselected Claude development")
    profile = WslProfile.load(config["profile"])
    sessions = set()
    for line in (run / "claude_events.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("type") == "system" and event.get("subtype") == "init":
            sessions.add(event.get("session_id"))
    if len(sessions) != 1 or not next(iter(sessions)):
        raise ValueError("Agent upgrade requires one recorded official CLI session")
    plan = {"run_dir": str(run), "from_version": from_version, "version": version,
            "sha256": sha256, "session_id": next(iter(sessions)), "model_calls": 0,
            "baseline_unchanged": True, "allowances_unchanged": True}
    if not execute:
        return plan
    if binary.is_symlink() or not binary.is_file() or _digest(binary) != sha256:
        raise ValueError("Agent upgrade source binary hash mismatch")
    native = WslNative()
    with WslRuntime(profile, native=native) as runtime:
        runtime._check_baseline()
        owner = config.get("host_owner")
        if not isinstance(owner, dict) or set(owner) != {"pid", "created"}:
            raise ValueError("Agent upgrade requires the recorded host owner identity")
        try:
            if psutil.Process(owner["pid"]).create_time() == owner["created"]:
                raise ValueError("Agent upgrade refuses a live development owner")
        except psutil.NoSuchProcess:
            pass
        if any(name.startswith(f"C4G-WSL2-Run-{profile.id}-") for name in native.list_distros()):
            raise ValueError("Agent upgrade refuses retained registered run clones")
        if path.read_bytes() != raw:
            raise ValueError("Development changed during agent upgrade preparation")
        directory = run / "agent_upgrades" / uuid.uuid4().hex
        directory.mkdir(parents=True)
        with (directory / "config.before.json").open("xb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        with binary.open("rb") as source, (directory / "claude").open("xb") as target:
            shutil.copyfileobj(source, target, 1024 * 1024)
            target.flush(); os.fsync(target.fileno())
        record = {"format": FORMAT, "agent": "claude", "version": version, "from_version": from_version,
                  "executable": str(profile.agent("claude")), "baseline_executable_sha256": baseline_sha256,
                  "artifact": (directory / "claude").relative_to(run).as_posix(),
                  "sha256": sha256, "bytes": binary.stat().st_size,
                  "source_config": (directory / "config.before.json").relative_to(run).as_posix(),
                  "source_config_sha256": hashlib.sha256(raw).hexdigest(), "session_id": plan["session_id"],
                  "authorization_basis": authorization_basis,
                  "official_release": "https://github.com/anthropics/claude-code/releases/tag/v" + version}
        updated = dict(config, agent_upgrade=record)
        load_agent_upgrade(run, updated, profile, plan["session_id"])
        (directory / "authorization.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        temporary = path.with_suffix(".upgrade.tmp")
        temporary.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
        if path.read_bytes() != raw:
            raise ValueError("Development changed before agent upgrade commit")
        temporary.replace(path)
    return plan | {"authorization": str(directory / "authorization.json")}


INSTALL_AGENT_UPGRADE = r'''
import hashlib, json, os, pathlib, stat, subprocess, sys
record=json.loads(sys.stdin.buffer.readline())
target=pathlib.Path('/opt/c4g-tools/claude')
info=target.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022:
    raise RuntimeError('Agent upgrade target must be a root-owned read-only regular executable')
with target.open('rb') as stream: original=hashlib.file_digest(stream,'sha256').hexdigest()
if original!=record['baseline_executable_sha256']:
    raise RuntimeError('Agent upgrade target differs from the authorized baseline executable')
temporary=target.with_name('.claude-upgrade-'+record['sha256'][:16])
with temporary.open('xb') as stream:
    os.chmod(temporary,0o700)
    digest=hashlib.sha256();size=0
    while block:=sys.stdin.buffer.read(1024*1024):
        size+=len(block)
        if size>record['bytes']: raise RuntimeError('Agent upgrade input exceeds its pinned byte count')
        stream.write(block);digest.update(block)
    stream.flush();os.fsync(stream.fileno())
if size!=record['bytes'] or digest.hexdigest()!=record['sha256']:
    raise RuntimeError('Agent upgrade input hash mismatch')
environment=dict(os.environ,DISABLE_AUTOUPDATER='1',CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
version=subprocess.run([str(temporary),'--version'],check=True,capture_output=True,text=True,
                       timeout=30,env=environment).stdout.strip().split()[0]
if version!=record['version']: raise RuntimeError('Agent upgrade executable version mismatch')
os.chmod(temporary,0o755);os.replace(temporary,target)
print(json.dumps({'agent':'claude','version':version,'sha256':digest.hexdigest(),'bytes':size,
                  'baseline_executable_sha256':original,'executable':str(target),'model_calls':0}))
'''
