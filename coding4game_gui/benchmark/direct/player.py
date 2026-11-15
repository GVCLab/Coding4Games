"""Windows screenshot/action player used by the fixed Direct GUI baseline."""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from coding4game_gui.benchmark.policy_observation import MODEL_IMAGE_HISTORY_MAX_BYTES
from coding4game_gui.benchmark.policy_observation import MODEL_IMAGE_HISTORY_MAX_COUNT
from coding4game_gui.benchmark.policy_observation import encode_policy_image
from coding4game_gui.benchmark.policy_observation import window_model_images
from coding4game_gui.benchmark.evidence.event_log import emit as emit_windows_event
from coding4game_gui.benchmark.evidence.event_log import emit_error as emit_windows_error
from coding4game_gui.benchmark.evidence.event_log import span as windows_event_span

from coding4game_gui.benchmark.evidence import progress
from coding4game_gui.benchmark.windows.raw_actions import parse_windows_acts
from coding4game_gui.benchmark.windows.timing import STEP_BUDGET_TOLERANCE_TICS
from coding4game_gui.benchmark.evidence.call_context import clear_episode
from coding4game_gui.benchmark.evidence.call_context import set_episode
from coding4game_gui.benchmark.direct.llm import PlayerInfrastructureError
from coding4game_gui.benchmark.direct.llm import image_block
from coding4game_gui.benchmark.direct.llm import text_block

MAX_CONSECUTIVE_INVALID_ACTION_REPLIES = 3


def _windows_action_protocol(spec: dict) -> str:
    """Render raw-action guidance with the scenario's effective action cap."""
    from coding4game_gui.benchmark.windows.raw_actions import windows_action_protocol
    from coding4game_gui.benchmark.windows.timing import resolve_max_action_tics

    runtime = spec.get("runtime") or {}
    cap = resolve_max_action_tics(runtime)
    protocol = windows_action_protocol(runtime.get("windows_input"))
    return protocol.replace(
        "integer from 1 to 1000", f"integer from 1 to {cap}"
    ).replace(
        "The per-action cap is 1000 tics",
        f"The per-action cap is {cap} tics",
    )


def _direct_player_text(value: object) -> str:
    """Make shared scenario prose accurate for a no-search direct Player."""
    text = str(value or "").rstrip()
    text = re.sub(r"\s*\(small budget\)", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r",?\s*(?:or\s+)?global tic[- ]budget\s+exhaustion",
        "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\s+or\s+when\s+the\s+(?:global\s+)?budget\s+runs\s+out",
        "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bthe policy\b", "you", text, flags=re.IGNORECASE)
    text = re.sub(r"\ba policy\b", "a player", text, flags=re.IGNORECASE)
    text = re.sub(r"\bpolicy must\b", "You must", text, flags=re.IGNORECASE)
    text = re.sub(r"\bpolicy receives\b", "You receive", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"\bpolicy tic\b", "episode tic", text, flags=re.IGNORECASE)
    return text


def build_direct_system(spec: dict, max_burst: int | None) -> str:
    """Build the frozen gameplay-only prompt for the Direct GUI baseline.

    This intentionally consumes only player-facing scenario fields and the
    action protocol.  Coach instructions, code contracts, diagnosis hints,
    workspace rules, and search-budget information have no path into it.
    """
    if spec.get("engine") != "windows":
        raise ValueError("Direct prompt requires a Windows scenario")
    burst = ("one or more complete action lines" if max_burst is None else
             f"1 to {max_burst} complete action line(s)")
    sections = [
        f"You are directly PLAYING {spec['title']} from screenshots, one "
        "decision at a time. Make the best gameplay decisions you can.",
    ]
    gameplay = _direct_player_text(
        spec.get("gameplay"))
    if gameplay:
        sections.append("# Game\n" + gameplay)
    objective = _direct_player_text(spec.get("objective"))
    if objective:
        sections.append("# Goal and scoring\n" + objective)
    # Agentic contracts may name the global DEVELOPMENT budget.  Direct has no
    # such budget, so the player-facing normalization removes that termination
    # mode while retaining registered per-episode/lifecycle endings.
    episode_contract = _direct_player_text(spec.get("episode_contract"))
    if episode_contract:
        sections.append("# Episode contract\n" + episode_contract)

    catalog = spec.get("action_catalog", [])
    action_lines = [
        f"- `{action['name']}`: {action['description']}"
        for action in catalog
    ]
    controls = "# Available controls\n" + (
        "\n".join(action_lines) if action_lines else "No fixed controls declared."
    )
    protocol = (
        "# Reply protocol\n"
        f"Each turn you receive the current screenshot and episode/tic status. "
        f"Reply with {burst}, executed in order before the next screenshot. "
        "Do not reply with prose instead of ACT lines. If an ACTION_ERROR is "
        "returned, correct the entire reply; no listed action from the rejected "
        "reply was executed.\n\n" + _windows_action_protocol(spec).rstrip()
    )
    sections.extend([controls, protocol])
    return "\n\n".join(sections).rstrip() + "\n"


