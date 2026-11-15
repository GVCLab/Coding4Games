"""No-model tests of the structured GUI wire format and actual session seam."""
import json

import numpy as np
import pytest

from coding4game_gui.benchmark.windows.session import GameSession
from coding4game_gui.runtime_client import client


def test_structured_actions_lower_to_existing_physical_controls():
    actions = [
        dict(type='click', button='left', x=12, y=34, tics=2),
        dict(type='click', button='right', x=12, y=34, tics=2),
        dict(type='click', button='middle', x=12, y=34, tics=2),
        dict(type='double_click', x=12, y=34, tics=2),
        dict(type='move', x=12, y=34, tics=2),
        dict(type='drag', path=[dict(x=1, y=2), dict(x=3, y=4)], tics=3),
        dict(type='scroll', x=12, y=34, scroll_x=1, scroll_y=2, tics=2),
        dict(type='keypress', keys=['CTRL', 'A'], tics=2),
        dict(type='wait', tics=4),
    ]
    assert client.gui_action_plan(actions) == [
        (['CLICK[12,34]'], 2), (['RCLICK[12,34]'], 2),
        (['MCLICK[12,34]'], 2),
        (['DOUBLE_CLICK[12,34]'], 2), (['MOVE[12,34]'], 2),
        (['DRAG[1,2,3,4]'], 3), (['SCROLL[12,34,1,-2]'], 2),
        (['KEY[CTRL]', 'KEY[A]'], 2), (['NEUTRAL'], 4),
    ]


@pytest.mark.parametrize('actions', [
    [], {}, [None], [dict(type='wait', tics=True)],
    [dict(type='wait', tics=0)], [dict(type='wait', tics=1, extra=1)],
    [dict(type='click', button='auxiliary', x=1, y=2, tics=1)],
    [dict(type='click', button='left', x=True, y=2, tics=1)],
    [dict(type='move', x=1280, y=0, tics=1)],
    [dict(type='move', x=0, y=768, tics=1)],
    [dict(type='keypress', keys=['CTRL', 'ESC'], tics=1)],
    [dict(type='keypress', keys=[], tics=1)],
    [dict(type='keypress', keys=['A', 'A'], tics=1)],
    [dict(type='drag', path=[dict(x=0, y=0)] * 3, tics=1)],
    [dict(type='scroll', x=0, y=0, scroll_x=0, scroll_y=0, tics=1)],
    [dict(type='scroll', x=0, y=0, scroll_x=11, scroll_y=0, tics=1)],
    [dict(type='type', text='hello', tics=1)],
])
def test_invalid_action_objects_reject(actions):
    with pytest.raises(ValueError):
        client.gui_action_plan(actions)


def test_escape_structured_action():
    assert client.gui_action_plan([dict(type='keypress', keys=['ESC'], tics=1)]) == [
        (['KEY[ESC]'], 1)]


class Env:
    TICRATE = 10
    windows_input = 'gamepad-kbm'

    def __init__(self):
        self.tic = 0
        self.total_reward = 0
        self.done = False
        self.end_reason = None
        self.calls = []
        self.closed = False
        self.reject = None
        self.terminal_at = None
        self.fail_at = None
        self.after_act = lambda: None

    def reset(self, *args): pass
    def window_info(self): return {}
    def frame(self): return np.full((4, 4, 3), self.tic, dtype=np.uint8)
    def close(self): self.closed = True

    def validate_action_plan(self, plan):
        if self.reject and any(self.reject in controls for controls, _ in plan):
            raise ValueError('backend rejected plan')

    def act(self, controls, tics):
        if self.fail_at == len(self.calls):
            raise RuntimeError('input failed')
        self.calls.append((controls, tics))
        self.tic += tics
        self.total_reward += 1
        if self.terminal_at is not None and self.tic >= self.terminal_at:
            self.done = True
            self.end_reason = 'terminal screen reached'
        self.after_act()


def session(tmp_path, env, **kwargs):
    return GameSession(lambda: env, tmp_path, tic_budget=20, **kwargs)


