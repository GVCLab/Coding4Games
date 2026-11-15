"""Fixed, unprivileged wheel resolver and installer for the WSL host controller."""
from __future__ import annotations

import email.parser
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import sysconfig
import urllib.request
import zipfile

try:
    from dependency_manifest import normalize_environment
except ImportError:  # Package import in host-side tests; staged worker remains top-level.
    from .dependency_manifest import normalize_environment


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def pip_arguments(python):
    return [str(python), '-m', 'pip', '--isolated', '--disable-pip-version-check']


def wheel_metadata(path):
    with zipfile.ZipFile(path) as archive:
        metadata = []
        for item in archive.infolist():
            parts = item.filename.split('/')
            if len(parts) == 2 and parts[0].endswith('.dist-info') and parts[1] == 'METADATA':
                metadata.append(item)
        if len(metadata) != 1 or metadata[0].file_size > 4 * 1024 * 1024:
            raise ValueError('invalid wheel metadata')
        return email.parser.BytesParser().parsebytes(archive.read(metadata[0]))


def resolve(root, config):
    wheels = root / 'wheels'
    wheels.mkdir()
    plan = config['environment']
    if plan['requirements']:
        args = pip_arguments(sys.executable) + ['download', '--only-binary=:all:',
            '--no-cache-dir', '--progress-bar', 'off', '--timeout', '30', '--retries', '1',
            '--dest', str(wheels), '--index-url', plan['indexes'][0]]
        if config.get('frozen_download'):
            args += ['--no-deps']
        for index in plan['indexes'][1:]:
            args += ['--extra-index-url', index]
        subprocess.run(args + plan['requirements'], check=True)
    records = []
    for path in sorted(wheels.iterdir()):
        if path.suffix != '.whl' or not path.is_file():
            raise ValueError('only wheel artifacts are supported')
        fields = wheel_metadata(path)
        records.append({'filename': path.name, 'name': fields['Name'], 'version': fields['Version'],
                        'sha256': digest(path), 'bytes': path.stat().st_size})
    assets = []
    for number, item in enumerate(plan['assets']):
        if 'source_path' in item:
            source = Path(item['source_path']).resolve()
            source.relative_to(Path(config['workspace']).resolve())
            if not source.is_file() or source.is_symlink():
                raise ValueError('asset source must be a regular workspace file')
        else:
            source = root / ('asset-' + str(number))
            request = urllib.request.Request(item['url'], headers={'User-Agent': 'Coding4Game-dependency-prepare/1'})
            class PublicRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    normalize_environment({'assets': [{'path': item['path'], 'sha256': item['sha256'], 'url': newurl}]})
                    return super().redirect_request(req, fp, code, msg, headers, newurl)
            opener = urllib.request.build_opener(PublicRedirect())
            with opener.open(request, timeout=30) as response, source.open('xb') as output:
                normalize_environment({'assets': [{'path': item['path'], 'sha256': item['sha256'], 'url': response.geturl()}]})
                total = 0
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > 2 * 1024**3:
                        raise ValueError('asset exceeds size limit')
                    output.write(block)
        if digest(source) != item['sha256']:
            raise ValueError('asset SHA-256 mismatch')
        assets.append({**{k: item[k] for k in ('path', 'sha256')},
                       'url': item.get('url'), 'bytes': source.stat().st_size,
                       'source_path': str(source)})
    result = {'python': platform.python_version(), 'platform': sysconfig.get_platform(),
              'wheels': records, 'assets': assets}
    (root / 'resolved.json').write_text(json.dumps(result))


def install(root, config):
    lock = config['lock']
    if lock['python'] != platform.python_version() or lock['platform'] != sysconfig.get_platform():
        raise ValueError('dependency target differs from the frozen Python target')
    wheels = root / 'wheels'
    assets = root / 'assets'
    assets.mkdir(exist_ok=True)
    for item in lock['wheels']:
        path = wheels / item['filename']
        if path.stat().st_size != item['bytes'] or digest(path) != item['sha256']:
            raise ValueError('wheel integrity mismatch before installation')
    for item in lock['assets']:
        path = assets / item['path']
        if path.stat().st_size != item['bytes'] or digest(path) != item['sha256']:
            raise ValueError('asset integrity mismatch before installation')
    venv = root / 'venv'
    subprocess.run([sys.executable, '-m', 'venv', '--system-site-packages', str(venv)], check=True)
    python = venv / 'bin/python'
    requirements = root / 'requirements.lock'
    requirements.write_text(''.join(f"{w['name']}=={w['version']} --hash=sha256:{w['sha256']}\n" for w in lock['wheels']))
    if lock['wheels']:
        args = pip_arguments(python) + ['install', '--no-index', '--only-binary=:all:',
            '--require-hashes', '--ignore-installed', '--no-cache-dir', '--no-compile', '--progress-bar', 'off',
            '--find-links', str(wheels), '-r', str(requirements)]
        subprocess.run(args, check=True)
    (root / 'installation-complete.json').write_text(json.dumps({'python': str(python)}))


def verify(root, config):
    python = root / 'venv/bin/python'
    subprocess.run(pip_arguments(python) + ['check'], check=True)
    with (root / 'freeze.txt').open('w') as output:
        subprocess.run(pip_arguments(python) + ['freeze', '--all'], stdout=output, check=True)
    (root / 'installed.json').write_text(json.dumps({'python': str(python), 'verified': True,
        'python_version': platform.python_version(), 'platform': sysconfig.get_platform()}))


if __name__ == '__main__':
    os.environ['PIP_CONFIG_FILE'] = os.devnull
    action, path = sys.argv[1:3]
    root = Path(path).resolve()
    config = json.loads((root / 'request.json').read_text())
    if action == 'resolve':
        resolve(root, config)
    elif action == 'install':
        install(root, config)
    elif action == 'verify':
        verify(root, config)
    else:
        raise ValueError('unsupported dependency operation')
