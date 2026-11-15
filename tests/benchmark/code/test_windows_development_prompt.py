import pytest


def test_scenario_action_cap_is_exposed_in_prompt_and_tool_schema():
    from coding4game_gui.benchmark.code.prompts import render_development_prompt
    from coding4game_gui.benchmark.code.tool_bridge import tools_for_phase
    spec = load_game_spec('aimlab', windows_input='gamepad-kbm')
    spec['runtime']['max_action_tics'] = 7
    prompt = render_development_prompt(
        spec, tic_budget=20, token_limit=1000, wall_seconds=60)
    assert 'integer tics from 1 to 7' in prompt
    assert 'Actions over 7 tics are rejected before any input' in prompt
    step = next(tool for tool in tools_for_phase(
        'final_test', max_action_tics=7) if tool['name'] == 'game.step')
    assert step['inputSchema']['properties']['tics']['maximum'] == 7
    assert all(shape['properties']['tics']['maximum'] == 7 for shape in
               step['inputSchema']['properties']['actions']['items']['oneOf'])
import ast
import re
from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.code.prompts import render_development_prompt
from coding4game_gui.benchmark.windows.terminal import validate_terminal_detection
from coding4game_gui.benchmark.code.tool_bridge import tools_for_phase
from coding4game_gui.runtime_client.client import gui_action_plan


def test_captured_pointer_instructions_match_existing_raw_controls():
    from coding4game_gui.benchmark.windows.raw_actions import resolve_raw_action
    prompt=render_development_prompt(load_game_spec('aimlab',windows_input='gamepad-kbm'))
    assert 'controls=["RMOVE[2,-1]"], tics=1' in prompt
    assert 'controls=["CLICK_CURRENT"], tics=1' in prompt
    assert 'sole control' in prompt
    step=next(item for item in tools_for_phase('development') if item['name']=='game.step')
    assert 'In gamepad mode only, use controls' not in step['description']
    move=resolve_raw_action(['RMOVE[2,-1]'],{},input_mode='gamepad-kbm')
    click=resolve_raw_action(['CLICK_CURRENT'],{},input_mode='gamepad-kbm')
    assert move.relative_moves==((2,-1),) and click.click_current is True
    with pytest.raises(ValueError,match='only control'):
        resolve_raw_action(['RMOVE[2,-1]','CLICK_CURRENT'],{},input_mode='gamepad-kbm')


def test_gui_python_examples_match_action_validator():
    prompt = render_development_prompt(load_game_spec('balatro', windows_input='gamepad-kbm'))
    batches = []
    for example in re.findall(r'```python\n(.*?)```', prompt, re.S):
        for node in ast.walk(ast.parse(example)):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == 'actions':
                        actions = ast.literal_eval(keyword.value)
                        assert gui_action_plan(actions)
                        batches.append(actions)
    assert len(batches) == 3
    assert any(action['type'] == 'drag' for batch in batches for action in batch)


def test_method_neutral_exploration_and_state_integrity():
    prompt = render_development_prompt({})
    assert 'No particular' in prompt
    assert 'perception method, algorithm or toolchain is preferred' in prompt
    assert 'Permission is not a guarantee of technical access' in prompt
    assert 'Obtain progress through normal gameplay inputs' in prompt
    assert 'Do not inject code' in prompt




def test_wsl_prompt_reports_verified_linux_environment():
    prompt = render_development_prompt(
        load_game_spec('brotato', windows_input='gamepad-kbm'),
        agent_name='claude', runtime_backend='wsl2',
    )
    normalized = ' '.join(prompt.split())
    for text in (
        'run-specific unprivileged Linux user `c4gprobe`',
        'no sudo or administrator access',
        'Python 3.13.5', 'OpenCV/cv2 4.10.0', 'NumPy 2.2.4', 'Pillow 11.1.0', 'requests 2.32.3',
        'Node.js 20', 'npm 9', 'Git 2.47', 'curl 8',
        'does not preinstall EasyOCR or PyTorch/torch',
        'CLI-bundled search and shell helpers vary with the selected baseline',
        'Public downloads are permitted by policy',
        'actual package routes and availability must be verified',
        'Direct access to the Windows desktop is unavailable unless an explicit host bridge',
        'Use the supplied game bridge for gameplay',
        'dependencies` object containing `environment`',
        '`requirements`, a list of exact `name==version` requirements',
        '`indexes`, a list of public HTTPS package-index URLs',
        '`assets`, a list of objects with `sha256`, asset-relative `path`',
        'an absolute `source_path` inside your development workspace',
        'host resolves and downloads this declaration while development networking is available',
        'creates a fresh environment from the same lock before cutting public network access',
        'No particular perception method, algorithm or toolchain is preferred',
        'submission.finish(id)',
    ):
        assert text in normalized
    for false_claim in (
        'Linux uid 1000',
        'pinned Python runtime currently provides OpenCV',
        'EasyOCR 1.7.2 with CPU-only PyTorch 2.7.1',
        'development shell also includes `rg`',
        'offline English EasyOCR packages and model weights',
    ):
        assert false_claim not in normalized
    assert 'In Bash, for example:' in prompt
    assert '```bash' in prompt
    assert '"$C4G_PYTHON" ./my_program/debug.py' in prompt
    assert '$env:C4G_PYTHON' not in prompt
    assert '```powershell' not in prompt