def test_scenario_action_cap_rejects_raw_action_before_budget_or_input(tmp_path):
    env = Env()
    with session(tmp_path, env, max_action_tics=5) as game:
        uid = game.start()['episode_id']
        assert game.status()['active_episode']['max_action_tics'] == 5
        begin = json.loads(
            (tmp_path / 'windows_episode_journal.jsonl').read_text(
                encoding='utf-8').splitlines()[0])
        assert begin['max_action_tics'] == 5
        with pytest.raises(ValueError, match='must not exceed 5'):
            game.step(uid, ['CLICK_CURRENT'], 6)
        assert env.calls == []
        assert game.status()['tics_used'] == 0


def test_scenario_action_cap_rejects_entire_batch_before_prefix_or_truncation(tmp_path):
    env = Env()
    with GameSession(lambda: env, tmp_path, tic_budget=4,
                     max_action_tics=3) as game:
        uid = game.start()['episode_id']
        with pytest.raises(ValueError, match='must not exceed 3'):
            game.step_batch(uid, [
                dict(type='wait', tics=2),
                dict(type='wait', tics=4),
            ])
        assert env.calls == []
        assert game.status()['tics_used'] == 0


def test_omitted_scenario_action_cap_preserves_1000(tmp_path):
    env = Env()
    env.frame = lambda: np.zeros((4, 4, 3), dtype=np.uint8)
    with GameSession(lambda: env, tmp_path, tic_budget=1000) as game:
        uid = game.start()['episode_id']
        game.step_batch(uid, [dict(type='wait', tics=1000)])
        assert env.calls == [(['NEUTRAL'], 1000)]
        assert game.status()['max_action_tics'] == 1000


@pytest.mark.parametrize('end_reason', [
    'program_exit', 'tic_budget', 'wall_budget', 'game_exit', 'agent_stopped',
    'game_over',
])
def test_terminal_only_test_retains_cost_and_partial_score_on_early_exit(tmp_path, end_reason):
    env = Env()
    with session(tmp_path, env, require_terminal=True) as game:
        uid = game.start()['episode_id']
        game.step_batch(uid, [dict(type='wait', tics=3)])
        result = game.stop(uid, reason=end_reason)
        assert result['outcome_kind'] == 'incomplete_task'
        assert result['complete'] is False and result['terminal_detected'] is False
        assert result['score'] is None and result['partial_score'] == 1
        assert result['tics'] == 3
        assert game.status()['tics_remaining'] == 17


@pytest.mark.parametrize('terminal_reason', ['terminal:victory', 'game_over'])
@pytest.mark.parametrize('require_terminal', [False, True])
def test_registered_terminal_is_recorded_and_accepted(tmp_path, terminal_reason, require_terminal):
    env = Env()
    env.terminal_at = 3
    env.after_act = lambda: setattr(env, 'end_reason', terminal_reason)
    with session(tmp_path, env, require_terminal=require_terminal) as game:
        uid = game.start()['episode_id']
        result = game.step_batch(uid, [dict(type='wait', tics=3)])
        assert result['complete'] is True and result['terminal_detected'] is True
        assert result['outcome_kind'] == 'valid' and result['score'] == 1


def test_uncapped_rollouts_share_remaining_total_budget(tmp_path):
    with GameSession(Env, tmp_path, tic_budget=20, max_tics=None) as game:
        first = game.start(max_tics=6)['episode_id']
        row = game.step_batch(first, [dict(type='wait', tics=10)])
        assert row['done'] and row['tics'] == 6
        assert game.status()['tics_remaining'] == 14
        second = game.start()['episode_id']
        row = game.step_batch(second, [dict(type='wait', tics=20)])
        assert row['done'] and row['tics'] == 14
        assert game.status()['tics_remaining'] == 0
        with pytest.raises(RuntimeError, match='tic_budget_exhausted'):
            game.start()


