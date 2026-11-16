"""Read-only installation diagnostics: no games, save writes, WSL clones or models."""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import shutil
import sys
import sysconfig
import tomllib

from coding4game_gui.catalog import FORMAL_SCENARIOS, onboarding_status, registered_scenario_paths
from coding4game_gui.operator_config import operator_paths
from coding4game_gui.paths import REPO_ROOT, SCENARIO_DIR

def _host_packages(*, ocr=False) -> list[str]:
    from packaging.requirements import Requirement

    manifest = REPO_ROOT / "pyproject.toml"
    if manifest.is_file():
        with manifest.open("rb") as stream:
            project = tomllib.load(stream)["project"]
        dependencies = project["optional-dependencies"]["ocr"] if ocr else project["dependencies"]
        packages = (Requirement(item).name for item in dependencies)
    else:
        dependencies = metadata.requires("coding4game-gui")
        if not dependencies:
            raise ValueError("The installed project has no dependency metadata")
        # These wheel markers use conjunctions. Evaluating their platform terms
        # can probe OS version by spawning a process on Windows.
        packages = []
        for item in map(Requirement, dependencies):
            clauses = str(item.marker).split(" and ") if item.marker else []
            extras = [clause for clause in clauses if clause.startswith("extra ")]
            selected = 'extra == "ocr"' in extras if ocr else not extras
            if selected:
                packages.append(item.name)
    return list(dict.fromkeys(packages))


def _missing_packages(packages) -> list[str]:
    missing = []
    for package in packages:
        try:
            metadata.version(package)
        except metadata.PackageNotFoundError:
            missing.append(package)
    return missing


