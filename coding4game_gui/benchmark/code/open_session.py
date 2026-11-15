"""Host-owned capability surface for open development and frozen program tests.

Submitted files are copied through the boxed supervisor, never imported here.
The development runner and final runner bind separate instances and leases.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import threading
import time
import uuid

from coding4game_gui.benchmark.code.submissions import validate_program_contract
from coding4game_gui.benchmark.code.tool_bridge import tools_for_phase
from coding4game_gui.benchmark.windows.session import positive
from coding4game_gui.benchmark.windows.session import StartupRetriesExhausted


_PROGRAM_OUTPUT_TAIL_BYTES = 4096


def _with_bundled_dependencies(files, dependencies):
    """Keep bundled metadata consistent with the host-frozen declaration."""
    if "c4g-program.json" not in files:
        return files
    metadata = json.loads(files["c4g-program.json"].decode("utf-8"))
    metadata["dependencies"] = dependencies
    updated = dict(files)
    updated["c4g-program.json"] = json.dumps(
        metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return updated


def _utf8_tail(value: bytes, limit: int) -> str:
    """Decode at most the final limit bytes without returning a split code point."""
    return value[-limit:].decode("utf-8", "ignore")


class _ProgramOutputTail:
    def __init__(self, secrets):
        self.secrets = tuple(sorted(
            {value for value in (str(item) for item in secrets) if value},
            key=lambda value: len(value.encode("utf-8")), reverse=True))
        overlap = max((len(value.encode("utf-8")) for value in self.secrets), default=1) - 1
        self.capacity = _PROGRAM_OUTPUT_TAIL_BYTES + overlap
        self.buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self.byte_counts = {"stdout": 0, "stderr": 0}

    def append(self, event):
        stream = event.get("stream")
        if stream not in self.buffers:
            return
        data = event.get("data", "")
        if isinstance(data, bytes):
            encoded = data
        else:
            data = data if isinstance(data, str) else str(data)
            encoded = data.encode("utf-8")
        self.byte_counts[stream] += len(encoded)
        buffer = self.buffers[stream]
        buffer.extend(encoded)
        if len(buffer) > self.capacity:
            del buffer[:-self.capacity]

    def result(self):
        result = {}
        for stream, buffer in self.buffers.items():
            # The secret-length overlap ensures a credential crossing the visible
            # tail boundary is replaced before the final byte cap is applied.
            tail = bytes(buffer).decode("utf-8", "ignore")
            for secret in self.secrets:
                tail = tail.replace(secret, "[REDACTED]")
            redacted = tail.encode("utf-8")
            tail = _utf8_tail(redacted, _PROGRAM_OUTPUT_TAIL_BYTES)
            count = self.byte_counts[stream]
            result[stream] = {"tail": tail, "bytes": count,
                              "truncated": (count > _PROGRAM_OUTPUT_TAIL_BYTES
                                            or len(redacted) > _PROGRAM_OUTPUT_TAIL_BYTES)}
        return result


class OpenSession:
    def __init__(self, game, supervisor, store, timeline, python, *,
                 phase="development", deadline=float("inf"), gateway=None,
                 require_evaluated_submission=False, execution_platform=None,
                 require_terminal_submission=False):
        tools_for_phase(phase, max_action_tics=getattr(game, "max_action_tics", 1000))
        self.game, self.supervisor, self.store = game, supervisor, store
        self.timeline, self.python = timeline, str(python)
        self.phase, self.deadline, self.gateway = phase, deadline, gateway
        self.require_evaluated_submission = bool(require_evaluated_submission)
        self.require_terminal_submission = bool(require_terminal_submission)
        self.execution_platform = execution_platform
        self.finished = False
        self._bridge = None
        self._lock = threading.RLock()
        self._program = None
        self._current_uid = None
        self._closed = False
        self._successful_submissions = {
            row["submission_id"]
            for row in self.game.status().get("episodes", [])
            if row.get("submission_id") and self._candidate_success(row)
        }

    def _candidate_success(self, result):
        if result.get("program_returncode") != 0:
            return False
        if not self.require_terminal_submission:
            return True
        return (result.get("terminal_detected") is True
                and result.get("complete") is True
                and result.get("outcome_kind") == "valid")

    def bind_bridge(self, bridge):
        self._bridge = bridge
        return self

    def restore_evaluations(self, rows):
        """Restore proven evaluation eligibility without replaying a programme."""
        with self._lock:
            if (self.phase != "development" or self._program is not None or self._current_uid
                    or self.finished or self._closed or self.game.status().get("active_episode")):
                raise RuntimeError("Evaluation recovery requires an unused development session")
            episodes = {row.get("episode_uid") or row.get("episode_id"): row
                        for row in self.game.status().get("episodes", [])}
            restored = set()
            for row in rows:
                if not row.get("submission_id") or not self._candidate_success(row):
                    continue
                recorded = episodes.get(row.get("episode_uid") or row.get("episode_id"))
                keys = ("submission_id", "tics", "score", "complete", "terminal_detected",
                        "outcome_kind", "end_reason")
                if recorded is None or any(row.get(key) != recorded.get(key) for key in keys):
                    raise ValueError("Programme evaluation does not match the durable gameplay ledger")
                self.store.get(row["submission_id"])
                restored.add(row["submission_id"])
            self._successful_submissions.update(restored)
            self.timeline.append("development.recovery_evaluations", details={
                "evaluated_submissions": sorted(restored), "programmes_replayed": 0})
            return sorted(restored)

    def _start_game(self, **arguments):
        try:
            return self.game.start(**arguments)
        except StartupRetriesExhausted:
            if self.gateway:
                self.gateway.revoke("game_startup_failed")
            raise

    def _validate(self, tool, arguments, phase):
        if phase != self.phase:
            raise ValueError("connection phase mismatch")
        spec = next((item for item in tools_for_phase(
                         phase, max_action_tics=getattr(
                             self.game, "max_action_tics", 1000))
                     if item["name"] == tool), None)
        if spec is None:
            raise ValueError("tool unavailable in this phase")
        schema = spec["inputSchema"]
        if (not isinstance(arguments, dict)
                or set(arguments) - set(schema.get("properties", {}))
                or not set(schema.get("required", [])) <= set(arguments)):
            raise ValueError("unexpected or missing tool arguments")
        for name, value in arguments.items():
            shape = schema["properties"][name]
            if "enum" in shape and value not in shape["enum"]:
                raise ValueError(f"invalid {name}")
            kinds = shape.get("type", [])
            kinds = [kinds] if isinstance(kinds, str) else kinds
            checks = {"string": isinstance(value, str), "integer": type(value) is int,
                      "array": isinstance(value, list), "object": isinstance(value, dict),
                      "null": value is None}
            if kinds and not any(checks.get(kind, False) for kind in kinds):
                raise ValueError(f"invalid {name} type")
            if value is not None and "minimum" in shape and value < shape["minimum"]:
                raise ValueError(f"invalid {name} range")
            if value is not None and "maximum" in shape and value > shape["maximum"]:
                raise ValueError(f"invalid {name} range")
        if "controls" in arguments and not all(isinstance(x, str) for x in arguments["controls"]):
            raise ValueError("controls must be strings")
        if tool == "game.step" and set(arguments) - {"episode_id"} not in (
                {"actions"}, {"controls", "tics"}):
            raise ValueError("game.step requires actions OR raw controls plus tics, never both")

    def _uid(self, args):
        active = self.game.status()["active_episode"]
        uid = args.get("episode_id") or (active or {}).get("episode_id") or self._current_uid
        if not uid:
            raise ValueError("no current episode")
        if self.phase == "final_test" and uid != self._current_uid:
            raise ValueError("only the current test episode is accessible")
        return uid

    def dispatch(self, tool, arguments, *, phase):
        self._validate(tool, arguments, phase)
        # Never hold this lock while a child program waits on its own game tools.
        if tool == "submission.run":
            return self.run_program(arguments["id"], arguments.get("max_tics"))
        with self._lock:
            if self._closed or self.finished:
                raise RuntimeError("session is closed or final submission selected")
            if tool == "bench.status":
                status = self.game.status()
                if phase == "final_test":
                    return {key: value for key, value in status.items() if key != "episodes"}
                if self.gateway:
                    status["model_budget"] = self.gateway.status()
                status["submissions"] = self.store.list()
                status["evaluated_submissions"] = sorted(self._successful_submissions)
                return status
            if tool == "game.start":
                if self._program:
                    raise RuntimeError("a submitted program owns the current episode")
                result = self._start_game(**arguments)
                self._current_uid = result["episode_id"]
            elif tool == "game.observe":
                result = self.game.observe(self._uid(arguments))
            elif tool == "game.step":
                if "actions" in arguments:
                    result = self.game.step_batch(self._uid(arguments), arguments["actions"])
                else:
                    result = self.game.step(self._uid(arguments), arguments["controls"], arguments["tics"])
            elif tool == "game.stop":
                result = self.game.stop(self._uid(arguments))
            elif tool == "bench.replay":
                return self._replay(**arguments)
            elif tool == "submission.save":
                if self._program:
                    raise RuntimeError("wait for the running submission before saving")
                validate_program_contract(arguments["entrypoint"])
                files = self.supervisor.read_tree(arguments["path"])
                dependencies = self.store._declared_dependencies(
                    files, arguments.get("dependencies"))
                if self.execution_platform == "linux":
                    dependencies = self.supervisor.freeze_dependencies(
                        files, dependencies or None, self.deadline)
                    if not isinstance(dependencies, dict):
                        raise RuntimeError("dependency freezing returned invalid metadata")
                    files = _with_bundled_dependencies(files, dependencies)
                viewport = getattr(self._bridge, "observation_size", (1280, 768))
                result = self.store.save(files, arguments["entrypoint"],
                    description=arguments.get("description", ""),
                    dependencies=dependencies,
                    **({"observation_size": viewport} if viewport != (1280, 768) else {}),
                    execution_platform=self.execution_platform)
                self.timeline.submission(result, event="submission.save")
                for name in ("DEVELOPMENT.md", "development.md"):
                    if name in files:
                        self.timeline.self_note(files[name].decode("utf-8", "replace"))
                return result
            elif tool == "submission.finish":
                if self._program:
                    raise RuntimeError("wait for the running submission before finishing")
                # Validate before stopping an episode or revoking a model lease.
                record, _ = self.store.get(arguments["id"])
                validate_program_contract(record["entrypoint"])
                status = self.game.status()
                if (self.require_terminal_submission
                        and arguments["id"] not in self._successful_submissions):
                    raise RuntimeError(
                        "submission.finish requires a zero-exit candidate run "
                        "that reached the registered terminal detector")
                if (self.require_evaluated_submission
                        and arguments["id"] not in self._successful_submissions):
                    raise RuntimeError(
                        "submission.finish requires a successful submission.run "
                        "for this candidate in submission-required mode")
                active = status["active_episode"]
                if active:
                    self.game.stop(active["episode_id"], reason="submission_finished")
                self.store.finish(arguments["id"])
                result = self.store.final()
                self.finished = True
                if self.gateway:
                    self.gateway.revoke("submission_selected")
                self.timeline.submission(result, event="submission.finish")
                return result
            else:
                raise ValueError("unsupported tool")
            refs = result.get("evidence_refs", [])
            if result.get("observation_path"):
                refs = [result["observation_path"]]
            self.timeline.append(tool, details={k: v for k, v in result.items()
                                 if k not in {"observation", "evidence_refs"}}, refs=refs)
            return result

    def run_program(self, submission_id, max_tics=None):
        if max_tics is not None:
            positive(max_tics, "max_tics")
        with self._lock:
            if self._closed or self.finished or self._program:
                raise RuntimeError("session closed or a program is already running")
            if self.game.status()["active_episode"]:
                raise RuntimeError("stop the current episode before submission.run")
            if self._bridge is None:
                raise RuntimeError("game connection is not bound")
            record, files = self.store.get(submission_id)
            recorded_platform = record.get("execution_platform")
            if recorded_platform != self.execution_platform:
                if recorded_platform is not None or self.execution_platform is not None:
                    raise ValueError("program execution platform differs from this runtime")
            validate_program_contract(record["entrypoint"])
            if tuple(record.get("observation_size", [1280, 768])) != getattr(
                    self._bridge, "observation_size", (1280, 768)):
                raise ValueError("program observation size differs from this experiment")
            client_name = (Path(record["entrypoint"]["argv"][0]).parent / "c4g_client.py").as_posix()
            if {"c4g_client.py", client_name.casefold()} & {name.casefold() for name in files}:
                raise ValueError("c4g_client.py is reserved for the supplied game client")
            self._program = {"handle": None}
        handle, uid, result = None, None, None
        terminated = False
        try:
            # Stage a fresh exact version, never run the mutable development source.
            from coding4game_gui.paths import runtime_source
            files[client_name] = runtime_source("client.py")
            directory = self.supervisor.stage(files, name=f"runs/{uuid.uuid4().hex}")
            prepared = {"python": self.python, "env": {}, "verified": False}
            if self.execution_platform == "linux":
                prepared = self.supervisor.prepare_program(
                    record, directory, self.deadline)
                if (not isinstance(prepared, dict)
                        or not isinstance(prepared.get("python"), str)
                        or not prepared["python"]
                        or not isinstance(prepared.get("env", {}), dict)
                        or any(not isinstance(key, str) or not isinstance(value, str)
                               for key, value in prepared.get("env", {}).items())):
                    raise RuntimeError("program preparation returned invalid metadata")
                self.timeline.append("submission.dependencies", details={
                    "id": submission_id,
                    "verified": prepared.get("verified") is True,
                    "lock_id": prepared.get("lock_id"),
                })
                if time.monotonic() >= self.deadline:
                    raise TimeoutError("wall deadline exhausted during program preparation")
            with self._lock:
                if self._closed or self.finished:
                    raise RuntimeError("session closed during program preparation")
                initial = self._start_game(max_tics=max_tics,
                    mode=record["entrypoint"]["mode"], submission_id=submission_id)
                uid = self._current_uid = initial["episode_id"]
                self.timeline.append("submission.run", details={"id": submission_id,
                    "episode_id": uid, "phase": self.phase,
                    "dependencies_verified": prepared.get("verified") is True,
                    "dependency_lock_id": prepared.get("lock_id")})
                argv = record["entrypoint"]["argv"]
                entrypoint = (self.supervisor.native_path(directory, argv[0])
                              if hasattr(self.supervisor, "native_path") else
                              str(Path(directory) / argv[0]))
                command = [prepared["python"], entrypoint, *argv[1:]]
                env = {**prepared.get("env", {}),
                       "C4G_BRIDGE_URL": self._bridge.url,
                       "C4G_BRIDGE_TOKEN": self._bridge.token,
                       "C4G_EPISODE_ID": uid, "C4G_BRIDGE_PHASE": self.phase}
                child = self.supervisor.launch(command, cwd=directory, env=env)
                handle = child["handle"]
                self._program["handle"] = handle
            output = self.game.root / "program_output" / f"{uid}.jsonl"
            output.parent.mkdir(parents=True, exist_ok=True)
            secrets = [self._bridge.token]
            if self.gateway is not None and getattr(self.gateway, "token", None):
                secrets.append(self.gateway.token)
            secrets.extend(getattr(self.timeline, "secrets", ()))
            diagnostics = _ProgramOutputTail(secrets)
            def persist(state):
                with output.open("a", encoding="utf-8") as stream:
                    for event in state.get("events", []):
                        diagnostics.append(event)
                        safe_event = event
                        if isinstance(event.get("data"), bytes):
                            safe_event = dict(event, data=event["data"].decode("utf-8", "replace"))
                        safe = json.dumps(safe_event, ensure_ascii=False)
                        for secret in diagnostics.secrets:
                            safe = safe.replace(secret, "[REDACTED]")
                        stream.write(safe + "\n")
            state = {}
            exit_policy = None
            cooperative_exit_wait_seconds = 0.0
            cleanup_deadline = None
            while True:
                if self._closed or time.monotonic() >= self.deadline:
                    result = self.game.stop(uid, reason="wall_budget")
                    exit_policy = "forced_session_close" if self._closed else "forced_deadline"
                    break
                state = self.supervisor.poll(handle)
                persist(state)
                if state.get("output_overflow"):
                    raise RuntimeError("submitted program output overflow; diagnostics="
                                       + json.dumps(diagnostics.result(), ensure_ascii=False))
                if not state.get("running", False) and state.get("output_complete", True):
                    result = self.game.stop(uid, reason=("program_exit" if state.get("exit_code") == 0
                                                        else "program_error"))
                    result = dict(result, program_returncode=state.get("exit_code"))
                    exit_policy = "cooperative_program_exit"
                    break
                active = self.game.status()["active_episode"]
                if not active or active["episode_id"] != uid:
                    result = self.game.observe(uid)
                    wait_started = time.monotonic()
                    cleanup_deadline = min(self.deadline, wait_started + 2.0)
                    while (not self._closed and time.monotonic() < cleanup_deadline
                           and state.get("running", False)):
                        time.sleep(min(.02, max(0., cleanup_deadline - time.monotonic())))
                        state = self.supervisor.poll(handle)
                        persist(state)
                        if state.get("output_overflow"):
                            raise RuntimeError("submitted program output overflow; diagnostics="
                                               + json.dumps(diagnostics.result(), ensure_ascii=False))
                    cooperative_exit_wait_seconds = max(0., time.monotonic() - wait_started)
                    if not state.get("running", False):
                        exit_policy = "cooperative_after_game_end"
                    elif self._closed:
                        exit_policy = "forced_session_close"
                    elif time.monotonic() >= self.deadline:
                        exit_policy = "forced_deadline"
                    else:
                        exit_policy = "forced_after_game_end_grace"
                    break
                time.sleep(.02)
            # Game completion is not pipe EOF. Terminate, then collect the final
            # diagnostics with a bounded drain allowance, explicitly recording
            # whether collection completed. This is cleanup time, not play time.
            if state.get("running") is not False:
                termination = self.supervisor.terminate(handle)
                terminated = True
                if isinstance(termination, dict) and termination.get("exit_code") is not None:
                    state = dict(state, running=False,
                                 exit_code=termination["exit_code"])
            else:
                # A known child exit needs no signal from the finally cleanup.
                terminated = True
            drain_deadline = cleanup_deadline or min(self.deadline, time.monotonic() + 2.0)
            while not state.get("output_complete", not state.get("running", True)):
                if time.monotonic() >= drain_deadline:
                    break
                state = self.supervisor.poll(handle)
                persist(state)
                if not state.get("output_complete", not state.get("running", True)):
                    time.sleep(.02)
            result = dict(result, program_returncode=state.get("exit_code"),
                          dependencies_verified=prepared.get("verified") is True,
                          dependency_lock_id=prepared.get("lock_id"),
                          program_output_complete=state.get("output_complete", not state.get("running", True)),
                          program_output=diagnostics.result(),
                          program_exit_policy=exit_policy,
                          cooperative_exit_wait_seconds=cooperative_exit_wait_seconds)
            if self._candidate_success(result):
                with self._lock:
                    self._successful_submissions.add(submission_id)
            self.timeline.append("submission.result", details=result,
                refs=[str(output.relative_to(self.game.root)).replace("\\", "/"),
                      *result.get("evidence_refs", [])])
            return result
        except BaseException:
            if uid:
                self.game.stop(uid, reason="program_runner_error", infrastructure_error=True)
            raise
        finally:
            try:
                if handle and not terminated:
                    self.supervisor.terminate(handle)
            finally:
                with self._lock:
                    self._program = None

    def _replay(self, uid, start, end, every=1):
        from coding4game_gui.benchmark.evidence.replay import to_tic
        positive(every, "every")
        row = next((entry for entry in self.game.status()["episodes"]
                    if entry["episode_id"] == uid), None)
        if row is None:
            raise ValueError("only a completed episode of this session can be replayed")
        rate = row.get("tics_per_second", 10)
        first, last = to_tic(start, rate), to_tic(end, rate)
        if first > last or (last - first) // every + 1 > 40:
            raise ValueError("invalid window or more than 40 replay frames")

        observations = {}
        for event in self.game.evidence(uid)["events"]:
            tic = event.get("tics")
            if (event.get("event") == "observation" and type(tic) is int
                    and first <= tic <= last and (tic - first) % every == 0):
                observations[tic] = event["observation_path"]
        directory = (self.game.root / "evidence" / uid).resolve()
        frames = []
        for tic, relative in sorted(observations.items()):
            path = (self.game.root / relative).resolve()
            if path.parent != directory or path.suffix != ".png":
                raise ValueError("invalid recorded observation path")
            if path.is_file():
                frames.append({
                    "tic": tic,
                    "observation": {"mimeType": "image/png",
                                    "data": base64.b64encode(path.read_bytes()).decode("ascii")},
                })
        return {"episode_id": uid, "frames": frames}

    def close(self):
        with self._lock:
            self._closed = True
            child = self._program
        if self.gateway:
            self.gateway.revoke()
        try:
            if child and child.get("handle"):
                self.supervisor.terminate(child["handle"])
        finally:
            self.game.close()
