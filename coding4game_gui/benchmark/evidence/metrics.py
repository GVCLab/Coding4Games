"""Append Direct episode scores with cumulative tics, tokens and wall time."""
from __future__ import annotations

import json
import time
from pathlib import Path

from coding4game_gui.benchmark.evidence._lock import locked


def append_jsonl(path: Path, row: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        with locked(f):
            f.write(json.dumps(row) + "\n")
            f.flush()


class MetricsWriter:
    def __init__(self, path: Path, arm: str, model: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.arm, self.model = arm, model
        self.tokens_in = 0
        self.tokens_out = 0
        self.llm_calls = 0
        self.env_tics = 0
        self.t0 = time.perf_counter()

    def add_tokens(self, tokens_in: int, tokens_out: int, calls: int = 1) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.llm_calls += calls

    def log_episode(self, episode_id: int, score: float | None, tics: float,
                    wall_s: float, error: str | None, outcome_kind: str) -> None:
        self.env_tics += tics  # Preserve fractional measured running time.
        row = {
            "arm": self.arm, "model": self.model, "round": episode_id,
            "seed": episode_id, "split": "baseline",
            "score": score, "tics": tics, "wall_s": wall_s, "error": error,
            "outcome_kind": outcome_kind,
            "tokens_in_cum": self.tokens_in, "tokens_out_cum": self.tokens_out,
            "llm_calls_cum": self.llm_calls,
            "env_tics_cum": self.env_tics,
            "bench_wall_s_cum": round(time.perf_counter() - self.t0, 2),
        }
        append_jsonl(self.path, row)
