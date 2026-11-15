"""Direct GUI baseline: one fixed-prompt model player, no Coach or search.

The baseline isolates the value of persistent policy evolution.  Every episode
starts a fresh Player conversation with the same gameplay-only system prompt.
There is deliberately no development tic budget, workspace, submission,
checkpoint, or post-search holdout phase.  Registered per-episode lifecycle
limits remain active so this runner uses the same task boundary as the formal
Windows executable-program experiments.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

from coding4game_gui.catalog import FORMAL_SCENARIOS, input_mode
from coding4game_gui.benchmark.evidence import progress
from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.operator_config import operator_paths
from coding4game_gui.benchmark.direct.player import PlayEpisode
from coding4game_gui.benchmark.direct.player import build_direct_system
from coding4game_gui.benchmark.direct.player import play_episodes
from coding4game_gui.benchmark.evidence.ledger import TrialLedger
from coding4game_gui.benchmark.evidence.ledger import sha256_text
from coding4game_gui.benchmark.evidence.metrics import MetricsWriter
from coding4game_gui.benchmark.evidence.metrics import append_jsonl
from coding4game_gui.benchmark.windows.raw_actions import normalize_windows_input_mode


def _safe_label(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


def _prepare_windows_run(
    out_dir: Path,
    scenario: str,
    spec: dict,
    *,
    arm: str = "direct-baseline",
    model: str | None = None,
    windows_input: str = "gamepad",
    source: str = "run_direct_baseline.pre_run",
) -> None:
    """Match the formal runner's pre-run cleanup and event-log wiring."""
    from coding4game_gui.benchmark.windows import save_state as save_state
    from coding4game_gui.benchmark.evidence.event_log import ENV_VAR as WINDOWS_EVENT_ENV
    from coding4game_gui.benchmark.evidence.event_log import emit as emit_windows_event
    from coding4game_gui.benchmark.evidence.event_log import span as windows_event_span
    from coding4game_gui.benchmark.windows.games_config import load_game_config

    os.environ[WINDOWS_EVENT_ENV] = str(
        (out_dir / "windows_events.jsonl").resolve())
    driver = spec["driver_game"]
    process = load_game_config(driver).process
    emit_windows_event(
        "benchmark_run", "begin", scenario=scenario,
        arm=arm, model=model, windows_input=windows_input,
        python=sys.executable)
    with windows_event_span(
            "residual_process_cleanup", process=process,
            source=source):
        save_state.kill_process(process)


def _write_episode_rows(
    out_dir: Path,
    episodes: list[PlayEpisode],
    *,
    model: str,
    prompt_sha: str,
    scenario: str,
) -> MetricsWriter:
    metrics = MetricsWriter(
        out_dir / "metrics.jsonl", arm="direct-baseline", model=model)
    ledger = TrialLedger(
        out_dir / "trials.jsonl",
        environment=f"windows/{scenario}",
        arm="direct-baseline",
        model=model,
        config={
            "mode": "direct_baseline",
            "policy_evolution": False,
            "identifier_kind": "episode",
        },
    )
    for episode in episodes:
        ident = episode.episode_id
        metrics.add_tokens(
            episode.tokens_in, episode.tokens_out, episode.calls)
        complete = episode.outcome_kind != "infrastructure_error"
        score = episode.score if complete else None
        metrics.log_episode(
            episode_id=int(ident), score=score,
            tics=episode.tics, wall_s=episode.wall_s, error=episode.error,
            outcome_kind=episode.outcome_kind)

        row = {
            "phase": "baseline",
            "round": int(ident),
            "episode_id": int(ident),
            "policy_version": "fixed_direct_prompt",
            "prompt_sha256": prompt_sha,
            "score": score,
            "score_version": episode.score_version,
            "process_score": episode.process_score,
            "final_component": episode.final_component,
            "combined_score": episode.combined_score,
            "net_score": episode.net_score,
            "game_score": episode.game_score,
            "success": episode.success,
            "partial_score": None if complete else episode.score,
            "tics": episode.tics,
            "budget_overshoot_tics": episode.budget_overshoot_tics,
            "game_running_seconds": episode.game_running_seconds,
            "wall_s": episode.wall_s,
            "error": episode.error,
            "outcome_kind": episode.outcome_kind,
            "end_reason": episode.end_reason,
            "calls": episode.calls,
            "tokens_in": episode.tokens_in,
            "tokens_out": episode.tokens_out,
            "invalid_action_replies": episode.invalid_action_replies,
            "actions": episode.actions,
        }
        if episode.uid:
            row["uid"] = episode.uid
        append_jsonl(out_dir / "episodes.jsonl", row)

        ledger.log_trial(
            phase="baseline", round_idx=int(ident),
            policy_version="fixed_direct_prompt", policy_sha=prompt_sha,
            parent_version=None, accepted=complete,
            episode_ids=[int(ident)],
            per_episode_scores={int(ident): episode.score} if complete else {},
            env_tics=episode.tics, env_tics_cum=metrics.env_tics,
            tokens_in=episode.tokens_in, tokens_out=episode.tokens_out,
            tokens_in_cum=metrics.tokens_in,
            tokens_out_cum=metrics.tokens_out,
            llm_calls_cum=metrics.llm_calls, wall_s=episode.wall_s,
            errors=[episode.error] if episode.error else [],
            change_summary="fixed gameplay-only prompt; no policy evolution",
            failure_analysis=None, next_hypothesis=None)
    return metrics


