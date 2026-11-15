"""Pure plans for Windows development and frozen tests."""
from dataclasses import dataclass
from pathlib import Path
import yaml

from coding4game_gui.catalog import registered_scenario_paths
from coding4game_gui.paths import SCENARIO_DIR

BENCHMARK_MODULE = "coding4game_gui.benchmark"

@dataclass(frozen=True)
class BenchmarkScenario:
    slug: str
    title: str
    engine: str
    path: Path
    driver_game: str | None = None
    tics_per_second: int | None = None

def discover_benchmark_scenarios(directory: Path = SCENARIO_DIR) -> list[BenchmarkScenario]:
    scenarios = []
    for path in registered_scenario_paths(directory):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("engine") != "windows":
                continue
            rate = (data.get("runtime") or {}).get("tics_per_second", 10)
            scenarios.append(BenchmarkScenario(
                path.stem, data.get("title", path.stem), "windows", path,
                data.get("driver_game"), rate,
            ))
        except (OSError, ValueError, yaml.YAMLError, AttributeError):
            continue
    return scenarios

def _positive(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")

def _input(scenario, mode):
    if scenario.engine != "windows":
        raise ValueError("only Windows scenarios are supported")
    if mode not in {"gamepad", "gamepad-kbm"}:
        raise ValueError("unknown Windows input mode")

@dataclass(frozen=True)
class DevelopmentPlan:
    scenario: BenchmarkScenario
    profile: Path
    out_dir: Path
    model: str
    upstream_url: str
    tic_budget: int
    wall_seconds: int
    token_limit: int
    windows_input: str = "gamepad"
    api_key_env: str = "OPENAI_API_KEY"
    agent: str = "claude"

@dataclass(frozen=True)
class ProgramTestPlan:
    scenario: BenchmarkScenario
    profile: Path
    out_dir: Path
    source_programs: Path
    tic_budget: int
    wall_seconds: int
    windows_input: str = "gamepad"
    program_id: str | None = None

def _program_args(plan, phase):
    _input(plan.scenario, plan.windows_input)
    _positive(plan.tic_budget, "tic budget")
    _positive(plan.wall_seconds, "wall seconds")
    return [phase, "--runtime-backend", "wsl2", "--profile", str(plan.profile), "--scenario", plan.scenario.slug,
            "--out-dir", str(plan.out_dir), "--tic-budget", str(plan.tic_budget),
            "--wall-seconds", str(plan.wall_seconds), "--windows-input", plan.windows_input]

def build_development_args(plan, *, execute=False):
    args = _program_args(plan, "develop")
    _positive(plan.token_limit, "token limit")
    if not plan.model.strip() or not plan.api_key_env.strip():
        raise ValueError("model and credential environment-variable name are required")
    if plan.agent not in {"claude", "codex"}:
        raise ValueError("WSL2 development supports Claude Code and Codex only")
    from urllib.parse import urlsplit
    url = urlsplit(plan.upstream_url)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("upstream must be an HTTPS URL without credentials/query/fragment")
    args += ["--agent", plan.agent, "--model", plan.model, "--upstream-url", plan.upstream_url,
             "--token-limit", str(plan.token_limit), "--api-key-env", plan.api_key_env]
    if execute:
        args += ["--execute", "--confirm-paid-run"]
    return args

def build_program_test_args(plan, *, execute=False):
    args = _program_args(plan, "test") + ["--source-programs", str(plan.source_programs)]
    if plan.program_id:
        if len(plan.program_id) != 64 or any(c not in "0123456789abcdef" for c in plan.program_id):
            raise ValueError("program ID must be a SHA-256 identifier")
        args += ["--program-id", plan.program_id]
    if execute:
        args.append("--execute")
    return args
