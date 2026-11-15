"""Exercise viewport screenshots and pointer dispatch together across MCP."""
import base64
import builtins
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import cv2
import numpy as np
import pytest

from coding4game_gui.runtime_client import client


def run_mcp(monkeypatch, actions, *, claude=True, viewport=None):
    calls = []
    frame = np.zeros((768, 1280, 3), dtype=np.uint8)
    frame[452:489, 280:495] = 255
    _, encoded = cv2.imencode('.png', frame)

    class Session:
        def dispatch(self, name, arguments, *, phase):
            calls.append((name, arguments))
            return {'observation': {'mimeType': 'image/png',
                'data': base64.b64encode(encoded).decode()}}

    requests = [dict(id=1, method='tools/list'), dict(id=2, method='tools/call',
        params={'name': 'game.step', 'arguments': {'actions': actions}})]
    stdout = io.StringIO()
    from coding4game_gui.benchmark.code.tool_bridge import ToolServer
    with ToolServer(Session(), observation_size=viewport or ((768, 461) if claude else (1280, 768))) as bridge:
        monkeypatch.setenv('C4G_BRIDGE_URL', bridge.url)
        monkeypatch.setenv('C4G_BRIDGE_TOKEN', bridge.token)
        client.mcp_main(stdin=io.StringIO('\n'.join(map(json.dumps, requests))),
                        stdout=stdout, claude_viewport=claude, viewport=viewport)
    return calls, [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_text_only_viewport_content_does_not_import_image_dependencies(monkeypatch):
    real_import = builtins.__import__

    def reject_image_dependencies(name, *args, **kwargs):
        if name in {'cv2', 'numpy'}:
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', reject_image_dependencies)
    content = client._mcp_content({'tics_remaining': 42}, viewport=(768, 461))

    assert [block['type'] for block in content] == ['text', 'text']
    assert '768x461' in content[0]['text']
    assert json.loads(content[1]['text']) == {'tics_remaining': 42}


def test_claude_image_and_click_use_same_viewport(monkeypatch):
    actions = [dict(type='click', button='left', x=232, y=281, tics=5)]
    original = copy.deepcopy(actions)
    calls, replies = run_mcp(monkeypatch, actions)
    mapped = calls[0][1]['actions'][0]
    assert (mapped['x'], mapped['y'], mapped['tics']) == (387, 469, 5)
    assert actions == original
    image = next(c for c in replies[1]['result']['content'] if c['type'] == 'image')
    frame = cv2.imdecode(np.frombuffer(base64.b64decode(image['data']), np.uint8), 1)
    assert frame.shape[:2] == (461, 768)
    assert frame[281, 232].min() > 240
    tools = replies[0]['result']['tools']
    step = next(t for t in tools if t['name'] == 'game.step')
    shape = step['inputSchema']['properties']['actions']['items']['oneOf'][0]
    assert shape['properties']['x']['maximum'] == 767
    assert shape['properties']['y']['maximum'] == 460


@pytest.mark.parametrize('kind,size', [
    ('click', (768, 461)), ('double_click', (768, 461)), ('move', (768, 461)),
    ('scroll', (768, 461)), ('drag', (768, 461)), ('drag', (1000, 1000)),
])
def test_all_pointer_types_map_endpoints_without_changing_timing(kind, size):
    action = dict(type=kind, tics=7, x=size[0]-1, y=size[1]-1)
    if kind == 'click':
        action['button'] = 'right'
    if kind == 'scroll':
        action.update(scroll_x=2, scroll_y=-3)
    if kind == 'drag':
        action = dict(type=kind, tics=7, path=[dict(x=0, y=0), dict(x=size[0]-1, y=size[1]-1)])
    mapped = client._viewport_arguments('game.step', {'actions': [action]}, size)['actions'][0]
    point = mapped['path'][-1] if kind == 'drag' else mapped
    assert (point['x'], point['y']) == (1279, 767)
    assert mapped['tics'] == 7
    if kind == 'scroll':
        assert (mapped['scroll_x'], mapped['scroll_y']) == (2, -3)


def test_invalid_later_action_rejects_entire_batch_before_bridge(monkeypatch):
    calls, replies = run_mcp(monkeypatch, [dict(type='wait', tics=5),
        dict(type='move', x=768, y=10, tics=5)])
    assert not calls
    assert 'error' in replies[1]


def test_default_mcp_keeps_canonical_image_and_coordinates(monkeypatch):
    calls, replies = run_mcp(monkeypatch,
        [dict(type='click', button='left', x=388, y=470, tics=5)], claude=False)
    assert calls[0][1]['actions'][0]['x'] == 388
    image = next(c for c in replies[1]['result']['content'] if c['type'] == 'image')
    frame = cv2.imdecode(np.frombuffer(base64.b64decode(image['data']), np.uint8), 1)
    assert frame.shape[:2] == (768, 1280)
    assert client.GUI_ACTION_SCHEMA['oneOf'][0]['properties']['x']['maximum'] == 1279


@pytest.mark.parametrize('size', [(768, 461), (1000, 1000)])
def test_replay_images_also_use_viewport(size):
    _, encoded = cv2.imencode('.png', np.zeros((768, 1280, 3), np.uint8))
    observation = dict(mimeType='image/png', data=base64.b64encode(encoded).decode())
    content = client._mcp_content({'frames': [{'observation': observation}]},
                                  viewport=size)
    image = next(c for c in content if c['type'] == 'image')
    frame = cv2.imdecode(np.frombuffer(base64.b64decode(image['data']), np.uint8), 1)
    assert frame.shape[:2] == (size[1], size[0])


@pytest.mark.parametrize('phase,size', [
    ('development', (1280, 768)), ('development', (768, 461)),
    ('final_test', (768, 461)), ('final_test', (1000, 1000)),
])
def test_separate_python_program_uses_experiment_pixels(tmp_path, phase, size):
    from coding4game_gui.benchmark.code.tool_bridge import ToolServer

    _, encoded = cv2.imencode('.png', np.zeros((768, 1280, 3), np.uint8))
    observation = dict(mimeType='image/png', data=base64.b64encode(encoded).decode())
    calls = []

    class Session:
        def dispatch(self, tool, arguments, *, phase):
            calls.append((tool, arguments, phase))
            return {'observation': observation}

    (tmp_path / 'c4g_client.py').write_bytes(Path(client.__file__).read_bytes())
    program = tmp_path / 'main.py'
    x, y = {(768, 461): (232, 281), (1000, 1000): (303, 611)}.get(size, (388, 470))
    program.write_text(
        "import json, struct\n"
        "from c4g_client import Client, observation_bytes\n"
        "c = Client.from_env()\n"
        "raw = observation_bytes(c.call('game.observe'))\n"
        f"assert struct.unpack('>II', raw[16:24]) == {size!r}\n"
        "c.call('game.step', actions=[dict(type='click', button='left', "
        f"x={x}, y={y}, tics=5)])\n", encoding='utf-8')
    with ToolServer(Session(), phase=phase, observation_size=size) as bridge:
        env = dict(os.environ, C4G_BRIDGE_URL=bridge.url, C4G_BRIDGE_TOKEN=bridge.token)
        env.pop('C4G_EPISODE_ID', None)
        result = subprocess.run([sys.executable, str(program)], env=env,
                                capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    expected_x, expected_y = {(768, 461): (387, 469), (1000, 1000): (388, 469)}.get(size, (388, 470))
    assert calls[-1] == ('game.step', {'actions': [dict(
        type='click', button='left', x=expected_x, y=expected_y, tics=5)]}, phase)


def test_gemini_image_and_click_use_same_pixels(monkeypatch):
    calls, replies = run_mcp(monkeypatch,
        [dict(type='click', button='left', x=303, y=611, tics=5)],
        claude=False, viewport=(1000, 1000))
    action = calls[0][1]['actions'][0]
    assert (action['x'], action['y']) == (388, 469)
    image = next(c for c in replies[1]['result']['content'] if c['type'] == 'image')
    frame = cv2.imdecode(np.frombuffer(base64.b64decode(image['data']), np.uint8), 1)
    assert frame.shape[:2] == (1000, 1000)
    assert frame[611, 303].min() > 240
    step = next(t for t in replies[0]['result']['tools'] if t['name'] == 'game.step')
    shape = step['inputSchema']['properties']['actions']['items']['oneOf'][0]
    assert shape['properties']['x']['maximum'] == 999
    assert shape['properties']['y']['maximum'] == 999


def test_gemini_tool_schema_disambiguates_xy_from_model_yx_convention():
    tools = client._viewport_tools((1000, 1000))
    step = next(t for t in tools if t['name'] == 'game.step')
    shape = step['inputSchema']['properties']['actions']['items']['oneOf'][0]
    x = shape['properties']['x']
    y = shape['properties']['y']
    assert 'horizontal' in x['description'].lower()
    assert 'left' in x['description'].lower()
    assert 'vertical' in y['description'].lower()
    assert 'top' in y['description'].lower()
    assert '[y,x]' in step['description']
    assert 'named x/y fields' in step['description']


def test_gemini_observation_repeats_xy_order_next_to_image():
    _, encoded = cv2.imencode('.png', np.zeros((768, 1280, 3), np.uint8))
    observation = dict(mimeType='image/png', data=base64.b64encode(encoded).decode())
    content = client._mcp_content({'observation': observation}, viewport=(1000, 1000))
    notice = next(block['text'] for block in content if block['type'] == 'text')
    assert 'x is horizontal' in notice
    assert 'y is vertical' in notice
    assert '[y,x]' in notice


def test_gemini_rejects_1000_before_any_action(monkeypatch):
    calls, replies = run_mcp(monkeypatch,
        [dict(type='wait', tics=5), dict(type='move', x=1000, y=611, tics=5)],
        claude=False, viewport=(1000, 1000))
    assert not calls
    assert 'error' in replies[1]