def run_baseline(
    *,
    out_dir: Path,
    scenario: str,
    episodes: int,
    windows_input: str = "gamepad",
    player_model: str,
    llm_log: bool = True,
    llm_fn=None,
    make_env=None,
    prepare_windows: bool = True,
    max_tics: int | None = None,
) -> dict:
    """Run and ledger a fixed-prompt baseline; dependencies are injectable.

    Passing ``llm_fn`` and ``make_env`` allows token-free unit tests.  The CLI
    leaves both unset and uses the LiteLLM Player bridge and the Windows
    benchmark environment.
    """
    if episodes <= 0:
        raise ValueError("episodes must be positive")
    windows_input = normalize_windows_input_mode(windows_input)
    spec = (load_game_spec(scenario) if windows_input == "gamepad" else
            load_game_spec(scenario, windows_input=windows_input))
    if spec.get("engine") != "windows":
        raise ValueError("only Windows baseline scenarios are supported")
    runtime = spec.get("runtime") or {}
    if not player_model.strip():
        raise ValueError("player_model must be nonempty")
    max_burst = runtime.get("max_burst", 5)
    max_calls = runtime.get("max_calls_per_ep", 25)
    if max_tics is not None and (type(max_tics) is not int or max_tics <= 0):
        raise ValueError("max_tics must be a positive integer")
    requested_max_tics = max_tics
    caps = [cap for cap in (max_tics, runtime.get("max_tics")) if cap is not None]
    max_tics = min(caps) if caps else None
    system_prompt = build_direct_system(spec, max_burst)
    from coding4game_gui.benchmark.windows.terminal import validate_terminal_detection
    terminal_detection = validate_terminal_detection(spec)
    if terminal_detection:
        system_prompt += ("\nThe host ends this Step episode when the frozen action frame "
                          "shows a registered failure or completion screen. Never dismiss "
                          "an episode-ending screen or restart a finished run.\n")
    prompt_sha = sha256_text(system_prompt)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    (out_dir / "system_prompt.md").write_text(system_prompt, encoding="utf-8")
    if llm_log:
        os.environ["C4G_LLM_LOG"] = str(
            (out_dir / "llm_calls.jsonl").resolve())
    else:
        os.environ.pop("C4G_LLM_LOG", None)
    os.environ["C4G_LLM_PHASE"] = "baseline"

    if prepare_windows:
        _prepare_windows_run(
            out_dir, scenario, spec, model=player_model,
            windows_input=windows_input)

    config = {
        "mode": "direct_baseline",
        "protocol": "direct-stepped",
        "terminal_detection": "registered_terminal" if terminal_detection else "disabled",
        "stepped_terminal_sampling": (spec.get('lifecycle') or {}).get('stepped_terminal_sampling', 'action_end_v1'),
        "results_recognition_version": (spec.get('scorer') or {}).get('recognition_version'),
        "score_version": (spec.get('scorer') or {}).get('score_version'),
        "scenario": scenario,
        "engine": "windows",
        "episodes": episodes,
        "concurrency": 1,
        "player_model": player_model,
        "player_backend": "litellm",
        "policy_evolution": False,
        "execution_isolation": "validated-action-protocol",
        "untrusted_code_execution": False,
        "game_location": "host",
        "sandbox": "not-applicable-no-generated-code",
        "per_episode_max_tics": max_tics,
        "requested_max_tics": requested_max_tics,
        "max_calls_per_ep": max_calls,
        "max_burst": max_burst,
        "windows_input": windows_input,
        "system_prompt_sha256": prompt_sha,
        "llm_log": os.environ.get("C4G_LLM_LOG"),
        "episode_ids": list(range(episodes)),
        "tics_per_second": runtime.get("tics_per_second"),
    }
    (out_dir / "config.json").write_text(
        json.dumps(config, indent=2), encoding="utf-8")

    if llm_fn is None:
        from coding4game_gui.benchmark.direct.player_bridge import _player_bridge
        llm_fn = _player_bridge(player_model)
    if make_env is None:
        from coding4game_gui.benchmark.windows.env import make_bench_env
        if windows_input != "gamepad":
            make_env = lambda: make_bench_env(  # noqa: E731
                scenario, windows_input=windows_input)
        else:
            make_env = lambda: make_bench_env(scenario)  # noqa: E731

    started = time.perf_counter()
    from coding4game_gui.benchmark.windows.save_session import benchmark_save
    from contextlib import nullcontext
    protection = (benchmark_save(spec, out_dir / 'save-protection')
                  if prepare_windows else nullcontext())
    with protection:
        played = play_episodes(
            make_env, llm_fn, player_model, system_prompt, episodes=episodes,
            max_burst=max_burst, max_calls_per_ep=max_calls, max_tics=max_tics,
            requested_max_tics=requested_max_tics,
            transcript=out_dir / "transcript.md", evidence_root=out_dir / "evidence")
    metrics = _write_episode_rows(
        out_dir, played, model=player_model, prompt_sha=prompt_sha,
        scenario=scenario)

    complete_scores = [episode.score for episode in played
                       if episode.outcome_kind != "infrastructure_error" and episode.score is not None]
    accepted = (len(played) == episodes and
                all(episode.outcome_kind != "infrastructure_error"
                    for episode in played))
    config |= {
        "accepted": accepted,
        "episodes_completed": len(played),
        "scored_episodes": len(complete_scores),
        "baseline_mean": (statistics.mean(complete_scores)
                          if complete_scores else None),
        "player_tokens_in": metrics.tokens_in,
        "player_tokens_out": metrics.tokens_out,
        "player_llm_calls": metrics.llm_calls,
        "environment_tics": metrics.env_tics,
        "wall_s": round(time.perf_counter() - started, 3),
    }
    (out_dir / "config.json").write_text(
        json.dumps(config, indent=2), encoding="utf-8")

    if prepare_windows:
        from coding4game_gui.benchmark.evidence.event_log import emit as emit_windows_event
        emit_windows_event(
            "benchmark_run", "end", scenario=scenario,
            arm="direct-baseline", windows_input=windows_input,
            accepted=accepted,
            episodes=len(played), baseline_mean=config["baseline_mean"],
            tics_used=metrics.env_tics)
    return config


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Run a fixed-prompt stepped Windows Direct baseline")
    parser.add_argument("--scenario", choices=FORMAL_SCENARIOS, required=True)
    parser.add_argument("--episodes", type=int, default=3,
                        help="independent fresh-conversation evaluation episodes")
    parser.add_argument("--player-model", required=True, help="Configured LiteLLM model")
    parser.add_argument("--max-tics", type=int, default=10000,
                        help="Positive per-episode tic cap, also bounded by the registered cap")
    parser.add_argument(
        "--windows-input", choices=["gamepad", "gamepad-kbm"],
        help="Physical controls. Defaults to the scenario's registered input.")
    parser.add_argument("--no-llm-log", action="store_true",
                        help="disable full provider-visible Player call logging")
    parser.add_argument("--out-root", default=None,
                        help="Output root, defaulting to launcher.local.json")
    parser.add_argument("--confirm-paid-run", action="store_true",
                        help="acknowledge real model billing and game side effects")
    return parser


