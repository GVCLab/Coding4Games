"""Exercise real Linux CLI session restoration against a synthetic local gateway.

No provider configuration, upstream request, model inference or game is used.
This checks workspace/session/connection recovery, not full game acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

REPLY = "C4G_RECOVERED_SESSION_SYNTHETIC_REPLY"


def verify_recovered_export(record, verified, history):
    """Independently check archived post-continuation history and candidates."""
    path = Path(record["path"])
    if path.stat().st_size != record["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
        raise RuntimeError("Post-recovery archive integrity mismatch")
    prefix = "run/" + verified.workspace.name + "/"
    checked = 0
    with tarfile.open(path) as archive:
        data = archive.extractfile(history["path"]).read()
        if hashlib.sha256(data[:history["bytes"]]).hexdigest() != history["sha256"]:
            raise RuntimeError("Original official CLI history was modified or truncated")
        for name, entry in verified.entries.items():
            if entry["kind"] != "file" or not name.startswith(prefix):
                continue
            relative = name[len(prefix):]
            if relative.split("/", 1)[0] in {".claude", ".codex", "mcp"}:
                continue
            if hashlib.sha256(archive.extractfile(name).read()).hexdigest() != entry["sha256"]:
                raise RuntimeError("Restored candidate/workspace file changed during fixture")
            checked += 1
    return {"original_history_prefix_unchanged": True,
            "continued_history_bytes": len(data), "candidate_files_unchanged": checked,
            "archive_sha256": record["sha256"]}


def _texts(value):
    if isinstance(value, str):
        if len(value) >= 40:
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {"text", "content", "message", "messages", "input", "payload"}:
                yield from _texts(item)


def _reply(protocol, model):
    if protocol == "anthropic":
        events = [
            ("message_start", {"type": "message_start", "message": {
                "id": "msg_synthetic_recovery", "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 0}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": REPLY}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {
                "stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}}),
            ("message_stop", {"type": "message_stop"}),
        ]
    else:
        message = {"id": "msg_synthetic_recovery", "type": "message", "role": "assistant",
                   "status": "completed", "content": [{"type": "output_text", "text": REPLY,
                                                          "annotations": []}]}
        response = {"id": "resp_synthetic_recovery", "object": "response", "created_at": 1,
                    "status": "completed", "model": model, "output": [message],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
        events = [("response.created", {"type": "response.created", "response": {
                    **response, "status": "in_progress", "output": []}}),
                  ("response.output_item.added", {"type": "response.output_item.added",
                    "output_index": 0, "item": {**message, "status": "in_progress", "content": []}}),
                  ("response.output_text.delta", {"type": "response.output_text.delta",
                    "output_index": 0, "content_index": 0, "item_id": message["id"], "delta": REPLY}),
                  ("response.output_item.done", {"type": "response.output_item.done",
                    "output_index": 0, "item": message}),
                  ("response.completed", {"type": "response.completed", "response": response})]
    class Response:
        status = 200
        headers = {"content-type": "text/event-stream"}
        def iter_bytes(self):
            for event, value in events:
                yield ("event: " + event + "\ndata: " + json.dumps(value) + "\n\n").encode()
        def close(self): pass
    return Response()


def execute(args):
    from coding4game_gui.wsl2 import WslProfile, WslRuntime
    from coding4game_gui.wsl2.recovery import verify_workspace_archive
    from coding4game_gui.benchmark.code.agent_driver import run_agent
    from coding4game_gui.benchmark.code.model_gateway import ModelGateway
    from coding4game_gui.benchmark.code.tool_bridge import ToolServer
    from coding4game_gui.benchmark.evidence.timeline import DevelopmentTimeline
    profile = WslProfile.load(args.profile)
    descriptor = json.loads(args.descriptor.read_text(encoding="utf-8"))
    verified = verify_workspace_archive(descriptor, profile)
    if str(uuid.UUID(args.session_id)) != args.session_id:
        raise ValueError("The original canonical CLI session UUID is required")
    prior_texts, history = set(), None
    with tarfile.open(fileobj=io.BytesIO(verified.payload)) as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith(".jsonl"):
                continue
            if (args.agent == "claude" and "/.claude/projects/" not in member.name
                    or args.agent == "codex" and "/.codex/sessions/" not in member.name):
                continue
            raw = archive.extractfile(member).read()
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            ids = {row.get("sessionId") for row in rows}
            ids.update(row.get("payload", {}).get("id") for row in rows
                       if row.get("type") == "session_meta")
            if args.session_id in ids or Path(member.name).stem == args.session_id:
                history = {"path": member.name, "bytes": len(raw),
                           "sha256": hashlib.sha256(raw).hexdigest()}
                prior_texts.update(text for row in rows[-40:] for text in _texts(row))
                break
    if history is None or not prior_texts:
        raise ValueError("Verified archive does not contain the requested official CLI session")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    report = {"status": "running", "kind": "real_cli_synthetic_gateway_workspace_recovery",
              "provider_requests": 0, "model_inference": False, "game_launches": 0,
              "full_development_recovery_accepted": False, "session_id": args.session_id,
              "source_history": history, "synthetic_generation_requests": 0}
    runtime = WslRuntime(profile, evidence_dir=out)
    protocol = "anthropic" if args.agent == "claude" else "responses"
    class NoGame:
        def dispatch(self, name, arguments, *, phase):
            raise RuntimeError("The recovery fixture exposes no game or submission operations")
    def transport(method, url, headers, body, timeout):
        request = json.loads(body)
        if report["synthetic_generation_requests"] >= 1:
            raise RuntimeError("The synthetic recovery fixture permits one generation only")
        report["synthetic_generation_requests"] += 1
        visible = set(_texts(request))
        report["prior_context_text_matches"] = len(prior_texts & visible)
        report["requested_model"] = request.get("model")
        return _reply(protocol, args.model)
    try:
        runtime.preflight(protected=(REPO, out))
        with runtime:
            supervisor = runtime.start("development", recovery=descriptor)
            report["runtime"] = runtime.role_evidence("development")
            with ToolServer(NoGame(), port=profile.ports["development_game"]) as bridge:
                with ModelGateway("http://127.0.0.1:1", "unused-synthetic-key", args.model,
                    10000, 90, out / "synthetic_gateway.jsonl", port=profile.ports["gateway"],
                    strict_total_enforcement=False, protocol=protocol, transport=transport) as gateway:
                    timeline = DevelopmentTimeline(out / "development_timeline.jsonl",
                                                  secrets=(gateway.token, bridge.token))
                    report["driver"] = run_agent(supervisor, profile, gateway, bridge,
                        "Host recovery fixture: keep the restored conversation and workspace. "
                        "Do not call tools or replay prior actions. Await the synthetic fixture reply.",
                        timeline, time.monotonic() + 90, agent=args.agent,
                        resume_session_id=args.session_id)
                    report["synthetic_accounting_only"] = gateway.status()
            report["status"] = "passed_for_real_cli_synthetic_gateway_only"
            events = Path(report["driver"]["evidence"]["events"]).read_text(encoding="utf-8")
            report["synthetic_reply_observed"] = REPLY in events
            if (report["driver"].get("exit_code") != 0
                    or report["driver"].get("success") is False
                    or not report["driver"].get("recovery_identity_observed")
                    or not report["synthetic_reply_observed"]
                    or report.get("requested_model") != args.model
                    or report.get("prior_context_text_matches", 0) < 1
                    or report["synthetic_generation_requests"] != 1):
                raise RuntimeError("Native CLI did not continue the archived session as expected")
        if len(runtime.archives) != 1:
            raise RuntimeError("Expected one recovered development export")
        report["post_continuation_verification"] = verify_recovered_export(
            runtime.archives[0], verified, history)
    except BaseException as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        report["runtime_exports"] = runtime.archives
        report["owned_clones_absent"] = all(
            item["distro"] not in runtime.native.list_distros() for item in runtime.archives)
        (out / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--descriptor", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--agent", choices=("claude", "codex"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"execute": False, "provider_requests": 0, "model_inference": False,
                          "side_effects": "Restore one verified workspace and run its official CLI against synthetic local replies; export and clean up the owned clone."}))
        return 0
    result = execute(args)
    print(json.dumps({key: result.get(key) for key in
                     ("status", "provider_requests", "synthetic_generation_requests", "prior_context_text_matches")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