def test_batch_is_ordered_with_one_final_observation_and_per_action_journal(tmp_path):
    env = Env()
    with session(tmp_path, env) as game:
        uid = game.start()['episode_id']
        before = len(list((tmp_path/'evidence'/uid).glob('*.png')))
        row = game.step_batch(uid, [dict(type='wait', tics=2), dict(type='move', x=3, y=4, tics=3)])
        assert env.calls == [(['NEUTRAL'], 2), (['MOVE[3,4]'], 3)]
        assert row['tics'] == 5 and row['score'] == 2
        assert row['batch']['executed_actions'] == 2
        assert row['batch']['remaining_actions'] == 0
        assert 'observation' in row
        assert len(list((tmp_path/'evidence'/uid).glob('*.png'))) == before + 1
        events = [json.loads(s) for s in game.journal_path.read_text().splitlines()]
        assert sum(e['event'] == 'progress' for e in events) == 2


def test_entire_batch_validates_before_any_input(tmp_path):
    env = Env()
    with session(tmp_path, env) as game:
        uid = game.start()['episode_id']
        for bad in [dict(type='move', x=-1, y=0, tics=1), dict(type='keypress', keys=['A'], tics=1)]:
            env.reject = 'KEY[A]'
            with pytest.raises(ValueError):
                game.step_batch(uid, [dict(type='wait', tics=1), bad])
            assert env.calls == [] and env.tic == 0


def test_budget_truncates_one_action_and_returns_terminal_image(tmp_path):
    env = Env()
    with session(tmp_path, env, max_tics=5) as game:
        uid = game.start()['episode_id']
        row = game.step_batch(uid, [dict(type='wait', tics=n) for n in (3, 4, 2)])
        assert env.calls == [(['NEUTRAL'], 3), (['NEUTRAL'], 2)]
        assert row['done'] and row['tics'] == 5 and env.closed
        assert row['batch']['executed_actions'] == 2
        assert row['batch']['remaining_actions'] == 1
        assert row['batch']['actions'][-1]['truncated']
        assert row['batch']['stop_reason'] == 'tic_budget'
        assert 'observation' in row


def test_terminal_and_wall_stop_batch_before_remaining_actions(tmp_path):
    env = Env(); env.terminal_at = 2
    with session(tmp_path/'terminal', env) as game:
        uid = game.start()['episode_id']
        row = game.step_batch(uid, [dict(type='wait', tics=2)] * 3)
        assert len(env.calls) == 1 and row['batch']['remaining_actions'] == 2
    env = Env(); now = [0.0]
    env.after_act = lambda: now.__setitem__(0, 2.0)
    with session(tmp_path/'wall', env, deadline=1, clock=lambda: now[0]) as game:
        uid = game.start()['episode_id']
        row = game.step_batch(uid, [dict(type='wait', tics=1)] * 3)
        assert len(env.calls) == 1 and row['batch']['remaining_actions'] == 2
        assert row['batch']['stop_reason'] == 'wall_budget'


def test_execution_failure_keeps_consumed_prefix_and_closes(tmp_path):
    env = Env(); env.fail_at = 1
    with session(tmp_path, env) as game:
        uid = game.start()['episode_id']
        with pytest.raises(RuntimeError, match='input failed'):
            game.step_batch(uid, [dict(type='wait', tics=2)] * 3)
        row = game.status()['episodes'][0]
        assert row['tics'] == 2 and row['outcome_kind'] == 'infrastructure_error'
        assert env.closed


def test_input_modes_cannot_use_each_others_wire_form(tmp_path):
    env = Env()
    with session(tmp_path/'kbm', env) as game:
        uid = game.start()['episode_id']
        with pytest.raises(ValueError, match='structured'):
            game.step(uid, ['NEUTRAL'], 1)
    env = Env(); env.windows_input = 'gamepad'
    with session(tmp_path/'pad', env) as game:
        uid = game.start()['episode_id']
        with pytest.raises(ValueError, match='keyboard/mouse'):
            game.step_batch(uid, [dict(type='wait', tics=1)])
        assert game.step(uid, ['NEUTRAL'], 1)['tics'] == 1


