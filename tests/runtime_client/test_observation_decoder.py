import base64
import io

import pytest
from PIL import Image

from coding4game_gui.runtime_client.client import BridgeError, observation_bytes


def test_python_observation_decodes_pixels_without_mcp_wrapper():
    stream = io.BytesIO()
    Image.new('RGB', (3, 2), (17, 42, 93)).save(stream, format='PNG')
    result = {'done': False, 'observation': {
        'mimeType': 'image/png',
        'data': base64.b64encode(stream.getvalue()).decode('ascii'),
    }}
    # The failed program searched this absent MCP field and silently lost vision.
    assert 'content' not in result
    image = Image.open(io.BytesIO(observation_bytes(result))).convert('RGB')
    assert image.size == (3, 2)
    assert image.getpixel((1, 1)) == (17, 42, 93)
    result['done'] = True
    assert observation_bytes(result) == stream.getvalue()


@pytest.mark.parametrize('result', [
    None, [], {}, {'content': [{'type': 'image', 'data': 'eA=='}]},
    {'observation': {'mimeType': 'image/png', 'data': '!invalid!'}},
    {'observation': {'mimeType': 'image/png', 'data': ''}},
    {'observation': {'data': 'eA=='}},
])
def test_bad_observation_fails_explicitly(result):
    with pytest.raises(BridgeError):
        observation_bytes(result)


def test_ended_result_without_image():
    assert observation_bytes({'done': True}) is None


def test_rendered_python_example_runs_against_client_transport(monkeypatch):
    import json
    import re
    import sys
    from coding4game_gui.runtime_client import client as sdk
    from coding4game_gui.benchmark.code.prompts import render_development_prompt

    stream = io.BytesIO()
    Image.new('RGB', (3, 2), (17, 42, 93)).save(stream, format='PNG')
    result = {'done': False, 'observation': {
        'mimeType': 'image/png', 'data': base64.b64encode(stream.getvalue()).decode()}}
    class Transport:
        def open(self, request, timeout):
            assert json.loads(request.data)['tool'] == 'game.observe'
            return io.BytesIO(json.dumps({'ok': True, 'result': result}).encode())
    monkeypatch.setenv('C4G_BRIDGE_URL', 'http://localhost/unused')
    monkeypatch.setenv('C4G_BRIDGE_TOKEN', 'test-only')
    monkeypatch.setattr(sdk.urllib.request, 'build_opener', lambda *args: Transport())
    monkeypatch.setitem(sys.modules, 'c4g_client', sdk)
    prompt = render_development_prompt({})
    example = next(code for code in re.findall(r'```python\n(.*?)```', prompt, re.S)
                   if 'observation_bytes' in code)
    namespace = {}
    exec(example, namespace)
    assert namespace['pixels'].getpixel((1, 1)) == (17, 42, 93)
    # The adapter wraps the same raw response; it does not change Client.call.
    wrapped = sdk._mcp_content(result)
    assert wrapped[1]['type'] == 'image'
    assert 'content' not in result
    result['observation']['data'] = '!invalid!'
    with pytest.raises(sdk.BridgeError):
        exec(example, {})
