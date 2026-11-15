import re
from pathlib import Path
from coding4game_gui.benchmark.code.prompts import render_development_prompt
from coding4game_gui.benchmark.direct.player import build_direct_system

import pytest

from coding4game_gui.benchmark.spec import load_game_spec


REPO = Path(__file__).resolve().parents[2]
SCENARIO_DIR = REPO / "coding4game_gui" / "benchmark" / "scenarios"
GUIDE_HEADING = (
    "Controller guide (game-default controller preset; context-sensitive):"
)
DEFAULT_PRESET_MARKER = "Game-default preset:"
TABLE_HEADER = "| Input token | Gameplay meaning | Menu / contextual meaning |"
INPUT_ORDER = [
    "neutral",
    "LS[x,y]",
    "RS[x,y]",
    "LT[v]",
    "RT[v]",
    "a",
    "b",
    "x",
    "y",
    "lb",
    "rb",
    "ls",
    "rs",
    "dpad_up",
    "dpad_down",
    "dpad_left",
    "dpad_right",
    "start",
    "back",
    "guide",
]
SCENARIOS = {
    "balatro",
    "black_myth",
    "brotato",
    "forza_horizon_5",
    "kof98_terminal",
    "slay_the_spire",
}


@pytest.mark.parametrize('game', ['black_myth', 'forza_horizon_5', 'slay_the_spire'])
def test_documented_keyboard_guides_are_mode_isolated(game):
    from coding4game_gui.benchmark.windows.raw_actions import WINDOWS_KBM_KEYS
    spec = load_game_spec(game, windows_input='gamepad-kbm')
    guide = spec['gameplay']
    assert 'Keyboard/mouse guide' in guide
    assert 'Controller guide' not in guide
    assert 'LS[' not in guide and 'RT[' not in guide
    for key in re.findall(r'KEY\[([^\]]+)\]', guide):
        assert key in WINDOWS_KBM_KEYS
    prompt = render_development_prompt(spec)
    assert '### Complete action list' in prompt
    assert 'C4G_WINDOWS_INPUT' not in prompt


def test_forza_kbm_uses_calibrated_wasd_and_requires_motion_evidence():
    text = load_game_spec('forza_horizon_5', windows_input='gamepad-kbm')['gameplay']
    normalized = ' '.join(text.split())
    assert 'uses the game-provided WASD-driving preset' in normalized
    assert '`KEY[W]`, `KEY[S]` | Throttle and brake/reverse' in normalized
    assert 'Camera controls only; never use them as throttle or brake' in normalized
    assert 'A menu or HUD transition alone is not evidence of motion' in normalized
    assert 'do not switch to an arrow-driving assumption' in normalized
    assert 'Do not inspect or change bindings' in normalized


def test_black_myth_kbm_documents_complete_default_combat_controls():
    spec = load_game_spec('black_myth', windows_input='gamepad-kbm')
    text = spec['gameplay']
    normalized = ' '.join(text.split())
    for token in (
        '`KEY[W]`', '`KEY[A]`', '`KEY[S]`', '`KEY[D]`', '`KEY[SHIFT]`',
        '`KEY[SPACE]`', '`KEY[CTRL]`', '`CLICK[x,y]`', '`RCLICK[x,y]`',
        '`MCLICK[x,y]`', '`KEY[R]`', '`KEY[Q]`', '`KEY[V]`', '`KEY[F]`',
        '`KEY[1]`', '`KEY[2]`', '`KEY[3]`', '`KEY[4]`', '`KEY[Z]`',
        '`KEY[X]`', '`KEY[C]`', '`KEY[E]`', '`KEY[P]`', '`KEY[ESC]`',
    ):
        assert token in normalized
    assert 'toggles lock-on' in normalized
    assert 'cannot hold to charge a heavy attack' in normalized
    assert 'mouse-relative camera input' in normalized
    code_prompt = render_development_prompt(spec)
    direct_prompt = build_direct_system(spec, 5)
    assert '`click (middle) (x,y)`' in code_prompt
    assert '`MCLICK[x,y]`' not in code_prompt
    assert '`MCLICK[x,y]`' in direct_prompt


def test_kof98_requires_its_registered_controller_protocol():
    with pytest.raises(ValueError, match='no verified gameplay guide'):
        load_game_spec('kof98_terminal', windows_input='gamepad-kbm')


def _guide_tokens(text: str) -> list[str]:
    assert text.count(GUIDE_HEADING) == 1
    assert text.count(DEFAULT_PRESET_MARKER) == 1
    assert "no custom remapping is assumed" in " ".join(text.split())
    guide = text.split(GUIDE_HEADING, 1)[1]
    assert TABLE_HEADER in guide
    return re.findall(r"^\| `([^`]+)` \|", guide, flags=re.MULTILINE)


def test_every_windows_scenario_has_the_aligned_complete_controller_guide():
    paths = sorted(SCENARIO_DIR.glob("*.yaml"))
    assert SCENARIOS <= {path.stem for path in paths}

    for path in paths:
        if path.stem not in SCENARIOS:
            continue
        spec = load_game_spec(path.stem)
        assert _guide_tokens(spec["gameplay"]) == INPUT_ORDER, path.name


def test_development_and_direct_prompts_receive_each_controller_guide():
    for scenario in sorted(SCENARIOS):
        spec = load_game_spec(scenario)
        for arm in ("codex", "direct"):
            prompt = (render_development_prompt(spec, tic_budget=100) if arm == "codex"
                      else build_direct_system(spec, 5))
            assert prompt.count(GUIDE_HEADING) == 1, (scenario, arm)
            assert prompt.count(DEFAULT_PRESET_MARKER) == 1, (scenario, arm)
            assert "no custom remapping is assumed" in " ".join(prompt.split()), (
                scenario, arm)
            assert TABLE_HEADER in prompt, (scenario, arm)
