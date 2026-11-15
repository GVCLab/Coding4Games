"""Launch one paid coding-agent experiment immediately; use --preview to inspect only."""
from datetime import datetime
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.code.run_codex import read_task_instructions_file
from coding4game_gui.catalog import FORMAL_SCENARIOS, input_mode
from coding4game_gui.operator_config import load_provider_route, operator_paths

KEY_ENV = 'C4G_SINGLE_UPSTREAM_KEY'


def positive_integer(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Use a positive integer') from None
    if number <= 0:
        raise argparse.ArgumentTypeError('Use a positive integer')
    return number


def optional_limit(value):
    return 'unlimited' if value.lower() == 'unlimited' else positive_integer(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game', choices=FORMAL_SCENARIOS)
    parser.add_argument(
        '--model',
        help='Model selector declared in the provider configuration',
    )
    parser.add_argument('--agent', choices=['claude', 'codex'], default='claude',
                        help='Coding CLI to run inside the WSL2 development environment')
    parser.add_argument('--windows-input', choices=['auto', 'gamepad', 'gamepad-kbm'],
                        default='auto',
                        help='Use the scenario input protocol or choose it explicitly')
    parser.add_argument('--tic-budget', type=positive_integer, default=10000)
    parser.add_argument('--max-tics', type=positive_integer,
                        help='Optional smaller episode cap; the registered scenario cap always applies')
    parser.add_argument('--token-limit', type=optional_limit, default='unlimited')
    parser.add_argument('--wall-seconds', type=optional_limit, default='unlimited')
    parser.add_argument('--reasoning-effort')
    parser.add_argument('--task-instructions-file',
                        help='UTF-8 file (maximum 64 KiB) appended to the development task')
    parser.add_argument('--profile')
    parser.add_argument('--runtime-backend', choices=['wsl2'], default='wsl2')
    parser.add_argument('--provider-config')
    parser.add_argument('--output-root', type=Path,
                        help='Parent directory for generated run directories')
    parser.add_argument('--out-dir', help='Explicit new output directory for a persistent batch')
    parser.add_argument('--resume-run', type=Path,
                        help='Continue a closed WSL2 development with its original configuration and allowance')
    parser.add_argument('--recovery-route-authorization', type=Path,
                        help='Host-only JSON grant bound to this closed run and its authorized new HTTPS hostname')
    parser.add_argument('--development-continuation-authorization', type=Path,
                        help='Host-only JSON grant bound to this closed run and its current final selection')
    parser.add_argument('--minimum-development-fraction', type=float,
                        help='Human review guideline using either original cumulative resource allowance')
    parser.add_argument('--stop-on-http-400', action='store_true',
                        help='Revoke paid forwarding after the first upstream HTTP 400')
    parser.add_argument('--recovery-budget-notice', type=Path,
                        help='Host-only configuration-bound authorization for a generic development review notice')
    parser.add_argument('--preview', action='store_true', help='Inspect configuration without starting an experiment')
    supplied = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(supplied)
    try:
        paths = operator_paths(REPO)
    except ValueError as error:
        parser.error(str(error))
    args.profile = args.profile or str(paths['profile'])
    args.provider_config = args.provider_config or str(paths['credentials'])
    args.output_root = args.output_root or paths['output_root']
    from coding4game_gui.benchmark.code.development_policy import review_fraction
    try:
        review_fraction(args.minimum_development_fraction)
    except ValueError as error:
        parser.error(str(error))
    if args.recovery_route_authorization and not args.resume_run:
        parser.error('--recovery-route-authorization requires --resume-run')
    if args.development_continuation_authorization and not args.resume_run:
        parser.error('--development-continuation-authorization requires --resume-run')
    if args.recovery_budget_notice and not args.resume_run:
        parser.error('--recovery-budget-notice requires --resume-run')
    resumed = None
    if args.resume_run:
        incompatible = {'--game','--model','--agent','--windows-input','--tic-budget',
                        '--max-tics','--token-limit','--wall-seconds','--reasoning-effort',
                        '--task-instructions-file','--profile','--runtime-backend','--out-dir','--output-root',
                        '--minimum-development-fraction','--stop-on-http-400'}
        if any(value.split('=', 1)[0] in incompatible for value in supplied):
            parser.error('--resume-run retains original run options; do not supply new task/model/resource/profile options')
        try:
            args.resume_run = args.resume_run.resolve()
            resumed = json.loads((args.resume_run / 'config.json').read_text(encoding='utf-8'))
            for source, target in [('scenario','game'),('model','model'),('agent','agent'),
                ('windows_input','windows_input'),('tic_budget','tic_budget'),('max_tics','max_tics'),
                ('reasoning_effort','reasoning_effort'),('profile','profile'),('runtime_backend','runtime_backend')]:
                setattr(args, target, resumed.get(source))
            args.token_limit = resumed['token_limit'] if resumed['token_limit'] is not None else 'unlimited'
            args.wall_seconds = resumed['wall_seconds'] if resumed['wall_seconds'] is not None else 'unlimited'
            args.out_dir = str(args.resume_run)
            args.minimum_development_fraction = resumed.get('minimum_development_fraction')
        except (OSError, ValueError, KeyError) as error:
            print(f'Cannot read original development: {error}', file=sys.stderr)
            return 2
    elif not args.game or not args.model:
        parser.error('--game and --model are required for new development')
    elif args.windows_input == 'auto':
        args.windows_input = input_mode(args.game)
    from coding4game_gui.benchmark.code.agent_driver import validate_effort
    validate_effort(args.agent, args.reasoning_effort)
    profile = Path(args.profile).resolve()
    config = Path(args.provider_config).resolve()
    try:
        route = load_provider_route(config, args.model, allow_non_openai=True)
        model, base, key = route.model, route.api_base, route.api_key
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    del key
    if resumed and (model, route.api_mode, route.provider_model) != (
            resumed['model'], resumed.get('upstream_api', 'responses'), resumed.get('provider_model')):
        print('Configured route differs from the original model/protocol.', file=sys.stderr)
        return 2
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    budgets = f'tics-{args.tic_budget}_tokens-{args.token_limit}_wall-{args.wall_seconds}'
    out = args.output_root/f'{stamp}_{args.game}_{args.agent}_{args.model}_{budgets}_{uuid.uuid4().hex[:8]}_input-{args.windows_input}'
    if args.out_dir:
        out = Path(args.out_dir).resolve()
    proxy = resumed.get('model_proxy') if resumed else {'mode':'system','url':None}
    command = [sys.executable, '-m', 'coding4game_gui.benchmark', 'develop',
        '--agent', args.agent, '--profile', str(profile), '--scenario', args.game,
        '--runtime-backend', args.runtime_backend,
        '--windows-input', args.windows_input, '--out-dir', str(out),
        '--tic-budget', str(args.tic_budget), '--token-limit', str(args.token_limit),
        '--wall-seconds', str(args.wall_seconds),
        '--model', model, '--upstream-url', base, '--api-key-env', KEY_ENV,
        '--upstream-api', route.api_mode, '--provider-model', route.provider_model,
        '--credential-root', str(config.parent), '--model-proxy-mode', proxy['mode'],
        '--token-accounting', resumed['token_accounting'] if resumed else 'usage']
    if proxy.get('url'): command += ['--model-proxy-url', proxy['url']]
    if resumed: command += ['--resume-run', str(args.resume_run)]
    if args.recovery_route_authorization:
        command += ['--recovery-route-authorization', str(args.recovery_route_authorization.resolve())]
    if args.development_continuation_authorization:
        command += ['--development-continuation-authorization', str(args.development_continuation_authorization.resolve())]
    if args.minimum_development_fraction is not None:
        command += ['--minimum-development-fraction', str(args.minimum_development_fraction)]
    if args.stop_on_http_400:
        command += ['--stop-on-http-400']
    if args.recovery_budget_notice:
        command += ['--recovery-budget-notice', str(args.recovery_budget_notice.resolve())]
    if args.reasoning_effort:
        command += ['--reasoning-effort', args.reasoning_effort]
    if args.task_instructions_file:
        command += ['--task-instructions-file', str(Path(args.task_instructions_file).resolve())]
        try:
            task_instructions = read_task_instructions_file(args.task_instructions_file)['descriptor']
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    else:
        task_instructions = resumed.get('task_instructions') if resumed else None
    if not args.preview:
        command += ['--execute', '--confirm-paid-run']
    blockers = []
    if args.out_dir and out.exists() and not resumed:
        blockers.append('Output directory already exists; inspect it instead of overwriting')
    if not profile.is_file():
        blockers.append(f'{args.runtime_backend} runtime profile file does not exist')
    else:
        from coding4game_gui.wsl2.profile import WslProfile
        try:
            WslProfile.load(profile).agent(args.agent)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            blockers.append(f'Invalid WSL2 runtime profile: {exc}')
    try:
        spec = load_game_spec(args.game, windows_input=args.windows_input)
        limits = [value for value in (args.max_tics, spec['runtime'].get('max_tics'))
                  if value is not None]
        if limits:
            command += ['--max-tics', str(min(limits))]
        if resumed and not blockers:
            from coding4game_gui.benchmark.code.run_codex import parser as benchmark_parser
            from coding4game_gui.benchmark.code.recovery_controller import load_recovery
            checkpoint = load_recovery(benchmark_parser().parse_args(command[3:]),
                                       WslProfile.load(profile), spec)
    except (ValueError, OSError, KeyError) as exc:
        blockers.append(str(exc))
    print(json.dumps({'command': command, 'out_dir': str(out), 'agent': args.agent,
        'static_blockers': blockers,
        'billing': 'Host model gateway; actual price unknown; post-response usage accounting may exceed the token threshold',
        'limits': {'tic_budget': args.tic_budget, 'token_limit': args.token_limit,
                   'wall_seconds': args.wall_seconds},
        **({'task_instructions': task_instructions} if task_instructions is not None else {}),
        'unlimited_warning': 'Any unlimited token/time setting has no corresponding budget bound',
        'runtime_backend': args.runtime_backend,
        **({'recovery': checkpoint.evidence()} if resumed and not blockers else {}),
        'effects': ('Restore original workspace/session and cumulative accounting; preserve prior evidence; no automatic final test'
                    if resumed else 'Create a clean WSL2 development clone and archive it before cleanup, initialize/launch/close game, write evidence; no automatic final test'),
        'warning': 'Static checks do not prove game startup or WSL2 runtime stability. Do not run concurrent experiments.'},
        indent=2), flush=True)
    if blockers:
        print('Blocked by static preflight. No experiment started.', file=sys.stderr)
        return 2
    if args.preview:
        print('Preview only. No files created and no experiment started.')
        return 0
    route_now = load_provider_route(config, args.model, allow_non_openai=True)
    model_now, base_now, key = route_now.model, route_now.api_base, route_now.api_key
    if ((model_now, base_now, route_now.api_mode, route_now.provider_model) !=
            (model, base, route.api_mode, route.provider_model)):
        print('Provider route changed since preview; restart this command.', file=sys.stderr)
        return 2
    env = dict(os.environ)
    env[KEY_ENV] = key
    env['PYTHONUNBUFFERED'] = '1'
    del key
    try:
        result = subprocess.run(command, cwd=REPO, env=env, check=False)
        if result.returncode:
            return result.returncode
        config_path = out / 'config.json'
        if not config_path.is_file():
            print('Run exited without a result configuration.', file=sys.stderr)
            return 1
        result_config = json.loads(config_path.read_text(encoding='utf-8'))
        if result_config.get('status') != 'complete':
            print('Run did not complete; inspect ' + str(config_path), file=sys.stderr)
            return 1
        return 0
    finally:
        env.pop(KEY_ENV, None)


if __name__ == '__main__':
    raise SystemExit(main())
