"""Serial native-game acceptance of the closed-run recovery controller.

Use a separately labelled copy of a closed failed run. The installed Claude CLI
resumes its archived session and receives synthetic local tool calls only.
No provider configuration or real model service is used; formal runs stay intact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

REPO=Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path: sys.path.insert(0,str(REPO))


def execute(args):
    from coding4game_gui.benchmark.code import run_codex as runner
    from coding4game_gui.benchmark.code.model_gateway import ModelGateway
    from coding4game_gui.benchmark.code.recovery_accounting import read_gateway_journal
    from coding4game_gui.benchmark.code.submissions import ProgramStore
    from coding4game_gui.benchmark.evidence.timeline import DevelopmentTimeline
    from coding4game_gui.benchmark.spec import load_game_spec
    from coding4game_gui.benchmark.code.recovery_controller import protocol_hash
    from scripts.acceptance.wsl2_development_recovery import _reply
    source=args.source_run.resolve();config=json.loads((source/'config.json').read_text(encoding='utf-8'))
    if config['status'] not in ('failed','incomplete') or config['agent']!='claude':
        raise ValueError('This fixture requires a closed failed/incomplete Claude development')
    store=ProgramStore(source/'programs')
    if store.final() is not None: raise ValueError('Do not use a selected/completed development as the recovery fixture')
    evaluated=[row['details'] for row in DevelopmentTimeline(source/'development_timeline.jsonl').read()
        if row.get('kind')=='submission.result' and row.get('source')=='harness'
        and row['details'].get('program_returncode')==0 and row['details'].get('complete')
        and row['details'].get('terminal_detected') and row['details'].get('outcome_kind')=='valid']
    if not evaluated: raise ValueError('Source has no proven saved Python candidate for the native fixture')
    candidate=evaluated[-1]['submission_id'];manifest,_=store.get(candidate)
    if manifest['dependencies']: raise ValueError('This baseline-only fixture does not reconstruct source dependency caches')
    root=args.out.resolve();root.mkdir(parents=True,exist_ok=False);out=root/'run_input-gamepad'
    if config['windows_input']!='gamepad': out=root/'run_input-gamepad-kbm'
    out.mkdir();shutil.copytree(source/'programs',out/'programs')
    for name in ('task_prompt.md','episodes.jsonl','windows_episode_journal.jsonl'):
        shutil.copy2(source/name,out/name)
    events=[]
    with (source/'claude_events.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row=json.loads(line)
            if row.get('type')=='system' and row.get('subtype')=='init':
                events.append({'type':'system','subtype':'init','session_id':row['session_id']})
    (out/'claude_events.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in events),encoding='utf-8')
    rows,_=read_gateway_journal(source/'model_gateway.jsonl')
    (out/'model_gateway.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in rows),encoding='utf-8')
    prior=DevelopmentTimeline(source/'development_timeline.jsonl').read()
    with (out/'development_timeline.jsonl').open('w',encoding='utf-8') as stream:
        for row in prior:
            if row.get('kind')=='submission.result' and row.get('source')=='harness':
                stream.write(json.dumps(row)+'\n')
    exports=[]
    for record in config['runtime_exports']:
        if record['role']!='development': continue
        destination=out/'wsl2_exports'/Path(record['path']).name;destination.parent.mkdir(exist_ok=True)
        shutil.copy2(record['path'],destination)
        if hashlib.sha256(destination.read_bytes()).hexdigest()!=record['sha256']:
            raise ValueError('Copied source archive hash mismatch')
        exports.append(dict(record,path=str(destination)))
    spec=load_game_spec(config['scenario'],windows_input=config['windows_input'])
    config.update(out_dir=str(out),runtime_exports=exports,upstream_url='http://127.0.0.1:1',
                  scenario_protocol_sha256=protocol_hash(spec))
    (out/'config.json').write_text(json.dumps(config,indent=2),encoding='utf-8')
    report={'kind':'real_cli_real_game_synthetic_provider_recovery_fixture','status':'running',
        'source_run':str(source),'fixture_run':str(out),'candidate_id':candidate,
        'provider_requests':0,'synthetic_requests':0,'formal_job_accounting_modified':False,
        'historical_confirmed_tokens_reused_as_fixture_data':config['model_budget']['tokens_reported'],
        'historical_development_tics_reused_as_fixture_data':config['game_budget']['tics_used']}
    calls=[('mcp__c4g_game__bench_status',{}),('mcp__c4g_game__submission_run',
           {'id':candidate,'max_tics':config['resolved_max_tics']}),
           ('mcp__c4g_game__submission_finish',{'id':candidate})]
    def transport(method,url,headers,body,timeout):
        request=json.loads(body);report['synthetic_requests']+=1
        if report['synthetic_requests']>8: raise RuntimeError('Synthetic fixture request bound exceeded')
        tools={tool['name'] for tool in request.get('tools',[])}
        if not tools:
            return _reply('anthropic',config['model'])
        if not calls: raise RuntimeError('Unexpected generation after the fixture selection')
        name,arguments=calls.pop(0)
        if name not in tools: raise RuntimeError('Registered tool name is absent from the official CLI request')
        call_id='recovery_fixture_'+str(report['synthetic_requests'])
        events=[('message_start',{'type':'message_start','message':{'id':'msg_'+call_id,
            'type':'message','role':'assistant','model':config['model'],'content':[],
            'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'output_tokens':0}}}),
            ('content_block_start',{'type':'content_block_start','index':0,'content_block':{
                'type':'tool_use','id':call_id,'name':name,'input':{}}}),
            ('content_block_delta',{'type':'content_block_delta','index':0,'delta':{
                'type':'input_json_delta','partial_json':json.dumps(arguments)}}),
            ('content_block_stop',{'type':'content_block_stop','index':0}),
            ('message_delta',{'type':'message_delta','delta':{'stop_reason':'tool_use','stop_sequence':None},
                'usage':{'output_tokens':1}}),('message_stop',{'type':'message_stop'})]
        class Response:
            status=200;headers={'content-type':'text/event-stream'}
            def iter_bytes(self):
                for event,value in events: yield ('event: '+event+'\ndata: '+json.dumps(value)+'\n\n').encode()
            def close(self): pass
        return Response()
    values=['develop','--resume-run',str(out),'--out-dir',str(out),'--profile',config['profile'],
        '--scenario',config['scenario'],'--windows-input',config['windows_input'],'--agent',config['agent'],
        '--model',config['model'],'--upstream-url','http://127.0.0.1:1','--api-key-env','C4G_RECOVERY_FIXTURE_KEY',
        '--tic-budget',str(config['tic_budget']),'--max-tics',str(config['max_tics']),
        '--token-limit',str(config['token_limit']) if config['token_limit'] is not None else 'unlimited',
        '--wall-seconds',str(config['wall_seconds']) if config['wall_seconds'] is not None else 'unlimited',
        '--token-accounting',config['token_accounting'],'--upstream-api',config['upstream_api'],
        '--execute','--confirm-paid-run']
    if config.get('provider_model'): values+=['--provider-model',config['provider_model']]
    if config.get('reasoning_effort'): values+=['--reasoning-effort',config['reasoning_effort']]
    parsed=runner.parser().parse_args(values);deps=runner._dependencies()
    deps['ModelGateway']=lambda *a,**k:ModelGateway(*a,**k,transport=transport)
    os.environ['C4G_RECOVERY_FIXTURE_KEY']='unused-fixture-key'
    try:
        result=runner.execute(parsed,deps)
        if result['status']!='complete' or result['final_program_id']!=candidate:
            raise RuntimeError('Recovery fixture did not explicitly select the original candidate')
        if not result['agent_result'].get('recovery_identity_observed'):
            raise RuntimeError('Original native CLI session identity was not acknowledged')
        report.update(status='submitted_requires_independent_test_audit',
            cumulative_fixture_tics=result['game_budget']['tics_used'],
            new_native_game_tics=result['game_budget']['tics_used']-config['game_budget']['tics_used'],
            synthetic_accounting_increment=result['model_budget']['tokens_reported']-config['model_budget']['tokens_reported'])
    except BaseException as error:
        report.update(status='failed',error=f'{type(error).__name__}: {error}')
        raise
    finally:
        os.environ.pop('C4G_RECOVERY_FIXTURE_KEY',None)
        (root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({'execute':False,'provider_requests':0,'model_inference':False,
            'effects':'Copy labelled fixture evidence, resume the real CLI, run one native candidate evaluation and explicitly select it; independent tests remain separate.'}))
        return 0
    print(json.dumps(execute(args)))
    return 0


if __name__=='__main__': raise SystemExit(main())
