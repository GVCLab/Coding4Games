"""Recorded, no-LLM first-input and cold-start checks using the host protocol."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from coding4game_gui.benchmark.windows.session import GameSession
from coding4game_gui.benchmark.windows.env import make_bench_env
from coding4game_gui.runtime_client.client import gui_action_plan


def run(scenario, out, episodes, actions):
    if out.exists():
        raise ValueError('Use a new evidence directory')
    if episodes <= 0:
        raise ValueError('episodes must be positive')
    gui_action_plan(actions)
    cap = sum(action['tics'] for action in actions) + 10
    state = {'scenario': scenario, 'windows_input': 'gamepad-kbm',
             'model_calls': 0, 'purpose': 'first-input calibration, not formal development',
             'actions': actions, 'episodes': [], 'status': 'running'}
    out.mkdir(parents=True)
    try:
        with GameSession(lambda: make_bench_env(scenario, windows_input='gamepad-kbm'),
                         out, tic_budget=cap*episodes, max_tics=cap,
                         deadline=time.monotonic()+900) as game:
            for index in range(episodes):
                started = time.monotonic()
                row = {'index': index, 'observations': []}
                state['episodes'].append(row)
                initial = game.start(mode='stepped', max_tics=cap)
                uid = initial['episode_id']
                row.update(episode_id=uid, startup_seconds=time.monotonic()-started)
                def observe(name, result):
                    row['observations'].append({'name': name, 'path': result.get('observation_path'),
                        'tics': result.get('tics'), 'score': result.get('score'), 'done': result.get('done')})
                    if not result['done'] and result['score'] is not None:
                        raise AssertionError('Scoring must remain deferred during gameplay')
                    (out/'status.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
                    print(json.dumps({'episode':index, 'observation':name,
                                      'path':result.get('observation_path'),'tics':result.get('tics')}), flush=True)
                observe('initial', initial)
                time.sleep(2)
                observe('frozen_gap', game.observe(uid))
                for number, action in enumerate(actions):
                    result = game.step_batch(uid, [action])
                    observe('action_'+str(number), result)
                    if result['done']:
                        break
                row['result'] = game.stop(uid, reason='acceptance_probe_completed')
                if not row['result'].get('complete'):
                    raise AssertionError('Probe episode did not settle cleanly')
        state['status'] = 'completed_pending_visual_review'
    except BaseException as exc:
        state.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        (out/'status.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', required=True)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--episodes', type=int, default=3)
    parser.add_argument('--actions-json', required=True,
                        help='Explicit registered structured inputs; gameplay time is charged normally')
    args = parser.parse_args()
    run(args.scenario, args.out.resolve(), args.episodes, json.loads(args.actions_json))


if __name__ == '__main__':
    main()
