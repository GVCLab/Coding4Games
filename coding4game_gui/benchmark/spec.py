"""Registered Windows scenario loading and shared public task fields."""
from __future__ import annotations
from pathlib import Path

from coding4game_gui.paths import REPO_ROOT, SCENARIO_DIR
WINDOWS_GAMES_DIR = SCENARIO_DIR
RUNTIME_DEFAULTS = {"max_tics": 2100, "max_burst": 5, "max_calls_per_ep": 25}

def load_game_spec(scenario: str, *, windows_input: str | None = None) -> dict:
    import yaml
    if not scenario or Path(scenario).name != scenario or any(c in scenario for c in "/\\\\:"):
        raise ValueError("scenario must be a registered Windows name")
    path = WINDOWS_GAMES_DIR / f"{scenario}.yaml"
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict) or spec.get("engine") != "windows":
        raise ValueError("only Windows benchmark scenarios are supported")
    spec["runtime"] = RUNTIME_DEFAULTS | spec.get("runtime", {})
    # the engine cfg name; usually the yaml's own name
    spec["runtime"].setdefault("scenario", scenario)
    from coding4game_gui.benchmark.windows.raw_actions import normalize_windows_input_mode
    from coding4game_gui.benchmark.windows.raw_actions import select_windows_gameplay
    from coding4game_gui.benchmark.windows.raw_actions import windows_kbm_catalog_entries
    from coding4game_gui.benchmark.windows.timing import resolve_tics_per_second
    from coding4game_gui.benchmark.windows.timing import resolve_max_action_tics
    from coding4game_gui.benchmark.windows.timing import resolve_passive_score_interval_tics
    from coding4game_gui.benchmark.windows.timing import resolve_start_settle_seconds
    from coding4game_gui.benchmark.windows.scorers.factory import validate_scorer_selection
    from coding4game_gui.benchmark.windows.games_config import load_game_config
    # Registration remains inspectable without another host's checkpoints.
    # ResetController loads the driver with file validation before startup.
    cfg = load_game_config(spec["driver_game"], validate_files=False)
    required = {"title", "engine", "driver_game", "gameplay", "objective",
                "episode_contract", "runtime", "startup", "lifecycle", "scorer"}
    missing = required - set(spec)
    if missing:
        raise ValueError(f"Windows benchmark spec missing fields: {sorted(missing)}")
    removed = {"brief", "lang_diagnose", "code_contract", "code_signature",
               "action_line", "buttons_csv", "step_unit", "step_unit_plural",
               "reset"} & set(spec)
    if removed:
        raise ValueError(f"obsolete Windows benchmark fields: {sorted(removed)}")
    unknown = set(spec) - required
    if unknown:
        raise ValueError(
            f"Windows benchmark spec has unsupported fields: {sorted(unknown)}")
    spec["runtime"]["tics_per_second"] = resolve_tics_per_second(
        spec["runtime"])
    spec["runtime"]["max_action_tics"] = resolve_max_action_tics(
        spec["runtime"])
    resolve_passive_score_interval_tics(spec["runtime"])
    resolve_start_settle_seconds(spec["startup"].get("start_check"))
    scorer = spec["scorer"]
    if not isinstance(scorer, dict):
        raise ValueError("Windows benchmark spec scorer must be a mapping")
    if "time_efficiency" in scorer:
        raise ValueError("Retired Windows scorer option: time_efficiency")
    scenario_kind = str(scorer.get("kind") or "").strip().lower() or None
    scenario_impl = str(
        scorer.get("implementation") or "").strip().lower() or None
    if cfg.scorer_kind is None or cfg.scorer_implementation is None:
        raise ValueError(
            f"Windows benchmark driver {spec['driver_game']!r} has no "
            "complete games/<game>.yaml scorer kind/implementation default"
        )
    kind = scenario_kind or cfg.scorer_kind
    implementation = scenario_impl or cfg.scorer_implementation
    validate_scorer_selection(
        kind, implementation, context=f"Windows benchmark {scenario!r}")
    # Expose the resolved value to the trusted runtime and diagnostics.
    scorer["kind"] = kind
    scorer["implementation"] = implementation
    fixed_catalog = [
        {"name": name.upper(), "description": action.description}
        for name, action in cfg.actions.items()]
    # Runtime selection comes from the runner, never the scenario YAML.
    # Keeping it in the resolved in-memory runtime lets descendant helpers
    # share one trusted selection without adding a game-specific key.
    input_mode = normalize_windows_input_mode(windows_input)
    spec["runtime"]["windows_input"] = input_mode
    spec["gameplay"] = select_windows_gameplay(
        spec["gameplay"], input_mode, scenario=scenario)
    if input_mode == "gamepad":
        spec["action_catalog"] = fixed_catalog
    else:
        neutral = [{
            "name": "NEUTRAL",
            "description": "Release all keyboard and mouse controls.",
        }]
        spec["action_catalog"] = neutral + windows_kbm_catalog_entries(
            input_mode)
    return spec
