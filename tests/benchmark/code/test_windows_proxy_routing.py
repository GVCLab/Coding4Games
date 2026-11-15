"""Real local HTTP transport tests; no real model or paid provider is used."""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import json
import threading
import urllib.request

import pytest

from coding4game_gui.runtime_client.client import Client
from coding4game_gui.benchmark.code import model_gateway as gateway_module
from coding4game_gui.benchmark.code import run_codex as runner
from coding4game_gui.benchmark.code.tool_bridge import ToolServer


@contextmanager
def endpoint(*, redirect=None):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_CONNECT(self):
            calls.append((self.path, self.headers.get('Authorization'), None))
            self.send_response(407)
            self.end_headers()

        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            calls.append((self.path, self.headers.get('Authorization'), json.loads(body)))
            self.send_response(302 if redirect else 200)
            if redirect:
                self.send_header('Location', redirect)
            payload = json.dumps({'usage': {'total_tokens': 3}, 'output': []}).encode()
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', calls
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def test_game_client_never_uses_discovered_proxy(monkeypatch):
    class Session:
        def dispatch(self, tool, arguments, *, phase):
            return {'called': tool}
    with endpoint() as (proxy, calls), ToolServer(Session()) as server:
        monkeypatch.setattr(urllib.request, 'getproxies', lambda: {'http': proxy})
        monkeypatch.setattr(urllib.request, 'proxy_bypass', lambda host: False)
        monkeypatch.setattr(urllib.request, '_opener', None)
        assert Client(server.url, server.token).call('bench.status') == {'called': 'bench.status'}
        assert calls == []


@pytest.mark.parametrize('mode', ['system', 'direct', 'explicit'])
def test_host_gateway_route_and_credential_boundary(mode, monkeypatch, tmp_path):
    with endpoint() as (origin, origin_calls), endpoint() as (proxy, proxy_calls):
        monkeypatch.setattr(urllib.request, 'getproxies', lambda: {'http': proxy})
        # Explicit operator routing must not be silently bypassed by NO_PROXY.
        monkeypatch.setattr(urllib.request, 'proxy_bypass', lambda host: mode == 'explicit')
        with gateway_module.ModelGateway(origin + '/v1', 'host-only-secret', 'fixed', 100, 30,
                tmp_path / 'gateway.jsonl', input_token_bound=lambda value: 1,
                proxy_mode=mode, proxy_url=proxy if mode == 'explicit' else None) as gateway:
            connection = http.client.HTTPConnection('127.0.0.1', gateway.port, timeout=3)
            connection.request('POST', '/v1/responses', json.dumps({'input': 'test', 'max_output_tokens': 4}),
                               {'Authorization': 'Bearer ' + gateway.token, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            assert response.status == 200
            response.read(); connection.close()
            assert gateway.status()['tokens_used'] == 3
        actual, unused = (origin_calls, proxy_calls) if mode == 'direct' else (proxy_calls, origin_calls)
        assert len(actual) == 1 and unused == []
        assert actual[0][1] == 'Bearer host-only-secret'
        assert actual[0][2]['model'] == 'fixed'
        assert 'host-only-secret' not in (tmp_path / 'gateway.jsonl').read_text()


@pytest.mark.parametrize('mode,url', [('bad', None), ('direct', 'http://127.0.0.1:9'),
    ('system', 'http://127.0.0.1:9'), ('explicit', None), ('explicit', 'socks5://127.0.0.1:9'),
    ('explicit', 'http://user:password@127.0.0.1:9'), ('explicit', 'http://localhost/path')])
def test_proxy_configuration_fails_closed(mode, url):
    with pytest.raises(ValueError):
        gateway_module.validate_proxy(mode, url)


def test_gateway_redirects_remain_disabled():
    with endpoint() as (target, target_calls), endpoint(redirect=target) as (origin, calls):
        transport = gateway_module.make_transport('direct')
        response = transport('POST', origin, {'Authorization': 'Bearer fake'}, b'{}', 3)
        try:
            assert response.status == 302
        finally:
            response.close()
        assert len(calls) == 1 and target_calls == []


def test_explicit_https_proxy_uses_connect_target_without_changing_origin(monkeypatch):
    monkeypatch.setattr(urllib.request, 'proxy_bypass', lambda host: True)
    request = urllib.request.Request('https://api.example.invalid/v1/responses', data=b'{}')
    handler = gateway_module._ExplicitProxy({'https': 'http://127.0.0.1:9999'})
    assert handler.proxy_open(request, 'http://127.0.0.1:9999', 'https') is None
    assert request.host == '127.0.0.1:9999'
    assert request._tunnel_host == 'api.example.invalid'
    assert request.type == 'https'
    assert request.full_url == 'https://api.example.invalid/v1/responses'


def test_real_connect_does_not_send_provider_credential_before_tls_tunnel():
    with endpoint() as (proxy, calls):
        transport = gateway_module.make_transport('explicit', proxy)
        with pytest.raises(urllib.error.URLError, match='407'):
            transport('POST', 'https://api.example.invalid/v1/responses',
                      {'Authorization':'Bearer fake-provider-secret'}, b'{}', 3)
        assert calls == [('api.example.invalid:443', None, None)]


def test_proxy_preview_is_explicit_and_final_has_no_proxy_option(tmp_path):
    args = runner.parser().parse_args(['develop', '--profile', 'p', '--scenario', 'balatro',
        '--out-dir', str(tmp_path), '--tic-budget', '10', '--wall-seconds', '10', '--model', 'm',
        '--token-limit', '100', '--upstream-url', 'https://example.invalid/v1',
        '--model-proxy-mode', 'explicit', '--model-proxy-url', 'http://127.0.0.1:9999'])
    assert runner._plan(args)['model_proxy'] == {'mode': 'explicit', 'url': 'http://127.0.0.1:9999'}
    final = runner.parser().parse_args(['test', '--profile', 'p', '--scenario', 'balatro',
        '--out-dir', str(tmp_path), '--tic-budget', '10', '--wall-seconds', '10', '--source-programs', 'p'])
    assert runner._plan(final)['model_proxy'] is None
