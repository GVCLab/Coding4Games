"""Freeze dependency bytes once and reconstruct clean WSL Python environments."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import time
import uuid

from .dependency_manifest import ArtifactCache, lock_id, normalize_environment, validate_lock


CHUNK_BYTES = 1024 * 1024


class DependencyManager:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.runtime = supervisor.runtime
        self.cache = ArtifactCache(self.runtime.profile.root.parent / 'dependency_artifacts')
        self.prepared = {}
        self.logs = (self.runtime.evidence_dir or self.runtime.profile.root / 'diagnostics') / 'dependencies'
        self.logs.mkdir(parents=True, exist_ok=True)

    def _record(self, name, value):
        (self.logs / (name + '.json')).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')

    def _run(self, action, directory, deadline):
        if time.monotonic() >= deadline:
            raise TimeoutError('dependency preparation deadline exhausted')
        label = action + '-' + uuid.uuid4().hex
        evidence = {'action': action, 'role': self.supervisor.role, 'started_unix': time.time(),
                    'status': 'running', 'argv_kind': 'fixed_host_dependency_installer'}
        self._record(label, evidence)
        child = None
        try:
            child = self.supervisor._launch_dependency(
                [str(self.runtime.profile.python), str(directory / 'installer.py'), action, str(directory)],
                cwd=directory, env={'PIP_CONFIG_FILE': '/dev/null', 'PYTHONUNBUFFERED': '1',
                                    'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1'})
            evidence['child_pid'] = child.get('pid')
            evidence['child_cgroup'] = self.supervisor._child_cgroups.get(child['handle'])
            while True:
                state = self.supervisor.poll(child['handle'])
                for event in state['events']:
                    with (self.logs / (label + '.' + event['stream'] + '.log')).open('a', encoding='utf-8') as output:
                        output.write(event['data'])
                if state.get('output_overflow'):
                    raise RuntimeError('dependency output limit exceeded')
                if not state['running'] and state['output_complete']:
                    evidence.update(returncode=state['returncode'], output_complete=True)
                    if state['returncode'] != 0:
                        stderr = self.logs / (label + '.stderr.log')
                        tail = stderr.read_text(encoding='utf-8', errors='replace')[-3000:] if stderr.exists() else ''
                        raise RuntimeError('dependency ' + action + ' failed: ' + tail)
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('dependency preparation deadline exhausted')
                time.sleep(.1)
            evidence['status'] = 'complete'
        except BaseException as error:
            evidence.update(status='failed', error_type=type(error).__name__, error=str(error))
            raise
        finally:
            try:
                if child is not None:
                    # Kill descendants even after pip itself has exited.
                    self.supervisor.terminate(child['handle'])
                    evidence['descendants_stopped'] = True
            except BaseException as error:
                evidence.update(status='failed', cleanup_error=str(error))
                raise
            finally:
                evidence['seconds'] = time.time() - evidence['started_unix']
                self._record(label, evidence)

    def _stage_request(self, request, name):
        files = {'request.json': json.dumps(request).encode(),
                 'installer.py': Path(__file__).with_name('dependency_worker.py').read_bytes(),
                 'dependency_manifest.py': Path(__file__).with_name('dependency_manifest.py').read_bytes()}
        for filename in ['installer.py', 'dependency_manifest.py']:
            data = files[filename]
            target = self.logs / (hashlib.sha256(data).hexdigest() + '-' + filename)
            if not target.exists(): target.write_bytes(data)
        return self.supervisor.stage(files, '.c4g-dependencies/' + name)

    def _read(self, path):
        reply = self.supervisor.call('read_file_chunk', {'path': str(path), 'offset': 0, 'size': CHUNK_BYTES})
        if reply['bytes'] > CHUNK_BYTES:
            raise ValueError('dependency metadata exceeds limit')
        return base64.b64decode(reply['data'], validate=True)

    def _chunks(self, path, size, deadline=float('inf')):
        offset = 0
        while offset < size:
            if time.monotonic() >= deadline:
                raise TimeoutError('dependency transfer deadline exhausted')
            reply = self.supervisor.call('read_file_chunk', {'path': str(path), 'offset': offset, 'size': CHUNK_BYTES})
            data = base64.b64decode(reply['data'], validate=True)
            if not data or reply['bytes'] != size:
                raise ValueError('dependency artifact changed while copying')
            offset += len(data)
            yield data

    def _cache_file(self, item, source, deadline=float('inf')):
        if not self.cache.contains(item['sha256'], item['bytes']):
            self.cache.put(item['sha256'], item['bytes'], self._chunks(source, item['bytes'], deadline))

    def _materialize(self, item, path, deadline=float('inf')):
        if not self.cache.contains(item['sha256'], item['bytes']):
            raise ValueError('frozen dependency artifact is missing from the shared cache')
        offset = 0
        with self.cache.path(item['sha256']).open('rb') as source:
            while block := source.read(CHUNK_BYTES):
                if time.monotonic() >= deadline:
                    raise TimeoutError('dependency transfer deadline exhausted')
                self.supervisor.call('write_file_chunk', {'path': str(path), 'offset': offset,
                    'data': base64.b64encode(block).decode('ascii')})
                offset += len(block)

    def _hydrate(self, lock, deadline):
        wheels = [w for w in lock['wheels'] if not self.cache.contains(w['sha256'], w['bytes'])]
        assets = [a for a in lock['assets'] if not self.cache.contains(a['sha256'], a['bytes'])]
        if not wheels and not assets:
            return
        if any(a['url'] is None for a in assets):
            raise ValueError('captured local asset is missing; supply its frozen SHA-256 artifact')
        plan = {'requirements': [w['name'] + '==' + w['version'] for w in wheels],
                'indexes': lock['indexes'],
                'assets': [{k: a[k] for k in ['path', 'url', 'sha256']} for a in assets]}
        directory = self._stage_request({'environment': plan, 'workspace': str(self.supervisor.workspace),
            'frozen_download': True}, 'restore-' + uuid.uuid4().hex)
        self._run('resolve', directory, min(deadline, time.monotonic() + 900))
        resolved = json.loads(self._read(directory / 'resolved.json'))
        actual = {w['filename']: w for w in resolved['wheels']}
        if set(actual) != {w['filename'] for w in wheels}:
            raise ValueError('downloaded wheels differ from the frozen lock')
        for wheel in wheels:
            found = actual[wheel['filename']]
            if (found['sha256'] != wheel['sha256'] or found['bytes'] != wheel['bytes']
                    or re.sub(r'[-_.]+', '-', found['name']).lower() != wheel['name']
                    or found['version'] != wheel['version']):
                raise ValueError('downloaded wheel does not match its frozen hash')
            self._cache_file(wheel, directory / 'wheels' / wheel['filename'], deadline)
        found_assets = {a['path']: a for a in resolved['assets']}
        if set(found_assets) != {a['path'] for a in assets}:
            raise ValueError('downloaded assets differ from the frozen lock')
        for asset in assets:
            found = found_assets[asset['path']]
            if found['sha256'] != asset['sha256'] or found['bytes'] != asset['bytes']:
                raise ValueError('downloaded asset does not match its frozen hash')
            self._cache_file(asset, PurePosixPath(found['source_path']), deadline)

    def freeze(self, files, dependencies, deadline):
        dependencies = dict(dependencies or {})
        if 'environment_lock' in dependencies:
            raise ValueError('environment_lock is host-generated; declare environment instead')
        if 'environment' not in dependencies:
            return dependencies
        if self.supervisor.role != 'development':
            raise ValueError('dependency resolution is development-only')
        plan = normalize_environment(dependencies['environment'])
        self._record('request-' + uuid.uuid4().hex, plan)
        directory = self._stage_request({'environment': plan, 'workspace': str(self.supervisor.workspace)},
                                        'resolve-' + uuid.uuid4().hex)
        self._run('resolve', directory, min(deadline, time.monotonic() + 900))
        resolved = json.loads(self._read(directory / 'resolved.json'))
        assets = [{k: item[k] for k in ['path', 'url', 'sha256', 'bytes']} for item in resolved['assets']]
        lock = validate_lock({'format': 'c4g-wsl-env-lock-v1',
            'baseline_sha256': self.runtime.profile.baseline_sha256,
            'python': resolved['python'], 'platform': resolved['platform'],
            'requirements': plan['requirements'], 'indexes': plan['indexes'],
            'wheels': resolved['wheels'], 'assets': assets})
        for wheel in lock['wheels']:
            self._cache_file(wheel, directory / 'wheels' / wheel['filename'], deadline)
        for item, source in zip(lock['assets'], resolved['assets']):
            self._cache_file(item, PurePosixPath(source['source_path']), deadline)
        dependencies['environment'] = {key: plan[key] for key in ['requirements', 'indexes']}
        dependencies['environment_lock'] = lock
        self._record('lock-' + lock_id(lock), lock)
        return dependencies

    def prepare(self, record, directory, deadline):
        dependencies = record.get('dependencies') or {}
        lock = dependencies.get('environment_lock')
        if lock is None:
            if 'environment' in dependencies:
                raise ValueError('environment declaration has not been frozen')
            return {'python': str(self.runtime.profile.python), 'env': {}, 'verified': False}
        lock = validate_lock(lock, baseline_sha256=self.runtime.profile.baseline_sha256)
        identity = lock_id(lock)
        if identity in self.prepared:
            return self.prepared[identity]
        final = self.supervisor.role == 'final_test'
        if final and self.supervisor._final_execution_started:
            raise RuntimeError('dependency preparation must precede final execution')
        evidence = {'lock_id': identity, 'program_id': record['id'], 'role': self.supervisor.role,
                    'status': 'preparing', 'network_transitions': [], 'started_unix': time.time()}
        self.supervisor._dependency_preparing = True
        install_root = None
        evidence_name = 'prepare-' + identity + '-' + self.supervisor.role + '-' + uuid.uuid4().hex
        try:
            if final:
                evidence['network_transitions'].append(self.runtime.native.set_dependency_network(
                    self.supervisor.distro, self.runtime._cgroups['final_test'], public=True))
            self._hydrate(lock, deadline)
            install_root = self._stage_request({'lock': lock}, 'env-' + identity + '-' + uuid.uuid4().hex)
            for wheel in lock['wheels']:
                self._materialize(wheel, install_root / 'wheels' / wheel['filename'], deadline)
            for asset in lock['assets']:
                self._materialize(asset, install_root / 'assets' / asset['path'], deadline)
            self._run('install', install_root, min(deadline, time.monotonic() + 600))
            evidence['status'] = 'installed'
        except BaseException as error:
            evidence.update(status='failed', error_type=type(error).__name__, error=str(error))
            if final: self.supervisor._dependency_failed = True
            raise
        finally:
            try:
                if final:
                    evidence['network_transitions'].append(self.runtime.native.set_dependency_network(
                        self.supervisor.distro, self.runtime._cgroups['final_test'], public=False))
                    evidence['final_network_verified'] = True
            except BaseException as error:
                evidence.update(status='failed', network_error=str(error))
                self.supervisor._dependency_failed = True
                raise
            finally:
                if evidence['status'] != 'installed':
                    self.supervisor._dependency_preparing = False
                evidence['seconds'] = time.time() - evidence['started_unix']
                self._record(evidence_name, evidence)
        if evidence['status'] != 'installed':
            raise RuntimeError('dependency preparation did not complete')
        self.supervisor._dependency_preparing = True
        try:
            self._run('verify', install_root, min(deadline, time.monotonic() + 60))
            installed = json.loads(self._read(install_root / 'installed.json'))
            if installed.get('verified') is not True:
                raise ValueError('dependency installation was not verified')
            for filename in ['freeze.txt', 'requirements.lock']:
                (self.logs / (identity + '-' + filename)).write_bytes(self._read(install_root / filename))
            result = {'python': installed['python'], 'env': {'C4G_ASSET_DIR': str(install_root / 'assets')},
                      'verified': True, 'lock_id': identity}
            evidence['status'] = 'verified'
        except BaseException as error:
            evidence.update(status='failed', verification_error=str(error))
            if final: self.supervisor._dependency_failed = True
            raise
        finally:
            self.supervisor._dependency_preparing = False
            self._record(evidence_name, evidence)
        self.prepared[identity] = result
        return result