def diagnose(repo: Path = REPO_ROOT, *, game=None, agent="claude", model=None,
             verify_baseline=False) -> dict:
    repo = Path(repo).resolve()
    checks = []

    def check(name, ok, detail, *, warning=False):
        checks.append({"id": name, "status": "pass" if ok else "warning" if warning else "blocked",
                       "detail": detail})

    check("windows", sys.platform == "win32", "Gameplay requires an unlocked interactive Windows desktop.")
    check("python", sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 11)
          and sysconfig.get_platform() == "win-amd64", "Documented host environment: CPython 3.11 x64.")
    try:
        missing = _missing_packages(_host_packages())
    except (OSError, ValueError, ImportError, KeyError) as error:
        check("dependencies", False,
              f"Cannot inspect project dependencies ({type(error).__name__}). Run host setup.")
    else:
        check("dependencies", not missing, "Missing distributions: " + ", ".join(missing) if missing
              else "Host distributions installed; GPU, native drivers and imports still require runtime checks.")
    check("wsl_command", shutil.which("wsl.exe") is not None,
          "WSL2 must be installed; run wsl --version and wsl --list --verbose separately.")
    try:
        paths = operator_paths(repo)
    except ValueError as error:
        check("local_settings", False, str(error))
        paths = None
    if paths is not None:
        profile_file = paths["profile"]
        check("profile_file", profile_file.is_file(), f"Runtime profile: {profile_file}")
        if profile_file.is_file():
            try:
                from coding4game_gui.wsl2.profile import WslProfile
                profile = WslProfile.load(profile_file)
                profile.agent(agent)
                check("profile_agent", True, f"Profile declares {agent}; no CLI was executed.")
                check("baseline_file", profile.baseline.is_file(), f"Baseline: {profile.baseline}")
                if verify_baseline and profile.baseline.is_file():
                    digest = hashlib.sha256()
                    with profile.baseline.open("rb") as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(block)
                    check("baseline_hash", digest.hexdigest() == profile.baseline_sha256,
                          "Baseline SHA-256 checked against the profile.")
            except (OSError, ValueError, TypeError, KeyError):
                check("profile", False, "Profile validation failed; generate a valid WSL2 profile with the selected agent.")
        provider = paths["credentials"]
        check("provider_file", provider.is_file(), f"Host provider configuration: {provider}")
        if model and provider.is_file():
            try:
                from coding4game_gui.operator_config import load_provider_route
                route = load_provider_route(provider, model, allow_non_openai=True)
                placeholder = ".invalid" in route.api_base or "REPLACE_WITH" in route.provider_model
                check("provider_route", not placeholder,
                      "Replace the example provider endpoint and model ID." if placeholder else
                      "Selected route and key reference resolve; no provider request was made.")
            except (ImportError, ValueError):
                check("provider_route", False, "Selected route is invalid or its host key environment variable is unset.")
        check("output_location", True, f"New runs: {paths['output_root']} (not created by this check).")
    game_report = None
    if game:
        try:
            import yaml
            scenario_path = repo / "coding4game_gui" / "benchmark" / "scenarios" / f"{game}.yaml"
            scenario = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
            driver = scenario["driver_game"]
            from scripts.calibration.add_new_game_preflight import audit_game
            game_report = audit_game(repo, driver, game)
            check("game_configuration", game_report["config_ready"],
                  "Static game configuration only; inspect game_checks for missing resources.")
            check("game_acceptance_scope", game in FORMAL_SCENARIOS, onboarding_status(game), warning=True)
            from coding4game_gui.benchmark.windows.local_paths import read_driver_data
            driver_data = read_driver_data(repo / "games" / f"{driver}.yaml")
            scorer = scenario.get("scorer") or {}
            startup = scenario.get("startup") or {}
            if (scorer.get("kind") == "ocr" or "ocr" in scorer or
                    "menu_skip_ocr" in startup or "menu_skip_ocr_rect" in startup or
                    (driver_data.get("scorer") or {}).get("kind") == "ocr"):
                missing_ocr = _missing_packages(_host_packages(ocr=True))
                check("game_ocr_dependencies", not missing_ocr,
                      "Missing OCR distributions: " + ", ".join(missing_ocr) +
                      ". Run ./scripts/start.ps1 -Setup -Ocr." if missing_ocr else
                      "OCR distributions installed; verify the GPU and OCR runtime in game calibration.")
            if driver_data.get("ime_context_mode") == "disabled":
                native = repo / "coding4game_gui/benchmark/windows/drivers/windows_xinput/native_ime"
                try:
                    manifest = json.loads((native / "manifest.json").read_text(encoding="utf-8"))
                    ok = all(hashlib.sha256((native / name).read_bytes()).hexdigest() == manifest["sha256"][name]
                             for name in ("OwnedImeController.exe", "owned_ime_context32.dll"))
                except (OSError, ValueError, KeyError, TypeError):
                    ok = False
                check("game_ime_components", ok,
                      "BattleBlock requires the verified native IME helper. See docs/SUPPORTED_GAMES.md.")
            toolkit = scenario.get("lifecycle", {}).get("save_verification", {}).get("toolkit_root")
            if toolkit:
                from coding4game_gui.benchmark.windows.save_verification import (
                    PALWORLD_INSPECTOR_SCRIPT,
                    PALWORLD_TOOLKIT_FILES,
                )

                toolkit_path = repo / toolkit
                toolkit_ready = all((toolkit_path / name).is_file()
                                    for name in PALWORLD_TOOLKIT_FILES)
                check("game_save_toolkit", toolkit_ready and shutil.which("node") is not None,
                      "Palworld requires Node.js and the separately acquired toolkit at " + str(toolkit_path))
                check("game_save_inspector", PALWORLD_INSPECTOR_SCRIPT.is_file(),
                      "Palworld requires the bundled Node.js save inspector.")
        except (OSError, ValueError, TypeError, KeyError, ImportError) as error:
            check("game_configuration", False, f"Cannot inspect game configuration ({type(error).__name__}). Install the source checkout dependencies.")
    return {"scope": "static_configuration", "static_checks_pass": all(c["status"] != "blocked" for c in checks),
            "real_game_acceptance_performed": False, "checks": checks,
            "game": game, "game_checks": game_report,
            "next_steps": "docs/QUICKSTART.md; docs/SUPPORTED_GAMES.md"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scenarios = [path.stem for path in registered_scenario_paths(SCENARIO_DIR)]
    parser.add_argument("--game", choices=scenarios)
    parser.add_argument("--agent", choices=["claude", "codex"], default="claude")
    parser.add_argument("--model", help="Optionally validate a configured route without making a request")
    parser.add_argument("--verify-baseline", action="store_true", help="Also hash the potentially large baseline TAR")
    parser.add_argument("--list-games", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.list_games:
        entries = [{"scenario": slug, "status": onboarding_status(slug)}
                   for slug in scenarios]
        if args.json:
            print(json.dumps(entries, indent=2))
        else:
            for entry in entries:
                print(f"{entry['scenario']}: {entry['status']}")
        return 0
    report = diagnose(game=args.game, agent=args.agent, model=args.model, verify_baseline=args.verify_baseline)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for item in report["checks"]:
            print(f"[{item['status'].upper()}] {item['id']}: {item['detail']}")
        if report["game_checks"]:
            for item in report["game_checks"]["checks"]:
                if item["status"] != "pass":
                    print(f"[{item['status'].upper()}] game/{item['id']}: {item['detail']}")
                    if item.get("command"):
                        print(f"  {item['command']}")
        print("Static checks only. No game, WSL clone, save restoration or model call was started.")
    return 0 if report["static_checks_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