@dataclass
class PlayEpisode:
    episode_id: int
    score: float | None = None
    score_version: str | None = None
    process_score: float | None = None
    final_component: float | None = None
    combined_score: float | None = None
    net_score: float | None = None
    game_score: float | None = None
    success: bool | None = None
    tics: int = 0
    game_running_seconds: float | None = None
    budget_overshoot_tics: int = 0
    error: str | None = None
    outcome_kind: str = "valid"
    end_reason: str | None = None
    wall_s: float = 0.0
    uid: str | None = None  # the episode uid (matches llm_calls.jsonl episode_uid)
    actions: list = field(default_factory=list)  # (t, buttons, tics, reward)
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    infrastructure_attempts: int = 0
    invalid_action_replies: int = 0
    resolved_max_burst: int | None = None
    requested_max_tics: int | None = None
    resolved_max_tics: int | None = None
    replies: list = field(default_factory=list)  # (tic, reply) — collected, not written


def play_one(env, llm_fn, model: str, system: str, *, episode_id: int,
             max_burst: int | None, max_calls_per_ep: int | None,
             max_tics: int | None = None, requested_max_tics: int | None = None,
             evidence_root: Path | None = None) -> PlayEpisode:
    """Run one Windows episode with a fresh conversation and deferred scoring."""
    uid = uuid.uuid4().hex[:8]
    out = PlayEpisode(episode_id, uid=uid, resolved_max_burst=max_burst,
                      requested_max_tics=requested_max_tics, resolved_max_tics=max_tics)
    t_start = time.perf_counter()
    reset_fields = {"episode_id": episode_id, "ep": episode_id, "episode_uid": uid}
    emit_windows_event("episode_reset", "begin", **reset_fields)
    try:
        env.reset(episode_id=episode_id)
        start_evidence = getattr(env, "start_terminal_evidence", None)
        if evidence_root is not None and start_evidence is not None:
            start_evidence(evidence_root / uid / "terminal", uid)
    except Exception as error:
        emit_windows_error("episode_reset", error, **reset_fields)
        out.error = f"{type(error).__name__}: {error}"
        out.outcome_kind = "infrastructure_error"
        out.end_reason = "episode_reset_error"
        out.wall_s = time.perf_counter() - t_start
        finish = getattr(env, "finish_episode", None)
        if finish is not None:
            try:
                with windows_event_span("episode_cleanup", source="reset_error", **reset_fields):
                    finish()
            except Exception as cleanup_error:
                out.error += f"; cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}"
        return out
    emit_windows_event("episode_reset", "end", **reset_fields)
    set_episode(None, episode_id, uid, episode_id=episode_id)
    emit_windows_event("episode", "begin", episode_id=episode_id, max_tics=max_tics)
    elapsed = 0
    # Stateful WITHIN an episode: one growing conversation accumulates every
    # (frame, reply) decision point, mirroring the code arm's module globals
    # (persist within a life, reset at env.reset()). `messages` is local to this
    # play_one call, so it is naturally fresh for each episode.
    messages: list[dict] = []
    image_window_reported = False
    consecutive_invalid_replies = 0
    pending_action_error: str | None = None

    def reject_player_reply(error: str, *, event: str,
                            exc: ValueError | None = None) -> bool:
        """Record an invalid Windows reply and return whether it may retry."""
        nonlocal consecutive_invalid_replies, pending_action_error
        out.invalid_action_replies += 1
        consecutive_invalid_replies += 1
        retry_allowed = (
            consecutive_invalid_replies
            < MAX_CONSECUTIVE_INVALID_ACTION_REPLIES
            and (max_calls_per_ep is None or out.calls < max_calls_per_ep)
        )
        fields = {
            "tic": tic_now,
            "call_index": out.calls,
            "outcome_kind": "action_error",
            "recoverable": retry_allowed,
            "strike": consecutive_invalid_replies,
            "max_strikes": MAX_CONSECUTIVE_INVALID_ACTION_REPLIES,
        }
        if exc is None:
            emit_windows_event(event, "error", error=error, **fields)
        else:
            emit_windows_error(event, exc, **fields)
        if retry_allowed:
            pending_action_error = (
                f"ACTION_ERROR: {error}\n"
                "No action was executed and no game tics were consumed. "
                "Reissue valid ACT line(s). "
                f"Strike {consecutive_invalid_replies} of "
                f"{MAX_CONSECUTIVE_INVALID_ACTION_REPLIES}."
            )
            return True
        out.error = error
        out.outcome_kind = "action_error"
        out.end_reason = (
            "invalid_action_replies_exhausted"
            if consecutive_invalid_replies
            >= MAX_CONSECUTIVE_INVALID_ACTION_REPLIES
            else "max_calls_per_ep_reached_after_invalid_reply"
        )
        return False

    try:
        while (not env.done
               and (max_calls_per_ep is None or out.calls < max_calls_per_ep)
               and (max_tics is None or elapsed < max_tics)):
            image_bytes, media_type = encode_policy_image(env.frame())
            status = f"Episode {episode_id}, tic {env.tic}."
            if pending_action_error is not None:
                status += "\n\n" + pending_action_error
                pending_action_error = None
            tic_now = env.tic
            messages.append({"role": "user",
                             "content": [text_block(status),
                                         image_block(image_bytes, media_type)]})
            request_messages, omitted_images, kept_image_bytes = (
                window_model_images(messages))
            if omitted_images and not image_window_reported:
                progress.note(
                    f"player image window: keeping at most "
                    f"{MODEL_IMAGE_HISTORY_MAX_COUNT} recent image(s) / "
                    f"{MODEL_IMAGE_HISTORY_MAX_BYTES / (1024 * 1024):.0f} MiB "
                    f"({kept_image_bytes / (1024 * 1024):.1f} MiB kept)")
                image_window_reported = True
            emit_windows_event(
                "player_decision", "begin", tic=tic_now,
                call_index=out.calls + 1,
                message_count=len(request_messages),
                historical_images_omitted=omitted_images,
                kept_image_bytes=kept_image_bytes)
            try:
                reply, tin, tout = llm_fn(model, system, request_messages)
            except PlayerInfrastructureError as exc:
                out.calls += 1
                out.infrastructure_attempts = exc.attempts
                out.outcome_kind = "infrastructure_error"
                out.end_reason = "infrastructure_error"
                out.error = f"{type(exc).__name__}: {exc}"
                emit_windows_error(
                    "player_decision", exc, tic=tic_now,
                    call_index=out.calls, message_count=len(messages),
                    outcome_kind=out.outcome_kind)
                break
            except BaseException as exc:
                emit_windows_error(
                    "player_decision", exc, tic=tic_now,
                    call_index=out.calls + 1,
                    message_count=len(messages))
                raise
            emit_windows_event(
                "player_decision", "end", tic=tic_now,
                call_index=out.calls + 1, reply_chars=len(reply),
                tokens_in=tin, tokens_out=tout)
            print(f"[player reply episode {episode_id} tic {tic_now}] {reply}")
            messages.append({"role": "assistant", "content": reply})
            out.tokens_in += tin
            out.tokens_out += tout
            out.calls += 1
            out.replies.append((tic_now, reply))
            try:
                acts = parse_windows_acts(reply, max_burst)
            except ValueError as error:
                if reject_player_reply(
                        f"{type(error).__name__}: {error}", event="player_reply_parse", exc=error):
                    continue
                break
            if not acts:
                if reject_player_reply("no ACT lines parsed", event="player_reply_parse"):
                    continue
                break
            if hasattr(env, "validate_action_plan"):
                try:
                    env.validate_action_plan(acts)
                except ValueError as error:
                    if reject_player_reply(
                            f"{type(error).__name__}: {error}", event="player_reply_validate", exc=error):
                        continue
                    break
            consecutive_invalid_replies = 0
            for buttons, tics in acts:
                if env.done:
                    break
                t_before = env.tic
                if max_tics is not None:
                    rollout_remaining = max_tics - elapsed
                    if rollout_remaining <= 0:
                        out.end_reason = f"max_tics ({max_tics}) reached"
                        break
                    tics = min(tics, rollout_remaining)
                try:
                    emit_windows_event(
                        "action", "begin", tic=t_before,
                        buttons=buttons, requested_tics=tics)
                    last_reward = env.act(buttons, int(tics))
                    emit_windows_event(
                        "action", "end", tic_before=t_before,
                        tic_after=env.tic, buttons=buttons,
                        requested_tics=tics, reward=last_reward,
                        done=env.done)
                except Exception as e:  # noqa: BLE001 — containment, like the code arm
                    emit_windows_error(
                        "action", e, tic=t_before, buttons=buttons,
                        requested_tics=tics)
                    out.error = f"{type(e).__name__}: {e}"
                    out.outcome_kind = "action_error"
                    elapsed = env.tic
                    break
                elapsed = env.tic
                out.actions.append((t_before, buttons, tics, last_reward))
                if max_tics is not None and elapsed >= max_tics and not env.done:
                    out.end_reason = f"max_tics ({max_tics}) reached"
                    break
            if out.error:
                break
        out.score = env.total_reward
        out.tics = elapsed
        out.game_running_seconds = getattr(env, "game_running_seconds", None)
        if max_tics is not None:
            out.budget_overshoot_tics = max(0, elapsed - max_tics)
        if out.budget_overshoot_tics > STEP_BUDGET_TOLERANCE_TICS and not out.error:
            out.outcome_kind = "infrastructure_error"
            out.end_reason = "budget_overshoot"
            out.error = f"Game tic budget exceeded by {out.budget_overshoot_tics} tics"
        out.end_reason = out.end_reason or getattr(env, "end_reason", None)
        out.wall_s = time.perf_counter() - t_start
    finally:
        settle = getattr(env, 'finalize_score', None)
        if settle is not None:
            try:
                out.score = settle()
                save_score = getattr(env, "save_score_evidence", None)
                if evidence_root is not None and save_score is not None:
                    save_score(evidence_root / uid / "settlement")
                components = getattr(env, 'score_breakdown', None)
                if components is not None and components.get('score_version'):
                    for name in ('score_version', 'process_score', 'final_component',
                                 'combined_score', 'net_score', 'game_score', 'success'):
                        setattr(out, name, components.get(name))
            except Exception as exc:
                out.score = None
                out.error = out.error or f"episode scoring failed: {type(exc).__name__}: {exc}"
                out.outcome_kind = 'infrastructure_error'
        emit_windows_event(
            "episode_cleanup", "begin", source="play_one.finally",
            tic=getattr(env, "tic", None), error=out.error,
            end_reason=out.end_reason, outcome_kind=out.outcome_kind)
        if hasattr(env, "finish_episode"):
            try:
                env.finish_episode()
            except Exception as e:  # noqa: BLE001
                emit_windows_error(
                    "episode_finish", e, source="play_one.finally")
                out.error = out.error or f"episode cleanup: {type(e).__name__}: {e}"
                out.outcome_kind = "infrastructure_error"
        emit_windows_event(
            "episode_cleanup", "end", source="play_one.finally",
            tic=out.tics, score=out.score, error=out.error,
            end_reason=out.end_reason, outcome_kind=out.outcome_kind)
        emit_windows_event(
            "episode", "end", tic=out.tics, score=out.score,
            calls=out.calls, error=out.error, end_reason=out.end_reason,
            outcome_kind=out.outcome_kind)
        clear_episode()
    return out