def main(argv=None) -> None:
    parser = _build_argparser()
    args = parser.parse_args(argv)
    if args.max_tics <= 0:
        parser.error('--max-tics must be positive')
    if args.episodes <= 0:
        parser.error("--episodes must be positive")
    if not args.player_model.strip():
        parser.error("--player-model must be nonempty")
    if not args.confirm_paid_run:
        parser.error("real baseline execution requires --confirm-paid-run")

    model = args.player_model
    windows_input = normalize_windows_input_mode(args.windows_input or input_mode(args.scenario))
    stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    run_name = (
        f"{stamp}_{args.scenario}_direct-baseline_{_safe_label(model)}_input-{windows_input}"
    )
    out_dir = Path(args.out_root or operator_paths()["output_root"]) / run_name
    progress.banner(
        "DIRECT BASELINE",
        f"{args.scenario} | player={model} | {args.episodes} fresh episode(s) | "
        f"input={windows_input}")
    result = run_baseline(
        out_dir=out_dir, scenario=args.scenario, episodes=args.episodes,
        windows_input=windows_input,
        player_model=args.player_model, llm_log=not args.no_llm_log, max_tics=args.max_tics)
    mean = result["baseline_mean"]
    mean_text = "n/a" if mean is None else f"{mean:+.2f}"
    progress.banner(
        "DONE",
        f"accepted={result['accepted']} | baseline_mean={mean_text} | "
        f"episodes={result['episodes_completed']}/{args.episodes}")
    progress.note(
        f"player tokens: {result['player_tokens_in']:_} in / "
        f"{result['player_tokens_out']:_} out | results in {out_dir}")
    print(
        f"DONE arm=direct-baseline accepted={result['accepted']} "
        f"baseline_mean={mean} episodes={result['episodes_completed']} "
        f"player_tokens_out={result['player_tokens_out']} out={out_dir}")
    raise SystemExit(0 if result["accepted"] else 1)


if __name__ == "__main__":
    main()