class CapturedPointerEnv(Env):
    def validate_action_plan(self, plan):
        from coding4game_gui.benchmark.windows.raw_actions import resolve_raw_action
        for controls, _ in plan:
            resolve_raw_action(controls, {}, input_mode=self.windows_input,
                               content_size=(4,4))


def test_captured_pointer_tokens_reach_backend_through_game_session(tmp_path):
    env=CapturedPointerEnv()
    with session(tmp_path,env) as game:
        uid=game.start()['episode_id']
        moved=game.step(uid,['RMOVE[2,-1]'],2)
        clicked=game.step(uid,['CLICK_CURRENT'],2)
        assert moved['tics']==2 and clicked['tics']==4
        assert env.calls==[(['RMOVE[2,-1]'],2),(['CLICK_CURRENT'],2)]
        assert game.status()['tics_used']==4


@pytest.mark.parametrize('controls',[
    ['RMOVE[2,-1]','CLICK_CURRENT'], ['RMOVE[1001,0]'], ['RMOVE[bad,1]'],
    ['KEY[W]'],
])
def test_captured_pointer_extension_rejects_invalid_input_before_act(tmp_path,controls):
    env=CapturedPointerEnv()
    with session(tmp_path,env) as game:
        uid=game.start()['episode_id']
        with pytest.raises(ValueError):
            game.step(uid,controls,1)
        assert env.calls==[] and game.status()['tics_used']==0


def test_schema_constants_match_physical_protocol_and_host():
    from coding4game_gui.benchmark.windows.raw_actions import WINDOWS_KBM_KEYS
    from coding4game_gui.benchmark.code.tool_bridge import tools_for_phase
    assert tuple(client.GUI_KEYS) == WINDOWS_KBM_KEYS
    for phase in ('development', 'final_test'):
        host = next(t for t in tools_for_phase(phase) if t['name'] == 'game.step')
        boxed = next(t for t in client.TOOLS if t['name'] == 'game.step')
        assert host == boxed


def test_submission_save_schema_describes_executable_entrypoint():
    tool = next(t for t in client.TOOLS if t['name'] == 'submission.save')
    entrypoint = tool['inputSchema']['properties']['entrypoint']
    assert entrypoint['required'] == ['argv', 'mode', 'runtime']
    assert entrypoint['additionalProperties'] is False
    assert entrypoint['properties']['argv']['minItems'] == 1
    assert entrypoint['properties']['argv']['items'] == {
        'type': 'string', 'minLength': 1,
    }
    assert entrypoint['properties']['mode']['enum'] == ['stepped']
    assert entrypoint['properties']['runtime']['enum'] == ['python']


@pytest.mark.parametrize('phase', ['development', 'final_test'])
def test_http_client_to_real_session_batch_and_duplicate_delivery(tmp_path, phase):
    from coding4game_gui.benchmark.code.open_session import OpenSession
    from coding4game_gui.benchmark.code.tool_bridge import ToolServer
    from coding4game_gui.benchmark.evidence.timeline import DevelopmentTimeline
    import urllib.request

    env = Env()
    with session(tmp_path, env) as game:
        host = OpenSession(game, None, None,
                           DevelopmentTimeline(tmp_path/'timeline.jsonl'), 'unused', phase=phase)
        uid = game.start()['episode_id']
        host._current_uid = uid
        with ToolServer(host, phase=phase) as server:
            boxed = client.Client(server.url, server.token)
            for fields in [{}, dict(controls=['NEUTRAL']), dict(tics=1),
                           dict(actions=[dict(type='wait', tics=1)], controls=['NEUTRAL'], tics=1),
                           dict(actions=[dict(type='wait', tics=1)], tics=1),
                           dict(controls=['NEUTRAL'], tics=1),
                           dict(actions=[dict(type='wait', tics=1), dict(type='move', x=-1, y=0, tics=1)])]:
                with pytest.raises(client.BridgeError):
                    boxed.call('game.step', episode_id=uid, **fields)
                assert env.calls == []
            row = boxed.call('game.step', episode_id=uid,
                             actions=[dict(type='keypress', keys=['A'], tics=1)] * 2)
            assert env.calls == [(['KEY[A]'], 1)] * 2
            assert [item['type'] for item in client._mcp_content(row)] == ['text', 'image']
            raw = json.dumps(dict(request_id='same-batch', tool='game.step', arguments=dict(
                episode_id=uid, actions=[dict(type='wait', tics=1)] * 2))).encode()
            request = urllib.request.Request(server.url, data=raw, headers={
                'Authorization': f'Bearer {server.token}', 'Content-Type': 'application/json'})
            replies = []
            for _ in range(2):
                with boxed._opener.open(request, timeout=5) as response:
                    replies.append(json.load(response))
            assert replies[0] == replies[1]
            assert env.tic == 4 and len(env.calls) == 4


