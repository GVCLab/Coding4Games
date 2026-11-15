"""Append Direct episode scores, prompt identity and resource accounting."""
from __future__ import annotations

import hashlib
import platform
import statistics
import subprocess
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def score_stats(scores: list[float]) -> dict[str, float | None]:
    if not scores:
        return {"mean": None, "std": None, "median": None,
                "min": None, "max": None, "n": 0}
    return {"mean": statistics.mean(scores),
            "std": statistics.pstdev(scores) if len(scores) > 1 else 0.0,
            "median": statistics.median(scores),
            "min": min(scores), "max": max(scores), "n": len(scores)}


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=5,
                              cwd=Path(__file__).parent).stdout.strip() or None
    except Exception:
        return None


def _runtime_metadata() -> dict[str, str]:
    meta = {"python": sys.version.split()[0], "platform": platform.platform()}
    return meta


class TrialLedger:
    def __init__(self, path: Path, environment: str, arm: str, model: str,
                 config: dict[str, Any]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.environment, self.arm, self.model = environment, arm, model
        self.config = config
        self.git_commit = _git_commit()
        self.runtime_metadata = _runtime_metadata()

    def log_trial(self, *, phase: str, round_idx: int, policy_version: str,
                  policy_sha: str, parent_version: str | None, accepted: bool,
                  episode_ids: list[int], per_episode_scores: dict[int, float],
                  env_tics: int, env_tics_cum: int,
                  tokens_in: int, tokens_out: int,
                  tokens_in_cum: int, tokens_out_cum: int, llm_calls_cum: int,
                  wall_s: float, errors: list[str],
                  change_summary: str | None, failure_analysis: str | None,
                  next_hypothesis: str | None) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "environment": self.environment, "arm": self.arm, "model": self.model,
            "phase": phase, "round": round_idx,
            "policy_version": policy_version, "policy_sha256": policy_sha,
            "parent_version": parent_version, "accepted": accepted,
            "config": self.config, "episode_ids": episode_ids,
            "per_episode_scores": {str(k): v for k, v in per_episode_scores.items()},
            "score_stats": score_stats(list(per_episode_scores.values())),
            "episodes": len(per_episode_scores),
            "environment_steps": env_tics, "environment_steps_cum": env_tics_cum,
            "wall_clock_seconds": round(wall_s, 3), "errors": errors,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "tokens_in_cum": tokens_in_cum, "tokens_out_cum": tokens_out_cum,
            "llm_calls_cum": llm_calls_cum,
            "change_summary": change_summary,
            "failure_analysis": failure_analysis,
            "next_hypothesis": next_hypothesis,
            "git_commit": self.git_commit,
            "runtime_metadata": self.runtime_metadata,
        }
        from coding4game_gui.benchmark.evidence.metrics import append_jsonl
        append_jsonl(self.path, entry)
