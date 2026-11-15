import hashlib
import json
import zipfile

import pytest

from coding4game_gui.wsl2 import dependency_worker
from coding4game_gui.wsl2.dependency_worker import install, resolve, verify, wheel_metadata


def write_wheel(path, entries):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def test_wheel_metadata_ignores_nested_vendored_distribution_metadata(tmp_path):
    wheel = tmp_path / 'setuptools-1-py3-none-any.whl'
    write_wheel(wheel, {
        'setuptools-1.dist-info/METADATA': b'Name: setuptools\nVersion: 1\n',
        'setuptools/_vendor/vendored-2.dist-info/METADATA': b'Name: vendored\nVersion: 2\n',
    })
    fields = wheel_metadata(wheel)
    assert fields['Name'] == 'setuptools'
    assert fields['Version'] == '1'


def test_wheel_metadata_rejects_duplicate_top_level_distribution_metadata(tmp_path):
    wheel = tmp_path / 'ambiguous-1-py3-none-any.whl'
    write_wheel(wheel, {
        'first-1.dist-info/METADATA': b'Name: first\nVersion: 1\n',
        'second-1.dist-info/METADATA': b'Name: second\nVersion: 1\n',
    })
    with pytest.raises(ValueError, match='invalid wheel metadata'):
        wheel_metadata(wheel)


def test_wheel_metadata_rejects_oversized_top_level_metadata(tmp_path):
    wheel = tmp_path / 'large-1-py3-none-any.whl'
    write_wheel(wheel, {'large-1.dist-info/METADATA': b'x' * (4 * 1024 * 1024 + 1)})
    with pytest.raises(ValueError, match='invalid wheel metadata'):
        wheel_metadata(wheel)


def asset_plan(url='https://assets.example.com/model.bin'):
    return {'environment': {'requirements': [], 'indexes': ['https://pypi.org/simple'],
        'assets': [{'path': 'models/model.bin', 'url': url, 'sha256': 'a' * 64}]},
        'workspace': '/workspace'}


def test_asset_redirect_handler_rejects_nonpublic_redirect_before_open(tmp_path, monkeypatch):
    def build_opener(handler):
        class Opener:
            def open(self, request, timeout):
                handler.redirect_request(request, None, 302, 'redirect', {},
                                         'http://127.0.0.1/private')
                pytest.fail('unsafe redirect was accepted')
        return Opener()

    monkeypatch.setattr(dependency_worker.urllib.request, 'build_opener', build_opener)
    with pytest.raises(ValueError, match='asset URL'):
        resolve(tmp_path, asset_plan())


def test_asset_download_rejects_unsafe_final_url_even_without_observed_redirect(
        tmp_path, monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return 'https://localhost/private'
        def read(self, size): return b''

    class Opener:
        def open(self, request, timeout): return Response()

    monkeypatch.setattr(dependency_worker.urllib.request, 'build_opener',
                        lambda handler: Opener())
    with pytest.raises(ValueError, match='public host'):
        resolve(tmp_path, asset_plan())


def test_install_uses_no_compile_and_defers_python_verification_until_separate_action(
        tmp_path, monkeypatch):
    wheel = tmp_path / 'wheels' / 'demo-1-py3-none-any.whl'
    wheel.parent.mkdir(); wheel.write_bytes(b'wheel bytes')
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lock = {'python': dependency_worker.platform.python_version(),
            'platform': dependency_worker.sysconfig.get_platform(),
            'wheels': [{'filename': wheel.name, 'name': 'demo', 'version': '1',
                        'sha256': digest, 'bytes': wheel.stat().st_size}], 'assets': []}
    calls = []

    def run(argv, **kwargs):
        calls.append((list(map(str, argv)), kwargs))

    monkeypatch.setattr(dependency_worker.subprocess, 'run', run)
    install(tmp_path, {'lock': lock})

    pip_install = next(argv for argv, _ in calls if 'install' in argv)
    assert '--no-compile' in pip_install
    assert not any('check' in argv or 'freeze' in argv for argv, _ in calls)
    installed_python = json.loads(
        (tmp_path / 'installation-complete.json').read_text())['python'].replace('\\', '/')
    assert installed_python.endswith('/venv/bin/python')
    assert not (tmp_path / 'installed.json').exists()

    calls.clear()
    verify(tmp_path, {'lock': lock})
    assert any('check' in argv for argv, _ in calls)
    assert any('freeze' in argv for argv, _ in calls)
    assert json.loads((tmp_path / 'installed.json').read_text())['verified'] is True