def test_batch_and_separate_calls_produce_same_game_state(tmp_path):
    results = []
    actions = [dict(type='move', x=10, y=20, tics=1), dict(type='wait', tics=3)]
    for batched in (False, True):
        env = Env()
        with session(tmp_path/str(batched), env) as game:
            uid = game.start()['episode_id']
            for group in ([actions] if batched else [[a] for a in actions]):
                row = game.step_batch(uid, group)
            results.append((env.calls, row['tics'], row['score'], row['observation']))
    assert results[0] == results[1]


def test_rounded_tics_and_exact_seconds_survive_episode_end_and_reopen(tmp_path):
    env = Env()
    env.game_running_seconds = .26
    env.after_act = lambda: setattr(env, 'tic', 3)
    with session(tmp_path, env) as game:
        uid = game.start()['episode_id']
        game.step_batch(uid, [dict(type='wait', tics=2)])
        assert game.status()['tics_used'] == 3
        result = game.stop(uid)
        assert result['game_running_seconds'] == pytest.approx(.26)
        assert game.status()['tics_used'] == 3
    with session(tmp_path, Env()) as game:
        assert game.status()['tics_used'] == 3
        assert game.status()['tics_remaining'] == 17


def test_code_stops_at_rounded_budget_without_sub_tic_pulse(tmp_path):
    env = Env()
    env.after_act = lambda: setattr(env, 'tic', 3)
    with GameSession(lambda: env, tmp_path, tic_budget=3) as game:
        uid = game.start()['episode_id']
        row = game.step_batch(uid, [dict(type='wait', tics=2)])
        assert row['done'] and row['tics'] == 3
        assert env.calls == [(['NEUTRAL'], 2)]
        assert row['batch']['actions'][0]['actual_tics'] == 3


@pytest.mark.parametrize('overshoot, valid', [(1, True), (2, True), (3, False)])
def test_final_action_tolerance_keeps_actual_usage_and_stops_batch(tmp_path, overshoot, valid):
    env = Env()
    env.after_act = lambda: setattr(env, 'tic', 3 + overshoot)
    with GameSession(lambda: env, tmp_path, tic_budget=3) as game:
        uid = game.start()['episode_id']
        row = game.step_batch(uid, [dict(type='wait', tics=3), dict(type='wait', tics=1)])
        assert row['complete'] is valid
        assert row['budget_overshoot_tics'] == overshoot
        assert row['tics'] == 3 + overshoot
        assert row['batch']['executed_actions'] == 1
        assert env.calls == [(['NEUTRAL'], 3)]
        assert game.status()['tics_used'] == 3 + overshoot
        with pytest.raises(RuntimeError, match='tic_budget_exhausted'):
            game.start()


def test_long_code_action_preserved_and_terminal_receipt_is_truncated(tmp_path):
    env = Env()
    with GameSession(lambda: env, tmp_path, tic_budget=300) as game:
        uid = game.start()['episode_id']
        row = game.step_batch(uid, [dict(type='wait', tics=100)])
        assert env.calls == [(['NEUTRAL'], 100)]
        assert row['batch']['actions'][0] == dict(
            index=0, requested_tics=100, allowed_tics=100, actual_tics=100, truncated=False)
        original = env.act
        def short(controls, tics):
            original(controls, 3)
            env.done, env.end_reason = True, 'terminal'
        env.act = short
        row = game.step_batch(uid, [dict(type='wait', tics=100), dict(type='wait', tics=1)])
        assert row['batch']['actions'][0]['truncated']
        assert row['batch']['actions'][0]['actual_tics'] == 3
        assert row['batch']['remaining_actions'] == 1


