"""Public onboarding scope; registration alone is not game acceptance."""

from pathlib import Path

from coding4game_gui.paths import SCENARIO_DIR

FORMAL_SCENARIOS = (
    "aimlab", "balatro", "battleblock", "black_myth", "brotato",
    "forza_horizon_5", "kingdom_rush_alliance_heart_of_the_forest",
    "kof98_terminal", "nba_2k25", "overcooked_2_2_1",
    "palworld_capture_penking", "riders_republic", "sekiro_gyoubu",
    "slay_the_spire", "trackmania",
)


def registered_scenario_paths(directory: Path = SCENARIO_DIR) -> list[Path]:
    return [path for slug in sorted(FORMAL_SCENARIOS)
            if (path := directory / f"{slug}.yaml").is_file()]


def input_mode(scenario: str) -> str:
    return "gamepad" if scenario in {
        "forza_horizon_5", "kof98_terminal", "nba_2k25", "riders_republic",
        "sekiro_gyoubu",
    } else "gamepad-kbm"


def onboarding_status(scenario: str) -> str:
    if scenario in FORMAL_SCENARIOS:
        return "accepted_on_original_host_requires_local_verification"
    return "registered_outside_release_acceptance_scope"