def play_episodes(make_env, llm_fn, model: str, system: str, *, episodes: int,
                  max_burst: int | None, max_calls_per_ep: int | None,
                  max_tics: int | None = None, requested_max_tics: int | None = None,
                  transcript: Path | None = None,
                  evidence_root: Path | None = None) -> list[PlayEpisode]:
    """Run serial episodes on the single Windows desktop."""
    with windows_event_span("environment_create", index=0, windows=True, requested_concurrency=1):
        env = make_env()
    results = []
    try:
        for episode_id in range(episodes):
            emit_windows_event(
                "episode_dispatch", "begin", episode_id=episode_id, ep=episode_id,
                max_tics=max_tics)
            progress.play_start(episode_id, episode_id, episodes, label="episode")
            result = play_one(
                env, llm_fn, model, system, episode_id=episode_id, max_burst=max_burst,
                max_calls_per_ep=max_calls_per_ep, max_tics=max_tics,
                requested_max_tics=requested_max_tics, evidence_root=evidence_root)
            results.append(result)
            progress.play_done(episode_id, result.score, result.tics, result.calls,
                               result.error, label="episode")
            emit_windows_event(
                "episode_dispatch", "end", episode_id=episode_id, ep=episode_id,
                score=result.score, tics=result.tics, calls=result.calls,
                error=result.error, end_reason=result.end_reason, outcome_kind=result.outcome_kind)
            monitor = getattr(env, "_terminal_monitor", None)
            if monitor is not None and monitor.failure:
                break
    finally:
        close = getattr(env, "close", None)
        if close is not None:
            close()

    if transcript is not None:
        with transcript.open("a", encoding="utf-8") as stream:
            for result in results:
                for tic, reply in result.replies:
                    stream.write(f"\n# episode{result.episode_id} (tic {tic})\n{reply}\n")
    return results
