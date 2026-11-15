"""Validate continuation of a closed development without granting a new run."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import time
import uuid

from coding4game_gui.benchmark.code.recovery_accounting import (
    read_gateway_journal, reconstruct_usage, recover_deadline,
)
from coding4game_gui.benchmark.code.submissions import ProgramStore
from coding4game_gui.benchmark.evidence import episode_journal
from coding4game_gui.wsl2.recovery import verify_workspace_archive
import tarfile


def protocol_hash(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, default=str,
                                     separators=(",", ":")).encode()).hexdigest()


@dataclass
class DevelopmentRecovery:
    root: Path
    config: dict
    original_config: bytes
    descriptor: dict
    usage: dict
    session_id: str
    deadline: float
    prompt: str
    source_journals: list
    evaluations: list
    route_migration: dict | None = None
    selection_continuation: dict | None = None
    budget_notice: dict | None = None
    agent_upgrade: object | None = None
    gateway_compatibility: dict | None = None
    gateway_tool_replay: dict | None = None
    tool_replay_sources: list = field(default_factory=list, repr=False)

    def evidence(self):
        return {"source_config_sha256": hashlib.sha256(self.original_config).hexdigest(),
                "archive_sha256": self.descriptor["sha256"],
                "session_id": self.session_id, "usage_checkpoint": self.usage,
                "source_gateway_journals": self.source_journals,
                "recorded_programme_evaluations": len(self.evaluations),
                "route_origin_recorded": bool(self.config.get("upstream_url")),
                "route_migration": self.route_migration,
                "selection_continuation": self.selection_continuation,
                "budget_notice": self.budget_notice,
                "agent_upgrade": self.agent_upgrade.record if self.agent_upgrade is not None else None,
                "gateway_compatibility": self.gateway_compatibility,
                 "gateway_tool_replay": self.gateway_tool_replay,
                 "tool_replay_provenance": {
                     "calls": len(self.tool_replay_sources),
                     "sha256": hashlib.sha256(json.dumps(self.tool_replay_sources, sort_keys=True,
                         separators=(",", ":")).encode()).hexdigest()},
                "stop_on_http_400": self.config.get("model_gateway_stop_on_http_400", False),
                "original_deadline_unix": (None if self.config["wall_seconds"] is None else
                    self.config["original_started_unix"] + self.config["wall_seconds"])}


def _route_migration(args, root, raw, config):
    """Require a host operator grant bound to this exact closed configuration.

    This is an explicit origin change, not proof of provider ownership. Model,
    protocol, path, proxy and resource identity retain their ordinary guards.
    """
    value = getattr(args, "recovery_route_authorization", None)
    previous = config.get("upstream_url")
    changed = previous and args.upstream_url != previous
    if not value:
        if changed:
            raise ValueError("Recovery cannot replace the original upstream route without explicit authorization")
        return None
    if not changed:
        raise ValueError("Route authorization requires a recorded original upstream route change")
    path = Path(value)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("Recovery route authorization must be a regular host file of at most 16384 bytes")
    payload = path.read_bytes()
    if len(payload) > 16384:
        raise ValueError("Recovery route authorization exceeds 16384 bytes")
    try:
        grant = json.loads(payload)
    except (ValueError, UnicodeError) as error:
        raise ValueError("Invalid recovery route authorization JSON") from error
    from coding4game_gui.wsl2.route_provenance import ENDPOINT_GRANT, validate_route_grant
    session_id = (_original_session(root, config["agent"])
                  if isinstance(grant, dict) and grant.get("kind") == ENDPOINT_GRANT else None)
    validate_route_grant(grant, root, raw, config, args.upstream_url, session_id=session_id)
    return {"authorization": grant, "authorization_sha256": hashlib.sha256(payload).hexdigest(),
            "authorization_bytes": len(payload)}


def _selection_continuation(args, root, raw, config, selected):
    """Accept only an explicit grant for reopening this exact completed selection."""
    value = getattr(args, "development_continuation_authorization", None)
    if not value:
        if selected is not None:
            raise ValueError("An explicitly selected development must use independent final tests unless continued by an explicit operator grant")
        return None
    if (selected is None or config.get("status") != "complete" or
            (config.get("agent_result") or {}).get("explicit_finished") is not True):
        raise ValueError("Development continuation authorization requires a closed explicitly selected development")
    path = Path(value)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("Development continuation authorization must be a regular host file of at most 16384 bytes")
    payload = path.read_bytes()
    if len(payload) > 16384:
        raise ValueError("Development continuation authorization exceeds 16384 bytes")
    try:
        grant = json.loads(payload)
    except (ValueError, UnicodeError) as error:
        raise ValueError("Invalid development continuation authorization JSON") from error
    fields = {"kind", "run_dir", "source_config_sha256", "selected_program_id",
              "selection_sha256", "authorization_basis"}
    if not isinstance(grant, dict) or set(grant) != fields:
        raise ValueError("Development continuation authorization has unexpected or missing fields")
    pointer = root / "programs" / "final.json"
    if pointer.is_symlink():
        raise ValueError("Development selection must not be a symlink")
    if (grant["kind"] != "c4g-development-continuation-authorization-v1" or
            not isinstance(grant["run_dir"], str) or Path(grant["run_dir"]).resolve() != root or
            grant["source_config_sha256"] != hashlib.sha256(raw).hexdigest() or
            grant["selected_program_id"] != selected["id"] or
            grant["selection_sha256"] != hashlib.sha256(pointer.read_bytes()).hexdigest()):
        raise ValueError("Development continuation authorization does not match the original configuration and selection")
    if not isinstance(grant["authorization_basis"], str) or not grant["authorization_basis"].strip():
        raise ValueError("Development continuation requires an explicit operator authorization basis")
    if (config.get("game_budget") or {}).get("tics_used", 0) >= config["tic_budget"]:
        raise ValueError("Original development gameplay allowance is exhausted")
    return {"authorization": grant, "authorization_sha256": hashlib.sha256(payload).hexdigest(),
            "authorization_bytes": len(payload)}


def archive_selection_for_continuation(recovery, segment):
    """Preserve the old pointer before withdrawing it under the profile lock."""
    continuation = recovery.selection_continuation
    if continuation is None:
        return
    pointer = recovery.root / "programs" / "final.json"
    payload = pointer.read_bytes()
    if pointer.is_symlink() or hashlib.sha256(payload).hexdigest() != continuation["authorization"]["selection_sha256"]:
        raise ValueError("Development selection changed during continuation validation")
    with (segment / "final.before.json").open("xb") as snapshot:
        snapshot.write(payload); snapshot.flush(); os.fsync(snapshot.fileno())
    pointer.unlink()


def _reference(root, value):
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise ValueError("Recovery evidence reference must stay within the original run")
    path = (root / value).resolve()
    if root not in path.parents:
        raise ValueError("Recovery evidence reference escapes the original run")
    return path


def _original_session(root, agent):
    path = root / f"{agent}_events.jsonl"
    found = None
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            value = (row.get("thread_id") if agent == "codex" and row.get("type") == "thread.started"
                     else row.get("session_id") if agent == "claude" else None)
            if value:
                found = str(uuid.UUID(value))
    if found is None:
        raise ValueError("No original official CLI session identity was recorded")
    return found


def _gateway_compatibility(root, config, session_id):
    """Bind an optional metadata-only repair to the preserved original run."""
    record = config.get("model_gateway_compatibility")
    if record is None:
        return None
    fields = {"format", "user_id_mode", "session_id", "run_dir", "source_config",
              "source_config_sha256", "authorization_basis"}
    if (not isinstance(record, dict) or set(record) != fields
            or record.get("format") != "c4g-run-anthropic-user-id-v1"
            or record.get("user_id_mode") != "session_uuid"
            or config.get("agent") != "claude" or record.get("session_id") != session_id
            or not isinstance(record.get("run_dir"), str)
            or Path(record["run_dir"]).resolve() != root):
        raise ValueError("Gateway compatibility does not match the original run/session")
    if (not isinstance(record["source_config"], str)
            or not isinstance(record["authorization_basis"], str)
            or not record["authorization_basis"].strip()):
        raise ValueError("Gateway compatibility requires preserved evidence and explicit authorization")
    return _verify_gateway_record_source(root, config, record, session_id)


def _gateway_tool_replay(root, config, session_id):
    """Authorize restoration of original Bash inputs for this one native session."""
    record = config.get("model_gateway_tool_replay")
    if record is None:
        return None
    fields = {"format", "replay_mode", "session_id", "run_dir", "source_config",
              "source_config_sha256", "authorization_basis"}
    if (not isinstance(record, dict) or set(record) != fields
            or record.get("format") != "c4g-run-anthropic-tool-replay-v1"
            or record.get("replay_mode") != "restore_leading_cd"
            or config.get("agent") != "claude" or record.get("session_id") != session_id
            or not isinstance(record.get("run_dir"), str)
            or Path(record["run_dir"]).resolve() != root
            or (config.get("model_gateway_compatibility") or {}).get("user_id_mode") != "session_uuid"):
        raise ValueError("Tool replay compatibility does not match the original run/session")
    if (not isinstance(record["source_config"], str)
            or not isinstance(record["authorization_basis"], str)
            or not record["authorization_basis"].strip()):
        raise ValueError("Tool replay compatibility requires preserved evidence and explicit authorization")
    return _verify_gateway_record_source(root, config, record, session_id)


def _verify_gateway_record_source(root, config, record, session_id):
    source = _reference(root, record["source_config"])
    if (not source.is_file() or source.stat().st_size > 16 * 1024 * 1024
            or hashlib.sha256(source.read_bytes()).hexdigest() != record["source_config_sha256"]):
        raise ValueError("Gateway compatibility source configuration changed")
    before = json.loads(source.read_bytes())
    identity = ("mode", "runtime_backend", "profile", "runtime_profile_sha256", "agent", "model",
                "scenario", "windows_input", "tic_budget", "token_limit", "max_tics", "wall_seconds",
                "original_started_unix", "token_accounting", "minimum_development_fraction",
                "reasoning_effort", "upstream_url", "upstream_api", "provider_model", "model_proxy",
                "scenario_protocol_sha256", "task_prompt_sha256", "agent_upgrade")
    from coding4game_gui.wsl2.route_provenance import route_identity_preserved
    if (any(before.get(key) != config.get(key) for key in identity if key != "upstream_url")
            or not route_identity_preserved(root, before, config, session_id=session_id)):
        raise ValueError("Gateway compatibility cannot change the original identity, route or allowance")
    for key in ("tokens_used", "tokens_reported", "unknown_usage_requests"):
        if config["model_budget"][key] < before["model_budget"][key]:
            raise ValueError("Gateway compatibility cannot discard consumed model resources")
    if config["game_budget"]["tics_used"] < before["game_budget"]["tics_used"]:
        raise ValueError("Gateway compatibility cannot discard consumed gameplay")
    return dict(record)


def load_recovery(args, profile, spec, *, now_unix=None, monotonic=None):
    """Read immutable evidence. The caller must separately acquire ownership."""
    root = Path(args.resume_run).resolve()
    raw = (root / "config.json").read_bytes()
    config = json.loads(raw)
    if type(config.get("model_gateway_stop_on_http_400", False)) is not bool:
        raise ValueError("Recovery HTTP 400 stop policy must be boolean")
    if config.get("mode") != "develop" or config.get("runtime_backend") != "wsl2":
        raise ValueError("Recovery requires an original WSL2 development")
    store = ProgramStore(root / "programs")
    selection_continuation = _selection_continuation(args, root, raw, config, store.final())
    if selection_continuation is None and config.get("status") not in {"failed", "incomplete"}:
        raise ValueError("Recovery requires a closed failed/incomplete development; reconcile live or retained work first")
    store.list()
    if Path(args.out_dir).resolve() != root:
        raise ValueError("Recovery must retain the original output directory")
    if Path(args.profile).resolve() != Path(config["profile"]).resolve():
        raise ValueError("Recovery cannot replace the original WSL2 profile")
    if (config.get("runtime_profile_sha256") and
            hashlib.sha256(Path(args.profile).read_bytes()).hexdigest() != config["runtime_profile_sha256"]):
        raise ValueError("Original WSL2 profile configuration changed")
    fields = ("scenario", "windows_input", "agent", "model", "token_limit", "tic_budget",
              "max_tics", "wall_seconds", "token_accounting", "reasoning_effort",
              "upstream_api", "provider_model", "minimum_development_fraction")
    for field in fields:
        if getattr(args, field, None) != config.get(field):
            raise ValueError(f"Recovery cannot change the original {field}")
    if getattr(args, "task_instructions_file", None):
        raise ValueError("Recovery retains original task instructions; do not supply another task file")
    route_migration = _route_migration(args, root, raw, config)
    from .development_policy import load_budget_notice, review_notice
    budget_notice = load_budget_notice(args, root, raw)
    from .model_gateway import validate_proxy
    if config.get("model_proxy") != validate_proxy(args.model_proxy_mode, args.model_proxy_url):
        raise ValueError("Recovery cannot change the original host proxy configuration")
    if config.get("scenario_protocol_sha256") and protocol_hash(spec) != config["scenario_protocol_sha256"]:
        raise ValueError("Registered game protocol changed since the original development")
    role = config.get("runtime_role") or {}
    if role.get("profile_id") != profile.id or role.get("baseline_sha256") != profile.baseline_sha256:
        raise ValueError("Original runtime profile/baseline identity mismatch")
    archives = [record for record in config.get("runtime_exports", [])
                if record.get("role") == "development" and record.get("distro") == role.get("distro")]
    if not archives:
        raise ValueError("Latest runtime has no verified workspace export; preserve and reconcile its retained clone")
    descriptor = dict(archives[-1])
    archive_path = Path(descriptor["path"]).resolve()
    if root not in archive_path.parents:
        raise ValueError("Recovery archive is outside the original run evidence")
    for key, expected in (("profile_id", profile.id), ("baseline_sha256", profile.baseline_sha256)):
        if descriptor.get(key, expected) != expected:
            raise ValueError("Archive descriptor conflicts with original runtime identity")
    descriptor.update(profile_id=profile.id, baseline_sha256=profile.baseline_sha256)
    if not descriptor.get("workspace"):
        # Historical exports predate the explicit workspace field. Require one
        # uniquely identified run directory, then apply the same strict verifier.
        with tarfile.open(descriptor["path"]) as archive:
            names = {PurePosixPath(member.name).parts[1] for member in archive
                     if len(PurePosixPath(member.name).parts) >= 2}
        if len(names) != 1:
            raise ValueError("Historical archive has no unique original workspace")
        descriptor["workspace"] = f"/home/{profile.user}/run/{names.pop()}"
    verified = verify_workspace_archive(descriptor, profile)
    session_id = _original_session(root, config["agent"])
    gateway_compatibility = _gateway_compatibility(root, config, session_id)
    gateway_tool_replay = _gateway_tool_replay(root, config, session_id)
    matching_history = False
    with tarfile.open(fileobj=io.BytesIO(verified.payload)) as archive:
        for member in archive:
            marker = "/.claude/projects/" if config["agent"] == "claude" else "/.codex/sessions/"
            if member.isfile() and marker in member.name and member.name.endswith(".jsonl"):
                if config["agent"] == "claude":
                    matching_history |= PurePosixPath(member.name).stem == session_id
                else:
                    first = json.loads(archive.extractfile(member).readline())
                    matching_history |= first.get("type") == "session_meta" and first.get("payload", {}).get("id") == session_id
    if not matching_history:
        raise ValueError("Verified workspace does not contain the recorded official CLI session")
    from coding4game_gui.wsl2.agent_upgrade import load_agent_upgrade
    agent_upgrade = load_agent_upgrade(root, config, profile, session_id)
    records, sources, native_sources, diagnostic_records = [], [], [], []
    diagnostic_logs = {entry["relative_path"] for entry in config.get("operator_diagnostic_gateway_logs", [])}
    logs = config.get("model_gateway_logs", ["model_gateway.jsonl"])
    if not logs or len(set(logs)) != len(logs):
        raise ValueError("Missing or duplicated original gateway journal references")
    for relative in logs:
        rows, source = read_gateway_journal(_reference(root, relative))
        records.extend(rows)
        if relative not in diagnostic_logs and not relative.startswith("operator_diagnostics/"):
            native_sources.append(source)
        else:
            diagnostic_records.extend(rows)
        sources.append(source)
    tool_replay_sources = []
    if gateway_tool_replay is not None:
        from .anthropic_replay import sources_from_journals
        tool_replay_sources = sources_from_journals(native_sources, config["model"])
    usage = reconstruct_usage(records, accounting=config["token_accounting"],
                              minimum=config.get("model_budget"))
    diagnostic_usage = reconstruct_usage(diagnostic_records, accounting=config["token_accounting"])
    if (diagnostic_usage["tokens_reported"] > usage["tokens_reported"]
            or diagnostic_usage["unknown_usage_requests"] > usage["unknown_usage_requests"]):
        raise ValueError("Diagnostic accounting exceeds the original cumulative usage")
    if config["token_limit"] is not None and usage["tokens_used"] >= config["token_limit"]:
        raise ValueError("Original model-token allowance is exhausted")
    if config["token_accounting"] == "strict" and usage["unknown_usage_requests"]:
        raise ValueError("Strict accounting cannot resume unknown historical usage")
    if config["wall_seconds"] is not None and "original_started_unix" not in config:
        raise ValueError("Historical finite deadline was not persisted; recovery cannot grant a new period")
    deadline = recover_deadline(config.get("original_started_unix"), config["wall_seconds"],
        now_unix=time.time() if now_unix is None else now_unix,
        monotonic=time.monotonic() if monotonic is None else monotonic)
    prompt_path = root / "task_prompt.md"
    task_digest = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    if config.get("task_prompt_sha256", task_digest) != task_digest:
        raise ValueError("Original development task text changed")
    prompt = (
        "Host recovery notice: continue the same development in its restored original workspace "
        "and official CLI session. The original task, model, protocol and cumulative allowances "
        "remain unchanged. Do not replay completed or ambiguously accepted model/tool requests. "
        "The previous game episode is closed or interrupted; do not assume its process memory "
        "survived. Inspect bench.status and saved submissions for authoritative settled results. "
        "Any later gameplay must use the registered cold start. Preserve the existing candidate "
        "files and choose a final programme explicitly with submission.finish. "
        f"Confirmed model usage is {usage['tokens_reported']} tokens, plus "
        f"{usage['unknown_usage_requests']} requests with unknown usage. "
        "Unknown usage is not zero and no allowance or deadline has been reset."
    )
    if selection_continuation is not None:
        prompt += (
            " The human explicitly requests continued meaningful development after the earlier "
            "early selection. The prior selection is preserved as history; this continuation "
            "requires another explicit submission.finish after development and evaluation. "
            "Historical unknown usage does not itself require immediate finalization: model "
            "access remains available below the original confirmed-token stopping threshold, "
            "subject to the original deadline and gameplay allowance. Do not treat unknown "
            "usage as zero or claim an exact remaining token allowance. Inspect bench.status "
            "for current cumulative resources, continue work toward the original game objective, "
            "and do not select the fallback solely because historical usage is unknown. "
            "No independent final-test results or strategies are supplied in this notice."
        )
    fraction = (budget_notice["authorization"]["minimum_development_fraction"]
                if budget_notice is not None else config.get("minimum_development_fraction"))
    prompt += review_notice(fraction, tic_budget=config["tic_budget"], token_limit=config["token_limit"])
    if diagnostic_records:
        prompt += (
            " Host diagnostic accounting notice: "
            f"{diagnostic_usage['tokens_reported']} confirmed tokens and "
            f"{diagnostic_usage['unknown_usage_requests']} unknown requests belong to operator diagnostics. "
            "They remain charged to the original allowance and do not count toward the development review floor. "
            f"Confirmed development usage excluding diagnostics is {usage['tokens_reported'] - diagnostic_usage['tokens_reported']} tokens. "
            "Use development-only confirmed usage for the token review threshold; the cumulative development-tic threshold is unchanged."
        )
    if budget_notice is not None and budget_notice["authorization"].get("require_registered_terminal_evaluation"):
        prompt += (
            " Human-approved task acceptance requirement: before explicit selection, obtain a programme evaluation "
            "with program_returncode=0, terminal_detected=true, complete=true and a valid non-null settled score. "
            "A normal process exit or the generic complete flag alone does not establish registered task completion. "
            "Continue meaningful development under the original remaining allowance until both the review floor "
            "and this task acceptance requirement are satisfied. This does not require winning or any minimum "
            "positive score, change gameplay/scoring/budget ceilings, or supply independent final-test feedback."
        )
    from coding4game_gui.benchmark.evidence.timeline import DevelopmentTimeline
    evaluations = [row["details"] for row in DevelopmentTimeline(root / "development_timeline.jsonl").read()
                   if row.get("kind") == "submission.result" and row.get("source") == "harness"
                   and isinstance(row.get("details"), dict)]
    return DevelopmentRecovery(root, config, raw, descriptor, usage, session_id,
                               deadline, prompt, sources, evaluations, route_migration,
                               selection_continuation, budget_notice, agent_upgrade,
                               gateway_compatibility, gateway_tool_replay, tool_replay_sources)


def verify_stopped(recovery, native, *, process_module=None):
    """Refuse live owners, registered source clones and recorded game processes.

    Call only while holding the original WSL profile's exclusive runtime lock.
    This first public path restores closed exported runs; it never kills or
    overwrites a surviving runtime to manufacture a resumable state.
    """
    if process_module is None:
        import psutil as process_module
    owner = recovery.config.get("host_owner") or {}
    own_pid = os.getpid()
    if owner.get("pid") and owner["pid"] != own_pid:
        try:
            process = process_module.Process(owner["pid"])
            if process.create_time() == owner.get("created"):
                raise ValueError("Original development owner is still live")
        except process_module.NoSuchProcess:
            pass
    for process in process_module.process_iter(["pid", "cmdline"]):
        try:
            if process.pid == own_pid:
                continue
            argv = process.info.get("cmdline") or []
            if "coding4game_gui.benchmark" not in argv:
                continue
            for index, value in enumerate(argv[:-1]):
                if value == "--out-dir" and Path(argv[index + 1]).resolve() == recovery.root:
                    raise ValueError("A benchmark operation still owns the original output directory")
        except process_module.NoSuchProcess:
            pass
    if recovery.descriptor["distro"] in native.list_distros():
        raise ValueError("Original WSL clone is still registered; reconcile retained work before archive restore")
    for state in episode_journal.states(recovery.root / "windows_episode_journal.jsonl").values():
        pid = (state.get("window") or {}).get("pid")
        if pid:
            try:
                if process_module.Process(pid).create_time() <= state["journal_ts"]:
                    raise ValueError("A recorded game process is still live; reconcile it before recovery")
            except process_module.NoSuchProcess:
                pass


def prepare_append_logs(root, agent):
    """Separate new records from an incomplete final line without rewriting it."""
    changes = []
    for name in ("episodes.jsonl", "windows_episode_journal.jsonl",
                 "development_timeline.jsonl", f"{agent}_events.jsonl"):
        path = root / name
        if path.is_symlink():
            raise ValueError("Host recovery journals must not be symlinks")
        if not path.is_file() or not path.stat().st_size:
            continue
        with path.open("r+b") as stream:
            stream.seek(-1, 2)
            if stream.read(1) == b"\n":
                continue
            stream.seek(0)
            before = hashlib.file_digest(stream, "sha256").hexdigest()
            stream.seek(0, 2)
            size = stream.tell()
            stream.write(b"\n"); stream.flush(); os.fsync(stream.fileno())
        changes.append({"path": name, "bytes_before": size, "sha256_before": before,
                        "appended_separator_bytes": 1})
    return changes
