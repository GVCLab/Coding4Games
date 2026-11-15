"""No-model, no-game acceptance for frozen WSL dependency preparation and sealing."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from coding4game_gui.wsl2 import WslProfile, WslRuntime
from coding4game_gui.wsl2.dependency_manifest import ArtifactCache
from coding4game_gui.benchmark.code.submissions import ProgramStore


def collect(supervisor, argv, directory, env, out, deadline):
    child = supervisor.launch(argv, cwd=directory, env=env)
    outputs = {'stdout': '', 'stderr': ''}
    while True:
        state = supervisor.poll(child['handle'])
        for event in state['events']:
            outputs[event['stream']] += event['data']
        if not state['running'] and state['output_complete']:
            break
        if time.monotonic() >= deadline:
            supervisor.terminate(child['handle'])
            raise TimeoutError('dependency acceptance execution deadline')
        time.sleep(.1)
    for stream, data in outputs.items():
        (out / (stream + '.log')).write_text(data, encoding='utf-8')
    return {'returncode': state['returncode'], 'output_complete': state['output_complete']}


def execute(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    profile = WslProfile.load(args.profile)
    deadline = time.monotonic() + args.wall_seconds
    runtime = WslRuntime(profile, out)
    summary = {'status': 'running', 'model_calls': 0, 'game_started': False,
               'baseline_sha256': profile.baseline_sha256, 'roles': {}, 'started_unix': time.time(),
               'cold_cache_final': args.cold_cache_final}
    def save():
        (out / 'acceptance.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    save()
    try:
        with runtime:
            runtime.preflight()
            dev = runtime.start('development')
            summary['roles']['development'] = runtime.role_evidence('development')
            assets = []
            for declaration in args.asset:
                name, source = declaration.split('=', 1)
                source = Path(source).resolve()
                sha = hashlib.sha256(source.read_bytes()).hexdigest()
                target = dev.workspace / '.c4g-dependencies' / 'source-assets' / name
                offset = 0
                with source.open('rb') as stream:
                    while data := stream.read(1024 * 1024):
                        dev.call('write_file_chunk', {'path': str(target), 'offset': offset,
                            'data': base64.b64encode(data).decode('ascii')})
                        offset += len(data)
                assets.append({'path': name, 'source_path': str(target), 'sha256': sha})
            files = {'main.py': args.probe.read_bytes()}
            for declaration in args.file:
                name, source = declaration.split('=', 1)
                files[name] = Path(source).read_bytes()
            dependencies = {'environment': {'requirements': args.require,
                'indexes': args.index or ['https://pypi.org/simple'], 'assets': assets}}
            print('Resolving and freezing dependencies.', flush=True)
            dependencies = dev.freeze_dependencies(files, dependencies, deadline)
            store = ProgramStore(out / 'programs', max_bytes=64*1024*1024, max_files=4096)
            record = store.save(files, {'argv': ['main.py'], 'runtime': 'python', 'mode': 'stepped'},
                dependencies=dependencies, execution_platform='linux')
            store.finish(record['id'])
            summary['program_id'] = record['id']
            summary['program_bytes'] = record['total_bytes']
            summary['lock'] = dependencies['environment_lock']
            save()
            for role, supervisor in [('development', dev), ('final_test', runtime.start('final_test'))]:
                print('Preparing ' + role + ' from the frozen lock.', flush=True)
                summary['roles'][role] = runtime.role_evidence(role)
                if role == 'final_test' and args.cold_cache_final:
                    # Exercise production hydration without deleting shared artifacts.
                    cache_root = out / 'cold-cache'
                    if cache_root.exists():
                        raise ValueError('cold-cache acceptance requires an absent cache directory')
                    supervisor._dependencies().cache = ArtifactCache(cache_root)
                target = supervisor.stage(files, 'submitted')
                prepared = supervisor.prepare_program(record, target, deadline)
                env = {**prepared['env'], 'C4G_ACCEPTANCE_ROLE': role, 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1'}
                result = None
                if role == 'final_test':
                    result = collect(supervisor, [prepared['python'], str(target / 'main.py')],
                                     target, env, out, deadline)
                # Development preparation is tested without invoking the payload;
                # the payload's network-denial assertions belong to final only.
                summary[role] = {'preparation': prepared, 'execution': result}
                if role == 'final_test' and (result['returncode'] != 0 or not result['output_complete']):
                    raise RuntimeError('final dependency payload failed; inspect stdout/stderr')
                save()
            copied = ProgramStore(out / 'final-programs').save(files, record['entrypoint'],
                dependencies=record['dependencies'], execution_platform='linux')
            summary['same_program_id'] = copied['id'] == record['id']
            if not summary['same_program_id']:
                raise ValueError('program identity changed during final copy')
            summary['status'] = 'passed'
    except BaseException as error:
        summary.update(status='failed', error_type=type(error).__name__, error=str(error))
        raise
    finally:
        summary['archives'] = runtime.archives
        registered = runtime.native.list_distros()
        checks = []
        for archive in runtime.archives:
            with tarfile.open(archive['path']) as stream:
                names = stream.getnames()
            lifecycle = json.loads((profile.root / 'clone_records' / (archive['distro'] + '.json')).read_text())
            checks.append({'distro': archive['distro'], 'bytes': archive['bytes'],
                'hash_verified': runtime._sha256(archive['path']) == archive['sha256'],
                'unregistered': archive['distro'] not in registered,
                'install_absent': not Path(lifecycle['install']).exists(),
                'excluded_dependency_payload': not any('/.c4g-dependencies/' in n or n.endswith('/.c4g-dependencies') for n in names)})
        summary['cleanup'] = checks
        summary['baseline_unchanged'] = runtime._sha256(profile.baseline) == profile.baseline_sha256
        if summary['status'] == 'passed' and (not summary['baseline_unchanged'] or any(
                not check[key] for check in checks for key in (
                    'hash_verified', 'unregistered', 'install_absent', 'excluded_dependency_payload'))):
            summary.update(status='failed', error_type='AcceptanceError',
                           error='Archive, cleanup, or baseline verification failed')
        summary['elapsed_seconds'] = time.time() - summary['started_unix']
        save()
        print(json.dumps({'status': summary['status'], 'same_program_id': summary.get('same_program_id'),
                          'program_bytes': summary.get('program_bytes'), 'cleanup': checks,
                          'seconds': summary['elapsed_seconds']}), flush=True)
    return 0 if summary['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--probe', type=Path, required=True)
    parser.add_argument('--require', action='append', default=[])
    parser.add_argument('--index', action='append', default=[])
    parser.add_argument('--asset', action='append', default=[], help='Asset-relative path=operator-provided file')
    parser.add_argument('--file', action='append', default=[], help='Bundle-relative path=fixture file')
    parser.add_argument('--wall-seconds', type=int, default=1200)
    parser.add_argument('--cold-cache-final', action='store_true',
                        help='Use a separate empty final cache to test exact public artifact recovery')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({'execute': False, 'model_calls': 0, 'game_started': False,
                          'requirements': args.require, 'out': str(args.out.resolve())}))
        return 0
    if args.wall_seconds <= 0:
        parser.error('--wall-seconds must be positive')
    return execute(args)


if __name__ == '__main__':
    raise SystemExit(main())
