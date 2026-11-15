import json

import numpy as np
import pytest

from coding4game_gui.benchmark.direct import run_direct_baseline as direct


class OneActionWindowsEnv:
    def __init__(self):
        self._tic = 0
        self.finished = 0

    def reset(self, episode_id):
        self.episode_id = episode_id
        self._tic = 0

    def frame(self):
        return np.zeros((16, 16, 3), dtype=np.uint8)

    def act(self, buttons, tics):
        self._tic += tics
        return 0.0

    @property
    def done(self):
        return self._tic > 0

    @property
    def tic(self):
        return self._tic

    @property
    def total_reward(self):
        return 7.0

    def finish_episode(self):
        self.finished += 1

    def close(self):
        pass


@pytest.mark.parametrize('cap,resolved', [(None, 30), (1, 1), (100, 30)])
def test_windows_direct_baseline_uses_episode_ids_and_no_global_tic_budget(
    tmp_path, monkeypatch, cap, resolved,
):
    monkeypatch.delenv("C4G_LLM_LOG", raising=False)
    monkeypatch.delenv("C4G_LLM_PHASE", raising=False)
    spec = {
        "title": "Windows direct test",
        "engine": "windows",
        "driver_game": "fake",
        "gameplay": "Use the controller.",
        "objective": "Win the match.",
        "episode_contract": "Each episode is a fresh start.",
        "action_catalog": [{"name": "A", "description": "confirm"}],
        "runtime": {
            "scenario": "windows_direct_test",
            "tics_per_second": 10,
            "max_tics": 30,
            "max_burst": None,
            "max_calls_per_ep": None,
        },
    }
    monkeypatch.setattr(direct, "load_game_spec", lambda _scenario: spec)
    systems = []

    def player(model, system, messages):
        systems.append(system)
        return "ACT 2 A", 5, 1

    result = direct.run_baseline(
        out_dir=tmp_path / "run",
        scenario="windows_direct_test",
        episodes=2,
        player_model="test-model",
        llm_fn=player,
        make_env=OneActionWindowsEnv,
        llm_log=False,
        prepare_windows=False,
        max_tics=cap,
    )

    assert result["episode_ids"] == [0, 1]
    assert "development_tics_budget" not in result
    assert result["per_episode_max_tics"] == resolved
    assert result["environment_tics"] == 2 * min(2, resolved)
    rows = [json.loads(line) for line in
            (tmp_path / "run" / "episodes.jsonl").read_text().splitlines()]
    assert [row["episode_id"] for row in rows] == [0, 1]
    assert "Win the match." in systems[0]
    assert "`A`: confirm" in systems[0]


def test_direct_cli_names_output_from_selected_input_and_model(tmp_path, monkeypatch):
    calls = []

    def run(**options):
        calls.append(options)
        return {'accepted': True, 'baseline_mean': 0, 'episodes_completed': 1,
                'player_tokens_in': 0, 'player_tokens_out': 0}

    monkeypatch.setattr(direct, 'run_baseline', run)
    with pytest.raises(SystemExit) as exit_result:
        direct.main([
            '--scenario', 'brotato', '--episodes', '1', '--player-model', 'provider/model',
            '--out-root', str(tmp_path), '--confirm-paid-run',
        ])
    assert exit_result.value.code == 0
    assert len(calls) == 1
    output = calls[0]['out_dir']
    assert output.parent == tmp_path
    assert output.name.endswith('_brotato_direct-baseline_provider_model_input-gamepad-kbm')
    assert calls[0]['windows_input'] == 'gamepad-kbm'


def test_windows_direct_baseline_uses_pure_keyboard_mouse_prompt(
        tmp_path, monkeypatch):
    monkeypatch.delenv("C4G_LLM_LOG", raising=False)
    monkeypatch.delenv("C4G_LLM_PHASE", raising=False)
    real_load = direct.load_game_spec
    seen = {}

    def load_spec(scenario, **kwargs):
        seen.update(scenario=scenario, kwargs=kwargs)
        return real_load(scenario, **kwargs)

    monkeypatch.setattr(direct, "load_game_spec", load_spec)
    systems = []

    def player(_model, system, _messages):
        systems.append(system)
        return "ACT 2 KEY[W]", 5, 1

    result = direct.run_baseline(
        out_dir=tmp_path / "run", scenario="brotato", episodes=1, player_model="test-model",
        windows_input="gamepad-kbm", llm_fn=player,
        make_env=OneActionWindowsEnv, llm_log=False, prepare_windows=False)

    assert seen == {
        "scenario": "brotato",
        "kwargs": {"windows_input": "gamepad-kbm"},
    }
    assert result["windows_input"] == "gamepad-kbm"
    assert result['terminal_detection'] == 'registered_terminal'
    assert "Keyboard/mouse guide" in systems[0]
    assert "CLICK[640,384]" in systems[0]
    assert "MOVE[640,384]" in systems[0]
    assert "RCLICK[x,y]" in systems[0]
    assert "MCLICK[x,y]" in systems[0]
    assert "DOUBLE_CLICK[x,y]" in systems[0]
    assert "DRAG[x1,y1,x2,y2]" in systems[0]
    assert "SCROLL[x,y,dx,dy]" in systems[0]
    assert "1280x768" in systems[0]
    assert "Controller guide" not in systems[0]
    assert "Xbox" not in systems[0]
    assert "LS[x,y]" not in systems[0]


def test_windows_direct_baseline_forwards_keyboard_mouse_to_environment(
        tmp_path, monkeypatch):
    from coding4game_gui.benchmark.windows import env as bench_env

    seen = {}

    def make_env(scenario, **kwargs):
        seen.update(scenario=scenario, kwargs=kwargs)
        return OneActionWindowsEnv()

    monkeypatch.setattr(bench_env, "make_bench_env", make_env)
    direct.run_baseline(
        out_dir=tmp_path / "run", scenario="brotato", episodes=1, player_model="test-model",
        windows_input="gamepad-kbm",
        llm_fn=lambda *_args: ("ACT 2 KEY[W]", 1, 1),
        llm_log=False, prepare_windows=False)

    assert seen == {
        "scenario": "brotato",
        "kwargs": {"windows_input": "gamepad-kbm"},
    }
