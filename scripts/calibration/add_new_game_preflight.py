"""Audit whether a real Windows game is ready for a scored benchmark.

This command never launches a game, restores a save, or calls an LLM. It
classifies every missing artifact as:

  pass    statically consistent with files
  auto    Codex can generate or repair it without game-state knowledge
  manual  the user must define a semantic fact or perform an in-game action
  error   present configuration is structurally inconsistent
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter
from importlib import metadata
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
# A direct script invocation must inspect its own checkout, even when Python's
# editable installation points at another worktree.
sys.path.insert(0, str(REPO_ROOT))
STATUSES = ("pass", "auto", "manual", "error")
WINDOWS_BENCH_KEYS = {
    "title", "engine", "driver_game", "gameplay", "objective",
    "episode_contract", "runtime", "startup", "lifecycle", "scorer",
}


def _check(checks: list[dict], ident: str, status: str, detail: str,
           command: str | None = None, *, blocks_config: bool = False) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown audit status {status!r}")
    item = {"id": ident, "status": status, "detail": detail}
    if command:
        item["command"] = command
    if blocks_config:
        item["blocks_config"] = True
    checks.append(item)


def _read_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("top level must be a mapping")
    return data


def _resolved_matches(repo_root: Path, raw: object) -> list[Path]:
    if not raw:
        return []
    from coding4game_gui.benchmark.windows.steam_paths import expand_steam_userdata
    expanded = os.path.expandvars(os.path.expanduser(
        expand_steam_userdata(str(raw), required=False)))
    path = Path(expanded)
    if not path.is_absolute():
        path = repo_root / path
    if "*" in str(path) or "?" in str(path):
        return [Path(hit) for hit in sorted(glob.glob(str(path)))]
    return [path] if path.exists() else []


def _golden_path(repo_root: Path, game: str, driver: dict,
                 bench: dict | None) -> Path | None:
    save = driver.get("save_state") or {}
    if save.get("golden"):
        raw = Path(str(save["golden"]))
        return raw if raw.is_absolute() else repo_root / raw
    checkpoint = ((bench or {}).get("startup") or {}).get("golden_checkpoint")
    if not checkpoint and save.get("restore_to"):
        checkpoint = "golden"
    if not checkpoint:
        return None
    return repo_root / "snapshots" / f"{game}_{checkpoint}.zip"


def _check_time_control_resources(checks: list[dict], driver: dict,
                                  launch_path: Path | None) -> None:
    """Check required timing files without importing or executing an injector."""
    if driver.get("use_speedhack") is not True:
        return
    if launch_path is None:
        _check(checks, "time_control_resources", "auto",
               "resolve the launch executable before checking required timing resources")
        return
    try:
        with Path(launch_path).open("rb") as executable:
            header = executable.read(64)
            if len(header) != 64 or header[:2] != b"MZ":
                raise ValueError("missing or truncated DOS header")
            offset = int.from_bytes(header[60:64], "little")
            if not 64 <= offset <= Path(launch_path).stat().st_size - 6:
                raise ValueError("invalid PE header offset")
            executable.seek(offset)
            coff = executable.read(6)
            if coff[:4] != b"PE\0\0":
                raise ValueError("missing PE signature")
            machine = int.from_bytes(coff[4:6], "little")
            arch = {0x14C: "x86", 0x8664: "x64"}.get(machine)
            if arch is None:
                raise ValueError(f"unsupported PE machine 0x{machine:04x}")
    except (OSError, ValueError) as exc:
        _check(checks, "time_control_resources", "error",
               f"cannot determine required timing architecture: {exc}")
        return
    try:
        package = metadata.distribution("xspeedhack")
        binaries = Path(package.locate_file("xspeedhack/bin"))
        bits = "32" if arch == "x86" else "64"
        required = [binaries / f"injector{bits}.exe",
                    binaries / f"speedhack{bits}.dll"]
        missing = [str(path) for path in required
                   if not path.is_file() or path.stat().st_size == 0]
    except (metadata.PackageNotFoundError, OSError) as exc:
        _check(checks, "time_control_resources", "error",
               f"required xspeedhack installation is unavailable: {type(exc).__name__}")
        return
    _check(
        checks, "time_control_resources", "error" if missing else "pass",
        (f"required {arch} timing resources missing or empty: {', '.join(missing)}; "
         "review the installation and security history" if missing else
         f"timing resources present for the {arch} launch executable; "
         "native attachment and freeze acceptance remain required"),
    )


def audit_game(repo_root: Path, game: str,
               scenario: str | None = None) -> dict:
    repo_root = Path(repo_root).resolve()
    scenario = scenario or game
    checks: list[dict] = []
    driver_path = repo_root / "games" / f"{game}.yaml"
    bench_path = (repo_root / "coding4game_gui" / "benchmark" / "scenarios" / f"{scenario}.yaml")

    if not driver_path.is_file():
        _check(checks, "driver_yaml", "auto",
               f"missing {driver_path.relative_to(repo_root)}; scaffold it")
        driver = {}
    else:
        try:
            from coding4game_gui.benchmark.windows.local_paths import read_driver_data
            driver = read_driver_data(driver_path)
            _check(checks, "driver_yaml", "pass",
                   str(driver_path.relative_to(repo_root)))
        except Exception as exc:  # noqa: BLE001 - audit should report all errors
            driver = {}
            _check(checks, "driver_yaml", "error",
                   f"cannot parse driver YAML: {type(exc).__name__}: {exc}")

    required_driver = [key for key in ("name", "process", "actions")
                       if not driver.get(key)]
    driver_parse_error = None
    if driver:
        try:
            from coding4game_gui.benchmark.windows.games_config import parse_game_config
            parse_game_config(driver, driver_path)
        except Exception as exc:  # noqa: BLE001 - return the whole audit
            driver_parse_error = f"{type(exc).__name__}: {exc}"
    _check(checks, "driver_contract",
           "error" if required_driver or driver_parse_error else "pass",
           (("missing driver fields: " + ", ".join(required_driver))
            if required_driver else
            (f"driver parser rejected config: {driver_parse_error}"
             if driver_parse_error else "driver config parses successfully")))

    action_durations = []
    for name, action in (driver.get("actions") or {}).items():
        if isinstance(action, dict):
            bad = sorted({"duration_seconds", "duration_tics"} & set(action))
            if bad:
                action_durations.append(f"{name}: {', '.join(bad)}")
    _check(
        checks, "action_duration_contract",
        "error" if action_durations else "pass",
        ("gameplay actions contain forbidden duration fields: " +
         "; ".join(action_durations) if action_durations else
         "gameplay action duration comes only from ACT <tics>"),
    )

    launch = driver.get("auto_launch") or {}
    launch_path = str(launch.get("launch_path") or "").strip()
    legacy_steam = bool(launch.get("steam_url"))
    resolved_launch_path = None
    if launch_path:
        from types import SimpleNamespace
        from coding4game_gui.benchmark.windows.game_launch import resolve_launch_path
        try:
            resolved_launch_path = resolve_launch_path(SimpleNamespace(launch_path=launch_path))
        except (ValueError, RuntimeError, OSError):
            pass
    launch_ok = resolved_launch_path is not None
    _check(
        checks,
        "auto_launch",
        "error" if legacy_steam else ("pass" if launch_ok else "auto"),
        ("steam_url is disabled; replace it with the installed executable path"
         if legacy_steam else
         (f"direct executable: {resolved_launch_path}"
          + (" (desktop_shell)" if launch.get("launch_method") == "desktop_shell" else "")
          if launch_ok else
          "discover the installed executable launch path")),
    )
    _check_time_control_resources(checks, driver, resolved_launch_path)

    save = driver.get("save_state") or {}
    if save:
        save_hits = _resolved_matches(repo_root, save.get("restore_to"))
        _check(checks, "save_directory", "pass" if save_hits else "manual",
               (f"resolved to {save_hits[0]}" if save_hits
                else "user must confirm the save directory/account"))
    else:
        _check(checks, "save_directory", "pass",
               "not declared in driver; a scenario checkpoint may use the default registry")

    menu_skip = driver.get("menu_skip") or []
    _check(
        checks, "menu_skip", "pass" if menu_skip else "manual",
        (f"{len(menu_skip)} recorded step(s)" if menu_skip else
         "record title/save menu to the fixed task start"),
        None if menu_skip else
        f"python scripts/calibration/record_pad_to_yaml.py --game {game}",
        blocks_config=not bool(menu_skip),
    )

    bench: dict | None = None
    if not bench_path.is_file():
        _check(checks, "bench_spec", "auto",
               f"missing {bench_path.relative_to(repo_root)}; scaffold it")
    else:
        try:
            bench = _read_yaml(bench_path)
            _check(checks, "bench_spec", "pass",
                   str(bench_path.relative_to(repo_root)))
        except Exception as exc:  # noqa: BLE001
            _check(checks, "bench_spec", "error",
                   f"cannot parse benchmark YAML: {type(exc).__name__}: {exc}")

    golden = _golden_path(repo_root, game, driver, bench)
    cleanup_policy = None
    cleanup_error = None
    try:
        from coding4game_gui.benchmark.windows.startup import validate_no_save_cleanup
        cleanup_policy = validate_no_save_cleanup((bench or {}).get("startup") or {}, save)
    except ValueError as exc:
        cleanup_error = str(exc)
    if golden is None:
        _check(checks, "golden_checkpoint", "pass",
               "not required; no save restore is configured")
    else:
        golden_ok = golden.is_file()
        _check(
            checks, "golden_checkpoint", "pass" if golden_ok else "manual",
            (f"found {golden}" if golden_ok else
             f"prepare the exact task start and create {golden}"),
            None if golden_ok else
            f"python scripts/calibration/checkpoint.py --game {game} golden",
            blocks_config=not golden_ok,
        )

    if cleanup_error:
        _check(checks, "state_preparation_contract", "error", cleanup_error)
    elif golden is not None:
        restore_target = None
        restore_error = None
        if save:
            matches = _resolved_matches(repo_root, save.get("restore_to"))
            restore_target = matches[0] if matches else None
            if restore_target is None:
                restore_error = ("set the verified save_state.restore_to in games.local.json"
                                 if save.get("restore_to") is None else
                                 "configured save_state.restore_to does not exist")
        else:
            try:
                from coding4game_gui.benchmark.windows.save_paths import default_save_path
                restore_target = default_save_path(game)
            except Exception as exc:  # noqa: BLE001
                restore_error = str(exc)
        _check(
            checks, "state_preparation_contract",
            "error" if restore_error else "pass",
            (restore_error or
             f"golden checkpoint restores to {restore_target}"),
        )
    else:
        try:
            from coding4game_gui.benchmark.windows.game_settings import SAVE_CLEARERS
            has_cleanup = game.lower() in SAVE_CLEARERS
        except Exception:  # noqa: BLE001 - report conservatively
            has_cleanup = False
        process_only = cleanup_policy == "process_kill"
        _check(
            checks, "state_preparation_contract",
            "pass" if has_cleanup or process_only else "manual",
            ("declared process-kill-only start; live repeated-start proof remains required"
             if process_only else
             "registered deterministic no-save cleanup exists" if has_cleanup
             else "prove process kill alone is deterministic or register a no-save cleanup"),
            blocks_config=not (has_cleanup or process_only),
        )

    if bench is not None:
        missing_bench = sorted(WINDOWS_BENCH_KEYS - set(bench))
        unknown_bench = sorted(set(bench) - WINDOWS_BENCH_KEYS)
        empty_bench = sorted(
            key for key in WINDOWS_BENCH_KEYS
            if key in bench and bench[key] is None)
        contract_errors = []
        if missing_bench:
            contract_errors.append(f"missing: {missing_bench}")
        if unknown_bench:
            contract_errors.append(f"unsupported: {unknown_bench}")
        if empty_bench:
            contract_errors.append(f"null: {empty_bench}")
        if bench.get("engine") != "windows":
            contract_errors.append("engine must be 'windows'")
        _check(checks, "bench_contract",
               "error" if contract_errors else "pass",
               ("; ".join(contract_errors) if contract_errors
                else "exact Windows benchmark top-level schema is present"))

        declared_driver = str(bench.get("driver_game") or "")
        _check(
            checks, "driver_scenario_link",
            "pass" if declared_driver == game else "error",
            (f"scenario uses driver {declared_driver!r}"
             if declared_driver == game else
             f"scenario declares driver {declared_driver!r}, audited driver is {game!r}"),
        )

        driver_actions = {str(k).upper()
                          for k in (driver.get("actions") or {})}
        _check(checks, "action_mapping",
               "pass" if driver_actions else "error",
               (f"{len(driver_actions)} driver action token(s) define the "
                "complete benchmark action space" if driver_actions else
                "driver YAML has no actions"))

        scorer = bench.get("scorer") or {}
        driver_scorer = driver.get("scorer") or {}
        kind = str(scorer.get("kind") or driver_scorer.get("kind") or "").lower()
        implementation = str(
            scorer.get("implementation") or
            driver_scorer.get("implementation") or "").lower()
        try:
            from coding4game_gui.benchmark.windows.scorers.factory import validate_scorer_selection
            validate_scorer_selection(
                kind or None, implementation or None,
                context=f"Windows benchmark {scenario!r}")
            scorer_error = None
        except Exception as exc:  # noqa: BLE001
            scorer_error = str(exc)
        _check(
            checks, "scorer_selection",
            "error" if scorer_error else "pass",
            (scorer_error or
             f"resolved scorer kind={kind!r}, implementation={implementation!r}"),
        )

        size = scorer.get("calibrated_client_size")
        _check(checks, "client_size",
               "pass" if isinstance(size, list) and len(size) == 2 else "manual",
               (f"calibrated at {size}" if size else
                "capture the live client size; do not guess it"),
               blocks_config=not (isinstance(size, list) and len(size) == 2))
        terminal = scorer.get("terminal_phases") or []
        phases = scorer.get("phases") or {}
        terminal_ok = bool(terminal) and all(name in phases for name in terminal)
        detection_disabled = (bench.get("lifecycle") or {}).get("terminal_detection") is False
        _check(checks, "terminal_phases",
               "pass" if detection_disabled or terminal_ok else "manual",
               ("no intermediate terminal detector is configured; verify the episode limit separately"
                if detection_disabled else f"terminal phases: {terminal}" if terminal_ok else
                "user must produce and label success/failure screens"))

        startup = bench.get("startup") or {}
        replay = str(startup.get("menu_skip_replay", "closed_loop")).lower()
        _check(
            checks, "menu_skip_replay",
            "pass" if replay in {"closed_loop", "open_loop"} else "error",
            (("production replay mode: open_loop; timing-sensitive and must "
              "be verified through the same production path")
             if replay == "open_loop" else
             f"production replay mode: {replay}"
             if replay in {"closed_loop", "open_loop"} else
             "startup.menu_skip_replay must be closed_loop or open_loop"),
        )
        runtime = bench.get("runtime") or {}
        try:
            from coding4game_gui.benchmark.windows.timing import resolve_tics_per_second
            tps = resolve_tics_per_second(runtime)
            timing_error = (None if "tics_per_second" in runtime else
                            "runtime.tics_per_second must be explicit for a new scenario")
        except Exception as exc:  # noqa: BLE001
            tps = None
            timing_error = str(exc)
        _check(
            checks, "tic_rate", "error" if timing_error else "pass",
            timing_error or f"runtime.tics_per_second={tps}",
        )
        start_check = startup.get("start_check")
        _check(checks, "start_check", "manual",
               ("assertion declared; live repeated-start evidence is still required"
                if start_check else
                "choose and verify a visible fact unique to the fresh task start"),
               blocks_config=not bool(start_check))
        state_step = "golden restore -> " if golden is not None else ""
        _check(checks, "cold_start_lifecycle", "manual",
               f"episodes use kill -> {state_step}direct launch -> menu_skip "
               "-> start_check -> close; prove this with repeated live starts")

    counts = Counter(item["status"] for item in checks)
    summary = {status: counts.get(status, 0) for status in STATUSES}
    config_ready = not any(
        item["status"] in {"auto", "error"} or item.get("blocks_config")
        for item in checks)
    return {"game": game, "scenario": scenario,
            "checks": checks, "summary": summary,
            "readiness_scope": "static_configuration",
            "config_ready": config_ready,
            # Backward-compatible alias. It no longer claims live evidence.
            "ready": config_ready}


def _print_human(report: dict) -> None:
    print(f"Windows benchmark preflight: {report['scenario']} "
          f"(driver {report['game']})")
    for item in report["checks"]:
        suffix = " [required for CONFIG READY]" if item.get("blocks_config") else ""
        print(f"[{item['status'].upper():6}] {item['id']}: "
              f"{item['detail']}{suffix}")
        if item.get("command"):
            print(f"         next: {item['command']}")
    summary = report["summary"]
    print("summary: " + " ".join(f"{k}={summary[k]}" for k in STATUSES))
    print("CONFIG READY" if report["ready"] else "CONFIG NOT READY")
    print("scope: static configuration only; live acceptance gates are separate")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", required=True, help="driver YAML slug")
    ap.add_argument("--scenario", help="benchmark scenario slug; default: game")
    ap.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    report = audit_game(args.repo_root, args.game, args.scenario)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_human(report)
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