@pytest.mark.parametrize('mode', ['gamepad', 'gamepad-kbm'])
def test_prompt_is_current_protocol_and_excludes_private_spec(mode):
    spec = load_game_spec('brotato', windows_input=mode)
    spec['startup']['secret_canary'] = 'PRIVATE_STARTUP_VALUE'
    spec['scorer']['secret_canary'] = 'PRIVATE_SCORER_VALUE'
    spec['runtime']['api_key'] = 'PRIVATE_RUNTIME_VALUE'
    prompt = render_development_prompt(spec, tic_budget=1200, wall_seconds=300,
                                       token_limit=30000, max_tics=100)
    normalized = ' '.join(prompt.split())
    assert all(value not in prompt for value in
               ['PRIVATE_STARTUP_VALUE','PRIVATE_SCORER_VALUE','PRIVATE_RUNTIME_VALUE'])
    assert all(tool['name'] in prompt for tool in tools_for_phase('development'))
    assert 'capped at 100 tics' in prompt and '30000 tokens' in prompt
    assert 'install compatible user-space' in normalized
    assert 'game.step resumes the game' in prompt
    assert 'must not use any LLM' in prompt
    assert 'submission.finish(id)' in prompt
    assert 'model-token budget, gameplay-tic budget, and wall-clock budget are separate hard limits' in normalized
    assert 'not one interchangeable pool and not targets to consume' in normalized
    assert 'obtain a bounded `submission.run` whose `program_returncode` is zero' in normalized
    assert 'Remaining gameplay tics never make it acceptable to consume the model-token budget' in normalized
    assert 'A selected executable program is the mandatory outcome' in prompt
    assert 'Research notes, screenshots, calibration data, and direct play are not the deliverable' in normalized
    assert 'A weak evaluated and selected program is better than no selected program' in normalized
    assert 'Always reserve enough model requests, game tics, and time' in normalized
    assert 'Before any budget is exhausted, finish the strongest saved runnable and evaluated candidate' in normalized
    assert 'may select a saved candidate at any time' in normalized
    assert 'Unused game tics do not block final selection' in normalized
    assert 'confirmed usage first reaches 70%' in prompt
    assert 'approximately 30% for finalization' in prompt
    assert 'does not extend either budget' in prompt
    assert 'policy(frame' not in prompt
    assert 'fresh policy sandbox is created for every evaluation' not in prompt
    assert '$game_title' not in prompt
    if mode == 'gamepad-kbm':
        assert 'Keyboard/mouse guide' in prompt
        assert 'Controller guide' not in prompt
    else:
        assert 'Controller guide' in prompt
        assert 'Keyboard/mouse guide' not in prompt


def test_zero_and_boolean_budgets_reject():
    for value in (0, -1, True):
        with pytest.raises(ValueError):
            render_development_prompt({}, token_limit=value)


def test_rollout_override_can_only_lower_registered_limit():
    spec = {'runtime': {'max_tics': 7}}
    assert 'capped at 7 tics' in render_development_prompt(spec, max_tics=100)
    assert 'capped at 5 tics' in render_development_prompt(spec, max_tics=5)


@pytest.mark.parametrize('mode', ['gamepad', 'gamepad-kbm'])
def test_enabled_terminal_prompt_describes_shared_host_behavior(mode):
    spec = load_game_spec('balatro', windows_input=mode)
    prompt = render_development_prompt(spec)
    contract = ' '.join(prompt.split())
    assert 'registered game-specific terminal conditions' in contract
    assert 'done=true is authoritative' in contract
    assert 'no additional game.stop call is required' in contract
    assert 'Stillness' in contract and 'not generic' in contract
    assert 'do not automatically stop gameplay' not in contract
    assert 'There is no automatic death' not in contract
    expected_overshoot = ('at most two tics without invalidating' if mode == 'gamepad-kbm'
                          else 'up to two tics without invalidating')
    assert expected_overshoot in contract
    assert 'one tic without invalidating' not in contract


def test_disabled_terminal_detection_prompt_requires_explicit_stop():
    spec = {
        'lifecycle': {'terminal_detection': False},
        'runtime': {'windows_input': 'gamepad-kbm'},
    }
    assert validate_terminal_detection(spec) is False
    contract = ' '.join(render_development_prompt(spec).split())
    assert 'Registered automatic terminal detection is disabled' in contract
    assert 'use game.stop when the episode should end' in contract
    assert 'done=true is authoritative' not in contract


def test_prompt_distinguishes_usage_threshold_from_strict_bound():
    usage = render_development_prompt({}, token_limit=100)
    assert "post-response usage accounting" in usage
    assert "final request may exceed" in usage
    assert "Missing usage is marked unknown" in usage
    strict = render_development_prompt({}, token_limit=100, token_accounting="strict")
    assert "certified input count" in strict
    assert "post-response usage accounting" not in strict
    with pytest.raises(ValueError, match="token_accounting"):
        render_development_prompt({}, token_accounting="silent-fallback")
