"""Preview or run a serial matrix of stepped Direct baseline episodes."""
import argparse
import json
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from coding4game_gui.operator_config import operator_paths
from coding4game_gui.catalog import FORMAL_SCENARIOS, input_mode
from coding4game_gui.benchmark.spec import load_game_spec

def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('Use a positive integer')
    return number


def commands(args):
    for game in args.games:
        mode = input_mode(game)
        runtime = load_game_spec(game, windows_input=mode)['runtime']
        episode_cap = min(cap for cap in (args.tic_budget, runtime['max_tics']) if cap is not None)
        for model in args.models:
            command = [sys.executable, '-m', 'coding4game_gui.benchmark', 'direct',
                       '--scenario', game, '--episodes', str(args.episodes),
                       '--player-model', model, '--windows-input', mode,
                       '--confirm-paid-run', '--max-tics', str(args.tic_budget),
                       '--out-root', str(args.output_root)]
            yield {'game': game, 'model': model, 'windows_input': mode,
                   'max_tics_per_episode': episode_cap, 'max_model_calls_per_episode': runtime['max_calls_per_ep'],
                   'command': command}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--games', nargs='+', choices=FORMAL_SCENARIOS, required=True)
    parser.add_argument('--models', nargs='+', required=True,
                        help='Configured LiteLLM model names')
    parser.add_argument('--tic-budget', type=positive, default=10000)
    parser.add_argument('--episodes', type=positive, default=3)
    parser.add_argument('--output-root', type=Path,
                        help='Parent directory for run evidence and batch journals')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    if any(not model.strip() or '=' in model for model in args.models):
        parser.error('Use plain configured model names')
    try:
        if args.output_root is None:
            args.output_root = operator_paths(REPO)['output_root']
        args.output_root = args.output_root.resolve()
        jobs = list(commands(args))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({'jobs': jobs, 'execute': args.execute,
                      'protocol': 'direct-stepped',
                      'billing': 'Configured LiteLLM/API billing. Tokens and wall time depend on provider responses.',
                      'effects': 'Serial game sessions. The host freezes the game between timed actions.'},
                     indent=2), flush=True)
    if not args.execute:
        print('Preview only. Add --execute to run the displayed matrix.')
        return 0
    root = args.output_root / '_baseline_batches'
    root.mkdir(parents=True, exist_ok=True)
    journal = root / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8] + '.jsonl')
    # Prevent two copies of this batch launcher from sharing the desktop.
    lock = root / 'active.lock'
    try:
        lease = lock.open('x', encoding='utf-8')
    except FileExistsError:
        parser.error(f'Another batch may be active: {lock}. Verify it has stopped before removing the lock.')
    try:
        with lease:
            lease.write(str(journal))
            lease.flush()
            failures = 0
            for index, job in enumerate(jobs, 1):
                print(f"[C4G batch] {index}/{len(jobs)} {job['game']} {job['model']}", flush=True)
                with journal.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(dict(job, event='started')) + '\n')
                result = subprocess.run(job['command'], cwd=REPO, check=False)
                with journal.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(dict(job, event='exited', exit_code=result.returncode)) + '\n')
                if result.returncode:
                    if result.returncode in {130, -1073741510, 3221225786}:
                        print('[C4G batch] Interrupted; remaining jobs will not start.', flush=True)
                        return result.returncode
                    failures += 1
                    print(f'[C4G batch] Job failed (exit={result.returncode}); continuing remaining jobs.', flush=True)
        print(f'[C4G batch] Finished: {len(jobs) - failures} succeeded, {failures} failed; journal: {journal}', flush=True)
        return 1 if failures else 0
    finally:
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
