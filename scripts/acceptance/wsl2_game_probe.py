"""Token-free WSL client -> existing Windows Balatro lifecycle acceptance.

This diagnostic launches a known fixture, not a coding CLI or a final bundle.
It preserves Balatro user data and never changes the registered protocol.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def tree_digest(root):
    digest = hashlib.sha256()
    if root.exists():
        for path in sorted(p for p in root.rglob('*') if p.is_file()):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def validate_resolved_data_path(data, appdata, profile):
    """Accept the normal target or the observed Codex MSIX cache layout."""
    if data.name.casefold() != 'balatro':
        raise ValueError('unexpected Balatro directory name')
    if data.parent == appdata:
        return False
    package_root = profile/'AppData'/'Local'/'Packages'
    if data.is_relative_to(package_root):
        parts = data.relative_to(package_root).parts
        if (len(parts) == 4 and parts[0].startswith('OpenAI.Codex_')
                and parts[1:] == ('LocalCache','Roaming','Balatro')):
            return True
    raise ValueError('unexpected Balatro user-data redirection')


def validate_output_paths(data, out):
    """Reject recursive backups and any overlapping restore trees."""
    if out == data or out.is_relative_to(data) or data.is_relative_to(out):
        raise ValueError('evidence and Balatro user-data paths must be disjoint')


def observation_evidence_passed(rows):
    named = {row.get('check'):row for row in rows}
    if not {'start','stop','final_status'} <= named.keys():
        return False
    images = [row['image'] for row in rows if row.get('image')]
    return (len(images) >= 2 and bool(named['start'].get('image'))
            and all(image.get('dimensions') == [1280,768] for image in images)
            and len({image['sha256'] for image in images}) > 1)


def cleanup_controller(process, distro):
    """Attempt distro termination even when killing the Windows launcher fails."""
    errors = []
    try:
        process.kill()
    except OSError as exc:
        errors.append(f'kill: {type(exc).__name__}')
    finally:
        try:
            stopped = subprocess.run(['wsl.exe','--terminate',distro],
                                     capture_output=True,timeout=15)
            if stopped.returncode:
                errors.append('distro terminate failed')
        except (OSError,subprocess.TimeoutExpired) as exc:
            errors.append(f'terminate: {type(exc).__name__}')
    try:
        process.communicate(timeout=10)
    except (OSError,subprocess.TimeoutExpired) as exc:
        errors.append(f'drain: {type(exc).__name__}')
    return errors


CONTROLLER = r'''
import base64, hashlib, json, pathlib, statistics, struct, tempfile, time
root = pathlib.Path(tempfile.mkdtemp(prefix='c4g-game-probe-'))
(root/'c4g_client.py').write_bytes(base64.b64decode(CLIENT))
import sys
sys.path.insert(0,str(root))
from c4g_client import Client, observation_bytes
c = Client(URL,TOKEN,timeout=420)
latencies=[]
for _ in range(10):
    started=time.perf_counter()
    c.call('bench.status')
    latencies.append((time.perf_counter()-started)*1000)
print(json.dumps({'check':'latency','milliseconds':latencies}),flush=True)
def emit(name,result):
    raw=observation_bytes(result) if 'observation' in result else None
    image=None
    if raw:
        assert raw[:8] == b'\x89PNG\r\n\x1a\n'
        image={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
               'dimensions':list(struct.unpack('>II',raw[16:24]))}
        assert image['dimensions']==[1280,768]
    if name == 'start' or not result.get('done',False):
        assert image is not None
    print(json.dumps({'check':name,'image':image,'result':{
        k:v for k,v in result.items() if k not in ('observation','evidence_refs')}}),flush=True)
initial=c.call('game.start',mode='stepped',max_tics=350)
uid=initial['episode_id']
assert initial['score'] is None and not initial['done']
emit('start',initial)
def click(x,y,tics):
    return dict(type='click',button='left',x=x,y=y,tics=tics)
actions=[dict(type='move',x=460,y=264,tics=10),click(460,264,25),
         dict(type='wait',tics=100)]
actions += [click(x,538,12) for x in (395,477,561,643,726)]
actions += [click(562,718,30)] + [dict(type='wait',tics=15)]*3
for index,action in enumerate(actions):
    result=c.call('game.step',episode_id=uid,actions=[action])
    assert result['episode_id']==uid
    if not result['done']:
        assert result['score'] is None
    emit('step_'+str(index),result)
    if result['done']:
        break
result=c.call('game.stop',episode_id=uid)
emit('stop',result)
assert result['done'] and result['complete']
assert result['tics']>0 and result['tics']<=352
assert isinstance(result['score'],(float,int)) and result['score']>0
status=c.call('bench.status')
assert status['tics_used']==result['tics'] and status['active_episode'] is None
print(json.dumps({'check':'final_status','result':status}),flush=True)
'''


def run(distro, out):
    if not distro.startswith('C4G-WSL2-Probe-'):
        raise ValueError('use a dedicated C4G-WSL2-Probe- distro')
    if out.exists() or not out.name.endswith('_input-gamepad-kbm'):
        raise ValueError('use a fresh evidence directory ending _input-gamepad-kbm')
    import psutil
    if any(p.info['name'].lower() == 'balatro.exe'
           for p in psutil.process_iter(['name']) if p.info['name']):
        raise RuntimeError('close the existing Balatro instance before this diagnostic')
    from coding4game_gui.benchmark.windows.session import GameSession
    from coding4game_gui.benchmark.code.open_session import OpenSession
    from coding4game_gui.benchmark.code.submissions import ProgramStore
    from coding4game_gui.benchmark.code.tool_bridge import ToolServer
    from coding4game_gui.benchmark.evidence.timeline import DevelopmentTimeline
    from coding4game_gui.benchmark.windows.env import make_bench_env
    from coding4game_gui.benchmark.windows.drivers.windows_kbm.input import session_ok
    if not session_ok():
        raise RuntimeError('requires the unlocked interactive Windows session')
    appdata = Path(os.environ['APPDATA']).resolve()
    logical_data = appdata/'Balatro'
    data = logical_data.resolve()
    # Packaged desktop callers can redirect APPDATA children into their own
    # LocalCache. Validate the observed target before backing up or moving it.
    if (logical_data.is_symlink()
            or getattr(logical_data,'is_junction',lambda:False)()):
        raise RuntimeError('unexpected Balatro user-data path')
    redirected = validate_resolved_data_path(
        data,appdata,Path(os.environ['USERPROFILE']).resolve())
    parent = data.parent
    validate_output_paths(data,out)
    out.mkdir(parents=True)
    backup = out/'original-Balatro'
    existed = data.exists()
    before = tree_digest(data)
    if existed:
        shutil.copytree(data, backup)
        if tree_digest(backup) != before:
            raise RuntimeError('Balatro backup digest mismatch')
    report = {'status':'running','distro':distro,'model_calls':0,
              'real_coding_cli':False,'frozen_bundle_final_test':False,
              'production_wsl_supervisor':False,'deployment_no_llm_verified':False,
              'workspace_isolation_verified':False,'wall_bound_strict':False,
              'fixture_only':True,'transport':'unchanged Windows loopback ToolServer',
              'tic_budget':350,'wall_seconds':480,'userdata_before':before,
              'userdata_path':str(data),'logical_userdata_path':str(logical_data),
              'appdata_redirected':redirected,
              'host_python':sys.executable,'checks':{}}
    token = None
    def save():
        text = json.dumps(report,indent=2,ensure_ascii=False)
        if token:
            text = text.replace(token,'[REDACTED]')
        (out/'acceptance.json').write_text(text,encoding='utf-8')
    save()
    started = time.monotonic()
    try:
        deadline = started+480
        with ExitStack() as stack:
            game = stack.enter_context(GameSession(
                lambda:make_bench_env('balatro',windows_input='gamepad-kbm'),
                out,tic_budget=350,max_tics=350,deadline=deadline))
            store = ProgramStore(out/'programs')
            timeline = DevelopmentTimeline(out/'development_timeline.jsonl')
            # Only game calls are used; this diagnostic has no artifact supervisor.
            session = OpenSession(game,None,store,timeline,sys.executable,deadline=deadline)
            stack.callback(session.close)
            bridge = stack.enter_context(ToolServer(session))
            session.bind_bridge(bridge)
            token = bridge.token
            timeline.secrets = (token,)
            source = ('CLIENT='+repr(base64.b64encode(
                (REPO/'coding4game_gui/runtime_client/client.py').read_bytes()).decode())
                +'\nURL='+repr(bridge.url)+'\nTOKEN='+repr(token)+'\n'+CONTROLLER)
            process = subprocess.Popen(
                ['wsl.exe','-d',distro,'-u','c4gprobe','--exec','python3','-'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                stdout,stderr = process.communicate(
                    source.encode(),timeout=min(450,max(1,deadline-time.monotonic())))
            except BaseException:
                report['controller_cleanup_errors'] = cleanup_controller(process,distro)
                raise
            safe_stdout = stdout.decode('utf-8',errors='replace').replace(token,'[REDACTED]')
            safe_stderr = stderr.decode('utf-8',errors='replace').replace(token,'[REDACTED]')
            (out/'controller.jsonl').write_text(safe_stdout,encoding='utf-8')
            (out/'controller.stderr.txt').write_text(safe_stderr,encoding='utf-8')
            report['controller_returncode'] = process.returncode
            report['game'] = game.status()
            rows = [json.loads(line) for line in safe_stdout.splitlines() if line.startswith('{')]
            report['checks']['wsl_controller_passed'] = process.returncode == 0
            image_rows = [r for r in rows if r.get('image')]
            report['checks']['images_received'] = len(image_rows)
            report['checks']['observed_frame_changed'] = len({r['image']['sha256'] for r in image_rows})>1
            report['checks']['required_observation_evidence'] = observation_evidence_passed(rows)
            latency = next((r['milliseconds'] for r in rows if r.get('check')=='latency'),[])
            if latency:
                report['status_roundtrip_ms'] = {'median':statistics.median(latency),'max':max(latency)}
            save()
            if process.returncode != 0 or not observation_evidence_passed(rows):
                raise RuntimeError('WSL game fixture failed; inspect controller evidence')
        report['status'] = 'real_game_bridge_passed_submission_and_final_test_unverified'
    except BaseException as exc:
        report.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        # Verify both resolved move targets before touching user data. Keep all
        # original and generated bytes in the evidence directory for recovery.
        generated = out/'generated-Balatro'
        remaining = [p.pid for p in psutil.process_iter(['name'])
            if p.info['name'] and p.info['name'].lower() == 'balatro.exe']
        report['remaining_balatro_pids'] = remaining
        if remaining:
            report.update(status='failed',userdata_restore_pending=True,
                          error='Game cleanup unsafe; original backup retained without moving live data')
            save()
            raise RuntimeError('Balatro is still running; original backup retained for recovery')
        if (data.parent != parent or backup.parent != out or generated.parent != out
                or generated.exists()):
            raise RuntimeError('unsafe restore paths; original backup retained')
        if data.exists():
            shutil.move(str(data),str(generated))
        try:
            if existed:
                shutil.copytree(backup,data)
        except BaseException:
            report.update(status='failed',userdata_restore_pending=True)
            save()
            raise
        report['userdata_restored'] = tree_digest(data) == before
        report['elapsed_seconds'] = time.monotonic()-started
        save()
        print(json.dumps({'status':report['status'],'out':str(out),
                          'userdata_restored':report['userdata_restored'],
                          'model_calls':0}),flush=True)
        if not report['userdata_restored']:
            raise RuntimeError('Balatro restore digest mismatch; backup retained')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--distro',required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--execute',action='store_true')
    args = parser.parse_args()
    if not args.execute:
        parser.error('requires --execute for game launch and save restoration')
    run(args.distro,args.out.resolve())