def test_oversized_batch_rejects_valid_prefix_before_budget_clamp(tmp_path):
    env = Env()
    with session(tmp_path, env) as game:
        uid = game.start()['episode_id']
        with pytest.raises(ValueError):
            game.step_batch(uid, [dict(type='wait', tics=1), dict(type='wait', tics=1001)])
        assert env.calls == []
        assert game.status()['tics_used'] == 0


def test_act_parser_enforces_per_action_not_batch_total():
    from coding4game_gui.benchmark.windows.raw_actions import parse_windows_acts
    assert len(parse_windows_acts('ACT 1000 A\nACT 1000 A', 2)) == 2
    with pytest.raises(ValueError, match='1000'):
        parse_windows_acts('ACT 1 A\nACT 1001 A', 2)


def test_gui_prompt_keeps_game_bindings_without_old_wire_examples():
    from coding4game_gui.benchmark.code.prompts import render_development_prompt
    from coding4game_gui.benchmark.spec import load_game_spec
    prompt = render_development_prompt(load_game_spec('brotato', windows_input='gamepad-kbm'))
    assert 'keypress (W)' in prompt and 'keypress (D)' in prompt
    assert 'KEY[W]' not in prompt and 'CLICK[' not in prompt
    assert 'client.call("game.step", actions=' in prompt
    assert 'game.step(controls=["RMOVE[2,-1]"], tics=1)' in prompt
    assert 'game.step(controls=["CLICK_CURRENT"], tics=1)' in prompt
    assert 'game.step(controls=["KEY[W]"]' not in prompt
    assert 'WHEEL STEPS, not pixels' in prompt
    assert 'not the native computer API' in prompt


def test_scroll_coordinates_and_key_limits_match_actual_raw_resolver():
    from coding4game_gui.benchmark.windows.raw_actions import (
        resolve_raw_action, MAX_SCROLL_STEPS, POLICY_WIDTH, POLICY_HEIGHT,
    )
    assert client._X['maximum'] == POLICY_WIDTH - 1
    assert client._Y['maximum'] == POLICY_HEIGHT - 1
    shape = next(s for s in client.GUI_ACTION_SCHEMA['oneOf']
                 if s['properties']['type']['enum'] == ['scroll'])
    assert shape['properties']['scroll_y']['maximum'] == MAX_SCROLL_STEPS
    controls, _ = client.gui_action_plan([dict(type='scroll', x=1279, y=767,
                                              scroll_x=1, scroll_y=2, tics=1)])[0]
    resolved = resolve_raw_action(controls, {}, input_mode='gamepad-kbm', content_size=(1600, 900))
    assert resolved.scrolls == (((1599.0, 899.0), 1, -2),)
    for key in client.GUI_KEYS:
        controls, _ = client.gui_action_plan([dict(type='keypress', keys=[key], tics=1)])[0]
        resolve_raw_action(controls, {}, input_mode='gamepad-kbm', content_size=(1600, 900))


def test_failed_journal_does_not_leave_game_running(tmp_path, monkeypatch):
    from coding4game_gui.benchmark.windows import session as game_session
    env = Env()
    with session(tmp_path, env) as game:
        uid = game.start()['episode_id']
        append = game_session.journal.append
        def failing_append(path, event, *args, **kwargs):
            if event in {'progress', 'batch_error'}:
                raise OSError('journal write failed')
            return append(path, event, *args, **kwargs)
        monkeypatch.setattr(game_session.journal, 'append', failing_append)
        with pytest.raises(OSError):
            game.step_batch(uid, [dict(type='wait', tics=1)] * 2)
        assert env.closed and env.tic == 1
        assert game.status()['episodes'][0]['outcome_kind'] == 'infrastructure_error'
