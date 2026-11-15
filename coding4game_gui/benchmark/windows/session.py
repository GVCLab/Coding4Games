"""Trusted, serial game lifecycle for the opt-in open-code protocol.

This module never executes submitted code. The injected environment is the real
Windows adapter in production and an external-game substitute in unit tests.
"""
from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

from coding4game_gui.benchmark.evidence import episode_journal as journal
from coding4game_gui.benchmark.evidence._lock import locked
from coding4game_gui.benchmark.windows.timing import STEP_BUDGET_TOLERANCE_TICS


def positive(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class StartupRetriesExhausted(RuntimeError):
    """The host cannot safely start another episode in this session."""


class _RetryableStartupError(RuntimeError):
    """One failed startup has been cleaned up and recorded."""


class GameSession:
    def __init__(self, make_env, out_dir, *, tic_budget, max_tics=None,
                 deadline=float("inf"), clock=time.monotonic,
                 monitor_interval=.05, require_terminal=False,
                 max_action_tics=1000):
        self.make_env = make_env
        self.require_terminal = bool(require_terminal)
        self.root = Path(out_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.tic_budget = positive(tic_budget, "tic_budget")
        self.max_tics = (positive(max_tics, "max_tics")
                         if max_tics is not None else None)
        from coding4game_gui.benchmark.windows.timing import resolve_max_action_tics
        self.max_action_tics = resolve_max_action_tics(
            {"max_action_tics": max_action_tics})
        self.deadline, self.clock = deadline, clock
        self.journal_path = self.root / "windows_episode_journal.jsonl"
        self._lock = threading.RLock()
        self._halt_lock = threading.Lock()
        self._active = None
        self._env = None
        self._closed = False
        self._results = self._result_rows()
        self._quit = threading.Event()
        ledger_rows = dict(self._results)
        for state in journal.states(self.journal_path).values():
            uid = state["episode_uid"]
            if uid in ledger_rows:
                row = dict(ledger_rows[uid])
                # Repair a missing or old sparse marker so journal-only readers
                # fold to the same authoritative finalized state next time.
                if (not state.get("ledgered") or any(
                        state.get(key) != value for key, value in row.items()
                        if key != "episode_uid")):
                    journal.append(self.journal_path, "ledgered", uid,
                                   **self._journal_result_fields(row))
            elif not state.get("ledgered"):
                row = dict(state)
                partial = row.get("score")
                row.update(done=True, complete=False, partial_score=partial,
                           outcome_kind="infrastructure_error",
                           end_reason="interrupted", score=None,
                           recovered_from_journal=True)
                self._append_result(row)
                journal.append(self.journal_path, "ledgered", uid,
                               **self._journal_result_fields(row))
            else:
                row = dict(state)
            self._results[uid] = row
        self._monitor_interval = max(.005, float(monitor_interval))
        self._watchdog = threading.Thread(target=self._watch, daemon=True,
                                          name="c4g-game-budget")
        self._watchdog.start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def status(self):
        with self._lock:
            used = sum(row.get("tics", 0) for row in self._results.values())
            if self._active:
                used += self._active["tics"]
            stop_reason = ("wall_budget_exhausted" if self.clock() >= self.deadline else
                           "tic_budget_exhausted" if used >= self.tic_budget else None)
            message = ("This run's time limit has been reached; gameplay cannot continue." if stop_reason == "wall_budget_exhausted" else
                       f"This run's tic limit has been reached: {used:g} / {self.tic_budget} tics; "
                       "Gameplay cannot continue; you may still inspect status, prepare and submit your program." if stop_reason else "")
            return {"tics_budget": self.tic_budget, "tics_used": used,
                    "max_action_tics": self.max_action_tics,
                    "budget_stop_reason": stop_reason, "budget_message": message,
                    "tics_remaining": max(0, self.tic_budget - used),
                    "budget_overshoot_tics": max(0, used - self.tic_budget),
                    "wall_enforcement": {
                        "kind": "cooperative_host_threads", "strict": False,
                        "limitation": "capture, scorer, or halt hangs may delay cleanup"},
                    "wall_seconds_remaining": (None if self.deadline == float("inf")
                                               else max(0, self.deadline - self.clock())),
                    "active_episode": dict(self._active) if self._active else None,
                    "episodes": list(self._results.values())}

    def start(self, max_tics=None, mode="stepped", *, submission_id=None):
        """Try at most three complete startups, preserving failed evidence."""
        with self._lock:
            for attempt in range(1, 4):
                try:
                    return self._start_once(max_tics, mode, submission_id=submission_id,
                                            startup_attempt=attempt)
                except _RetryableStartupError as exc:
                    if attempt == 3:
                        self._closed = True
                        self._quit.set()
                        raise StartupRetriesExhausted(
                            f"startup_retries_exhausted: 3 startup attempts failed; {exc}") from exc
                    print(f"[startup] Attempt {attempt}/3 failed and was cleaned up; "
                          f"retrying complete startup ({attempt + 1}/3).", flush=True)

    def _start_once(self, max_tics=None, mode="stepped", *, submission_id=None,
                    startup_attempt=1):
        with self._lock:
            if self.clock() >= self.deadline:
                raise RuntimeError("wall_budget_exhausted: This run's time limit has been reached; the game cannot start.")
            if self._closed:
                raise RuntimeError("session_closed: The game session has been closed.")
            if self._active:
                raise RuntimeError("an episode is already active; stop it explicitly")
            if mode != "stepped":
                raise ValueError("game mode must be stepped")
            caps = [self.status()["tics_remaining"]]
            if self.max_tics is not None:
                caps.append(self.max_tics)
            if max_tics is not None:
                caps.append(positive(max_tics, "max_tics"))
            cap = min(caps)
            if cap <= 0:
                raise RuntimeError("tic_budget_exhausted: " + self.status()["budget_message"])
            uid = uuid.uuid4().hex
            self._active = {"episode_id": uid, "episode_uid": uid,
                            "startup_attempt": startup_attempt, "startup_max_attempts": 3,
                            "episode_index": len(self._results), "mode": mode,
                            "submission_id": submission_id, "tics": 0,
                            "game_running_seconds": 0.0,
                            "tic_accounting": "cumulative_running_time_half_up",
                            "requested_max_tics": max_tics, "resolved_max_tics": cap,
                            "max_action_tics": self.max_action_tics,
                            "score": None, "done": False, "complete": False,
                            "evidence_refs": [],
                            "wall_enforcement": {
                                "kind": "cooperative_host_threads", "strict": False,
                                "limitation": "capture, scorer, or halt hangs may delay cleanup"},
                            "action_log_scope": "tool_calls_only"}
            journal.append(self.journal_path, "begin", uid,
                           **{k: v for k, v in self._active.items() if k != "episode_uid"})
            try:
                self._env = self.make_env()
                self._env.reset(self._active["episode_index"])
                self._active["tics_per_second"] = self._env.TICRATE
                if getattr(self._env, "max_action_tics", self.max_action_tics) != self.max_action_tics:
                    raise ValueError("game environment max_action_tics does not match the session")
                self._active["windows_input"] = getattr(self._env, "windows_input", "gamepad")
                self._active["window"] = self._env.window_info()
                start_evidence = getattr(self._env, "start_terminal_evidence", None)
                if start_evidence is not None:
                    start_evidence(self.root / "evidence" / uid / "terminal", uid)
                return self.observe(uid)
            except BaseException as exc:
                self._active["startup_error"] = f"{type(exc).__name__}: {exc}"
                try:
                    failed = self.stop(uid, reason="startup_error", infrastructure_error=True)
                except BaseException:
                    self._closed = True
                    self._quit.set()
                    raise
                from coding4game_gui.benchmark.windows.game_launch import SteamLaunchPendingError
                if isinstance(exc, SteamLaunchPendingError):
                    self._closed = True
                    self._quit.set()
                    raise StartupRetriesExhausted(
                        f'steam_launch_pending: startup was stopped without resubmitting; {exc}') from exc
                if isinstance(exc, (RuntimeError, OSError)) and not failed.get("cleanup_error"):
                    raise _RetryableStartupError(str(exc)) from exc
                self._closed = True
                self._quit.set()
                if failed.get("cleanup_error"):
                    raise StartupRetriesExhausted(
                        "startup_cleanup_failed: retry is unsafe after failed cleanup") from exc
                if isinstance(exc, Exception):
                    raise StartupRetriesExhausted(
                        f"startup_unrecoverable: {type(exc).__name__}: {exc}") from exc
                raise

    def _require(self, uid):
        if not self._active or self._active["episode_id"] != uid:
            result = self._results.get(uid, {})
            if result.get("budget_message"):
                raise RuntimeError(result["budget_stop_reason"] + ": " + result["budget_message"])
            raise ValueError("episode is not active (stale or unknown episode_id)")

    def observe(self, episode_id):
        with self._lock:
            if episode_id in self._results:
                return dict(self._results[episode_id])
            self._require(episode_id)
            import cv2
            from coding4game_gui.benchmark.policy_observation import policy_frame
            frame = policy_frame(self._env.frame())
            ok, encoded = cv2.imencode(".png", frame)
            if not ok:
                raise RuntimeError("cannot encode game observation")
            relative = f"evidence/{episode_id}/{uuid.uuid4().hex}.png"
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encoded.tobytes())
            self._active["evidence_refs"].append(relative)
            journal.append(self.journal_path, "observation", episode_id,
                           tics=self._active["tics"], score=self._active["score"],
                           observation_path=relative)
            return dict(self._active) | {
                "observation": {"mimeType": "image/png", "data":
                                base64.b64encode(encoded).decode("ascii")},
                "observation_path": relative, "captured_at": time.time()}

    def step(self, episode_id, actions, tics):
        """Run raw gamepad input or the registered captured-pointer extensions."""
        with self._lock:
            self._require(episode_id)
            positive(tics, "tics")
            if not isinstance(actions, list) or not all(isinstance(x, str) for x in actions):
                raise ValueError("actions must be a list of control strings")
            if self._active["windows_input"] == "gamepad-kbm":
                token=actions[0].strip().upper() if len(actions)==1 else ''
                if token!='CLICK_CURRENT' and not token.startswith('RMOVE['):
                    raise ValueError("keyboard/mouse mode requires structured actions except a sole RMOVE or CLICK_CURRENT")
            return self._run_plan(episode_id, [(actions, tics)])

    def step_batch(self, episode_id, actions):
        """Validate the entire GUI batch, then return only its final observation."""
        from coding4game_gui.runtime_client.client import gui_action_plan
        with self._lock:
            self._require(episode_id)
            if self._active["windows_input"] != "gamepad-kbm":
                raise ValueError("structured actions require keyboard/mouse mode")
            return self._run_plan(episode_id, gui_action_plan(actions), structured=actions)

    def _run_plan(self, episode_id, plan, *, structured=None):
        """Run validated pulses while holding the session lock, with no model waits."""
        from coding4game_gui.benchmark.windows.timing import validate_action_tics
        for _controls, requested_tics in plan:
            validate_action_tics(requested_tics, self.max_action_tics)
        # Include backend-specific validation before any prefix can execute.
        self._env.validate_action_plan(plan)
        batch = None
        if structured is not None:
            batch = {"id": uuid.uuid4().hex, "requested_actions": len(plan),
                     "executed_actions": 0, "remaining_actions": len(plan),
                     "stop_reason": None, "actions": []}
            self._active["batch"] = batch
            journal.append(self.journal_path, "batch_begin", episode_id,
                           batch_id=batch["id"], actions=structured)

        def finish(reason=None):
            if batch is not None:
                batch["stop_reason"] = reason or "batch_complete"
                journal.append(self.journal_path, "batch_end", episode_id, batch=batch)
            # Capture the terminal frame before stop closes the game. Ordinary
            # per-action evidence capture is unchanged; only agent observations batch.
            observation = self.observe(episode_id) if batch is not None or reason is None else None
            if reason is None:
                return observation
            result = self.stop(episode_id, reason=reason)
            if observation is not None:
                result.update({key: observation[key] for key in
                    ("observation", "observation_path", "captured_at")})
            return result

        try:
            for index, (actions, tics) in enumerate(plan):
                remaining = self._active["resolved_max_tics"] - self._active["tics"]
                wall_tics = remaining
                if self.deadline != float("inf"):
                    wall_tics = max(0, math.floor(
                        max(0., self.deadline - self.clock())
                        * self._active["tics_per_second"] + 1e-9))
                allowed = min(tics, remaining, wall_tics)
                if allowed <= 0:
                    return finish("tic_budget" if remaining <= 0 else "wall_budget")
                limiting_reason = ("wall_budget" if wall_tics < min(tics, remaining)
                                   else "tic_budget" if remaining <= tics else None)
                before = self._env.tic
                context = getattr(self._env, 'set_action_context', None)
                if context is not None:
                    context(batch_id=batch['id'] if batch else episode_id + ':step')
                self._env.act(actions, int(allowed))
                self._active.update(tics=self._env.tic, score=self._env.total_reward,
                                    game_running_seconds=self._current_running_seconds())
                if batch is not None:
                    batch["actions"].append({"index": index, "requested_tics": tics,
                        "allowed_tics": allowed, "actual_tics": self._env.tic - before,
                        "truncated": allowed < tics or self._env.tic - before < allowed})
                    timing = getattr(self._env, "last_action_timing", None)
                    if timing is not None:
                        batch["actions"][-1]["timing"] = dict(timing)
                    batch.update(executed_actions=index + 1, remaining_actions=len(plan) - index - 1)
                journal.append(self.journal_path, "progress", episode_id,
                               tics=self._env.tic, score=self._env.total_reward,
                               game_running_seconds=self._current_running_seconds(),
                               last_action={"actions": actions, "requested_tics": tics},
                               **({"batch_id": batch["id"], "action_index": index} if batch else {}))
                if self.clock() >= self.deadline:
                    limiting_reason = "wall_budget"
                if (self._env.done or limiting_reason is not None
                        or self._env.tic >= self._active["resolved_max_tics"]):
                    return finish(self._env.end_reason or limiting_reason or "tic_budget")
            return finish()
        except BaseException:
            try:
                if batch is not None:
                    batch["stop_reason"] = "step_error"
                    journal.append(self.journal_path, "batch_error", episode_id, batch=batch)
            finally:
                try:
                    self.stop(episode_id, reason="step_error", infrastructure_error=True)
                finally:
                    raise

    def stop(self, episode_id, *, reason="agent_stopped", infrastructure_error=False):
        with self._lock:
            if episode_id in self._results:
                return dict(self._results[episode_id])
            self._require(episode_id)
            lifecycle_errors = []
            row = dict(self._active)
            scoring_interrupt = None
            if self._env is not None:
                journal.append(self.journal_path, "score_settlement_started", episode_id)
                try:
                    settle = getattr(self._env, 'finalize_score', None)
                    if settle is not None:
                        settle()
                    save_score = getattr(self._env, 'save_score_evidence', None)
                    if save_score is not None:
                        row['score_settlement'] = save_score(self.journal_path.parent / 'score-settlement' / episode_id)
                except KeyboardInterrupt as exc:
                    scoring_interrupt = exc
                    infrastructure_error = True
                    reason = "scoring_interrupted"
                    lifecycle_errors.append("scoring interrupted by operator")
                except Exception as exc:
                    lifecycle_errors.append(f"scoring failed: {type(exc).__name__}: {exc}")
                monitor = getattr(self._env, '_terminal_monitor', None)
                if monitor is not None:
                    row['terminal_evidence'] = monitor.summary()
                    if monitor.failure:
                        self._closed = True
                        self._quit.set()
                        callback = getattr(self, 'terminal_evidence_failure_callback', None)
                        if callback is not None:
                            try:
                                callback()
                            except Exception as callback_exc:
                                lifecycle_errors.append(f'terminal evidence stop callback failed: {callback_exc}')
                journal.append(self.journal_path, "score_settlement_finished", episode_id,
                               errors=list(lifecycle_errors))
                row.update(tics=max(row["tics"], self._env.tic),
                           score=self._env.total_reward)
                components = getattr(self._env, 'score_breakdown', None)
                if components and components.get('score_version'):
                    row.update(components)
                    row['milestone_evidence'] = getattr(self._env, 'milestone_evidence', [])
            if row.get("tics_per_second"):
                row["game_running_seconds"] = self._current_running_seconds()
            row.update(done=True, complete=not infrastructure_error,
                       end_reason=reason, outcome_kind=("infrastructure_error"
                                                       if infrastructure_error else "valid"))
            # The Windows detector uses game_over for registered defeat phases.
            # Require the environment's matching terminal state for that reason;
            # a caller-supplied stop label alone cannot complete a final test.
            registered_defeat = (
                reason == "game_over" and self._env is not None
                and self._env.done and self._env.end_reason == "game_over")
            row["terminal_detected"] = (
                registered_defeat or reason == "terminal"
                or str(reason).startswith("terminal:"))
            if self.require_terminal and not row["terminal_detected"] and row["complete"]:
                row.update(complete=False, outcome_kind="incomplete_task")
            if lifecycle_errors:
                row.update(complete=False, outcome_kind="infrastructure_error",
                           sampling_error="; ".join(lifecycle_errors))
            row["budget_overshoot_tics"] = max(0, row["tics"] - row["resolved_max_tics"])
            row["budget_overshoot_tolerance_tics"] = STEP_BUDGET_TOLERANCE_TICS
            if (row["budget_overshoot_tics"] > row["budget_overshoot_tolerance_tics"]
                    and row["complete"]):
                row.update(complete=False, outcome_kind="infrastructure_error",
                           end_reason="budget_overshoot")
            try:
                with self._halt_lock:
                    try:
                        if self._env is not None:
                            self._env.close()
                    finally:
                        self._env = None
                        self._active = None
            except Exception as exc:
                row.update(complete=False, outcome_kind="infrastructure_error",
                           cleanup_error=f"{type(exc).__name__}: {exc}")
            if not row["complete"]:
                row["partial_score"], row["score"] = row["score"], None
            if reason in {"wall_budget", "tic_budget"}:
                used = sum(r.get("tics", 0) for r in self._results.values()) + row["tics"]
                if reason == "wall_budget":
                    code, message = "wall_budget_exhausted", "This run's time limit has been reached; the game has stopped."
                elif used >= self.tic_budget:
                    code, message = "tic_budget_exhausted", (
                        f"This run's tic limit has been reached: {used:g} / {self.tic_budget} tics; "
                        "The game has stopped; you may still inspect status, prepare and submit your program.")
                else:
                    code, message = "episode_tic_limit", (
                        f"This episode's tic limit has been reached: {row['tics']:g} / {row['resolved_max_tics']:g} tics; "
                        f"run total: {used:g} / {self.tic_budget} tics; you may still start the next episode.")
                row.update(budget_stop_reason=code, budget_message=message)
            journal.append(self.journal_path, "result_ready", episode_id,
                           **{k: v for k, v in row.items() if k != "episode_uid"})
            self._results[episode_id] = row
            self._append_result(row)
            journal.append(self.journal_path, "ledgered", episode_id)
            if scoring_interrupt is not None:
                raise scoring_interrupt
            return dict(row)

    def _result_rows(self):
        path = self.root / "episodes.jsonl"
        if not path.is_file():
            return {}
        result = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            uid = row.get("episode_uid") if isinstance(row, dict) else None
            if uid:
                result[uid] = row
        return result

    @staticmethod
    def _journal_result_fields(row):
        return {key: value for key, value in row.items()
                if key not in {"episode_uid", "journal_event", "journal_ts",
                               "ledgered"}}

    def _append_result(self, row):
        path = self.root / "episodes.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            with locked(stream):
                stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def evidence(self, episode_id):
        """Return only durable records belonging to one episode UID."""
        with self._lock:
            result = self._results.get(episode_id)
            if result is None:
                self._require(episode_id)
                result = dict(self._active)
            events = []
            if self.journal_path.is_file():
                for line in self.journal_path.read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("episode_uid") == episode_id:
                        events.append(row)
            return {"episode_id": episode_id, "result": dict(result),
                    "events": events,
                    "refs": list(result.get("evidence_refs", []))}

    def _current_running_seconds(self):
        return getattr(self._env, "game_running_seconds",
                       self._env.tic / self._active["tics_per_second"])

    def _watch(self):
        while not self._quit.wait(self._monitor_interval):
            row = self._active
            if row is None or self.clock() < self.deadline:
                continue
            # Freeze before waiting for observation or scoring under the session lock.
            halt_failed = False
            try:
                with self._halt_lock:
                    if self._active is row and self._env is not None:
                        self._env.halt_actions()
            except Exception:
                halt_failed = True
            with self._lock:
                if self._active is row:
                    terminal_reason = self._env.end_reason if self._env is not None and self._env.done else None
                    self.stop(
                        row["episode_id"],
                        reason="wall_budget_halt_error" if halt_failed else terminal_reason or "wall_budget",
                        infrastructure_error=halt_failed,
                    )

    def close(self):
        with self._lock:
            self._closed = True
            if self._active:
                self.stop(self._active["episode_id"], reason="session_closed")
        self._quit.set()
        if threading.current_thread() is not self._watchdog:
            self._watchdog.join(timeout=1)
