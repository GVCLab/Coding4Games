from types import SimpleNamespace
import json
import sys

import numpy as np
import pytest
from PIL import Image


@pytest.fixture
def frozen_desktop(monkeypatch):
    from PIL import ImageGrab
    from coding4game_gui.benchmark.windows import auto_nav

    state = {"foreground": 17, "covered": False, "offset": (100, 150),
             "reads": 0, "boxes": []}
    gui = SimpleNamespace(
        GetForegroundWindow=lambda: state["foreground"],
        GetClassName=lambda _hwnd: "overlay",
        IsChild=lambda _parent, _child: False,
        GetClientRect=lambda _hwnd: (0, 0, 500, 300),
        ClientToScreen=lambda _hwnd, xy: (
            xy[0] + state["offset"][0], xy[1] + state["offset"][1]),
        WindowFromPoint=lambda _xy: 99 if state["covered"] else 17)
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(
        MONITOR_DEFAULTTONEAREST=2))
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(
        MonitorFromWindow=lambda _hwnd, _flags: 1,
        GetMonitorInfo=lambda _monitor: {"Monitor": (0, 0, 1920, 1080)}))
    monkeypatch.setattr(auto_nav, "find_game_hwnd", lambda _process: (17, "game"))

    def grab(*, bbox, all_screens, include_layered_windows):
        assert all_screens and include_layered_windows
        state["boxes"].append(bbox)
        state["reads"] += 1
        return Image.new("RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]),
                         (state["reads"],) * 3)

    monkeypatch.setattr(ImageGrab, "grab", grab)
    env = SimpleNamespace(
        _env=SimpleNamespace(_suspended=True),
        _controller=SimpleNamespace(cfg=SimpleNamespace(process="demo.exe")))
    return env, state


@pytest.mark.parametrize("offset,box,partial", [
    ((100, 150), (100, 150, 600, 450), False),
    ((1800, 150), (1800, 150, 1920, 450), True),
])
def test_frozen_probe_reads_independent_desktop_and_records_clipping(
        frozen_desktop, offset, box, partial):
    from coding4game_gui.benchmark.windows.diagnostics.smoke import _capture_frozen_probe

    env, state = frozen_desktop
    state["offset"] = offset
    before, meta = _capture_frozen_probe(env)
    after, later = _capture_frozen_probe(env)
    assert state["boxes"] == [box, box]
    assert meta == later
    assert meta["captured_box"] == list(box)
    assert meta["partial_client"] is partial
    assert np.all(before == 1) and np.all(after == 2)


@pytest.mark.parametrize("problem,match", [
    ("running", "hard-suspended"),
    ("foreground", "foreground"),
    ("covered", "covered"),
    ("offscreen", "no visible"),
])
def test_frozen_probe_rejects_invalid_desktop_evidence(frozen_desktop, problem, match):
    from coding4game_gui.benchmark.windows.diagnostics.smoke import _capture_frozen_probe

    env, state = frozen_desktop
    if problem == "running":
        env._env._suspended = False
    elif problem == "foreground":
        state["foreground"] = 99
    elif problem == "covered":
        state["covered"] = True
    else:
        state["offset"] = (2000, 150)
    with pytest.raises(RuntimeError, match=match):
        _capture_frozen_probe(env)
    assert state["reads"] == 0


@pytest.mark.parametrize("input_mode, frozen_drift, settlement", [
    ("gamepad", 0, 0.0), ("gamepad", 7, None),
    ("gamepad-kbm", 0, None), ("gamepad-kbm", 7, 0.0),
])
def test_generic_smoke_uses_scenario_safe_action_and_serial_cold_starts(
        monkeypatch, tmp_path, input_mode, frozen_drift, settlement):
    from coding4game_gui.benchmark.windows.diagnostics import smoke

    instances = []

    class FakeEnv:
        def __init__(self, scenario, *, windows_input):
            self.scenario = scenario
            self.windows_input = windows_input
            self.resets = []
            self.finishes = 0
            self.tic = 0
            self.done = True
            self.total_reward = settlement
            self.end_reason = None
            self._timing_warned = 0
            self._scorer = SimpleNamespace(classify_phase=lambda _frame: "play")
            self.frozen_reads = 0
            self._env = SimpleNamespace(
                render=lambda: Image.fromarray(self.frame()),
                refresh_frozen_frame=lambda: pytest.fail(
                    "a suspended PrintWindow capture could block"))
            instances.append(self)

        def frozen_frame(self):
            value = self.tic + self.frozen_reads * frozen_drift
            self.frozen_reads += 1
            return Image.fromarray(np.full((2, 2, 3), value, dtype=np.uint8))

        def reset(self, episode_id=0):
            self.resets.append(episode_id)
            self.tic = 0
            self.done = False
            self.total_reward = settlement

        def finalize_score(self):
            return self.total_reward

        def act(self, buttons, tics):
            assert buttons == ["NEUTRAL"]
            self.tic += tics
            return 0.0

        def frame(self):
            return np.full((2, 2, 3), self.tic, dtype=np.uint8)

        def finish_episode(self):
            self.finishes += 1
            self.done = True

        def close(self):
            self.finish_episode()

    monkeypatch.setattr(smoke, "WindowsGameEnv", FakeEnv)
    monkeypatch.setattr(smoke, "_capture_frozen_probe", lambda env: (
        np.asarray(env.frozen_frame()), {"method": "fake_desktop"}))
    monkeypatch.setattr(smoke, "load_game_spec", lambda scenario, **kwargs: {
        "driver_game": "demo_driver",
        "runtime": {"tics_per_second": 2},
        "action_catalog": [{"name": "NEUTRAL", "description": "safe"}],
    })

    report = smoke.run_smoke(
        scenario="demo_scenario", episodes=2, max_tics=2,
        probe_gap_seconds=0, windows_input=input_mode,
        evidence_dir=tmp_path / "evidence")

    env = instances[0]
    assert env.scenario == "demo_scenario"
    assert env.windows_input == report["windows_input"] == input_mode
    assert env.resets == [-1, 0, 1]
    assert [row["tic"] for row in report["episodes"]] == [2, 2]
    assert [row["score"] for row in report["episodes"]] == [settlement, settlement]
    assert report["driver_game"] == "demo_driver"
    assert env.frozen_reads == 4  # Two freeze reads and one independent read at each cap.
    assert report["freeze_probe"]["consumed_tics"] == 3
    assert [row["cap_frozen_surface"]["consumed_tics"]
            for row in report["episodes"]] == [2, 2]
    assert [row["cap_frozen_surface"]["cached_vs_desktop_delta"]
            for row in report["episodes"]] == [2 * frozen_drift, 3 * frozen_drift]
    assert report["freeze_probe"]["frozen_gap_delta"] == frozen_drift
    assert (tmp_path / "evidence/probe_frozen_after.png").is_file()
    assert (tmp_path / "evidence/episode_1_end.png").is_file()
    assert (tmp_path / "evidence/episode_1_cap_desktop.png").is_file()
    observed = [json.loads(line) for line in
                (tmp_path / "evidence/observations.jsonl").read_text(
                    encoding="utf-8").splitlines()]
    assert [(row["episode"], row["tic"], row["score"])
            for row in observed] == [(0, 2, None), (1, 2, None)]
    for row in observed:
        image = np.asarray(Image.open(tmp_path / "evidence" / row["file"]))
        assert np.all(image == row["tic"])


def test_generic_smoke_rejects_unknown_action_before_launch(monkeypatch):
    from coding4game_gui.benchmark.windows.diagnostics import smoke

    monkeypatch.setattr(smoke, "load_game_spec", lambda scenario, **kwargs: {
        "driver_game": "demo",
        "runtime": {"tics_per_second": 10},
        "action_catalog": [{"name": "NEUTRAL", "description": "safe"}],
    })

    import pytest
    with pytest.raises(ValueError, match="not declared"):
        smoke.run_smoke(scenario="demo", episodes=1, action="JUMP")


@pytest.mark.parametrize("explicit_tics", [None, 4])
def test_smoke_obeys_scenario_action_cap_before_launch(monkeypatch, explicit_tics):
    from coding4game_gui.benchmark.windows.diagnostics import smoke

    calls = []
    env = SimpleNamespace(tic=0, done=False, total_reward=0.0,
                          end_reason=None, _timing_warned=0)
    env.reset = lambda **_kw: None
    env.frame = lambda: np.zeros((2, 2, 3), dtype=np.uint8)
    env.finalize_score = lambda: 0.0
    env.finish_episode = lambda: None
    env.close = lambda: None

    def act(_buttons, tics):
        calls.append(tics)
        env.tic += tics

    env.act = act
    monkeypatch.setattr(smoke, "load_game_spec", lambda *_a, **_kw: {
        "driver_game": "demo", "runtime": {"tics_per_second": 10,
                                             "max_action_tics": 3},
        "action_catalog": [{"name": "NEUTRAL"}],
    })

    def construct(*_a, **_kw):
        assert explicit_tics is None, "oversized diagnostics must fail before launch"
        return env

    monkeypatch.setattr(smoke, "WindowsGameEnv", construct)
    kwargs = dict(scenario="demo", episodes=1, max_tics=7, tail_tics=4,
                  action_tics=explicit_tics, skip_freeze_probe=True)
    if explicit_tics is not None:
        with pytest.raises(ValueError, match="must not exceed 3"):
            smoke.run_smoke(**kwargs)
        assert calls == []
    else:
        report = smoke.run_smoke(**kwargs)
        assert calls == [3, 3, 1]
        assert report["max_action_tics"] == 3
        assert report["episodes"][0]["tic"] == 7


@pytest.mark.parametrize("action,valid", [
    ("CLICK[1162,520]", True),
    ("CLICK[1280,520]", False),
])
def test_smoke_validates_pointer_against_production_viewport_before_launch(
        monkeypatch, action, valid):
    from coding4game_gui.benchmark.windows.diagnostics import smoke

    monkeypatch.setattr(smoke, "load_game_spec", lambda *_a, **_k: {
        "driver_game": "demo", "runtime": {"tics_per_second": 10},
        "action_catalog": []})

    def launch(*_args, **_kwargs):
        raise RuntimeError("pointer validation reached launch")

    monkeypatch.setattr(smoke, "WindowsGameEnv", launch)
    if valid:
        with pytest.raises(RuntimeError, match="pointer validation reached launch"):
            smoke.run_smoke(scenario="demo", windows_input="gamepad-kbm", action=action)
    else:
        with pytest.raises(ValueError):
            smoke.run_smoke(scenario="demo", windows_input="gamepad-kbm", action=action)


@pytest.mark.parametrize("action,valid", [
    ("LS[-0.2,0.8]", True),
    ("RT[1.1]", False),
    ("KEY[W]", False),
])
def test_smoke_accepts_valid_dynamic_gamepad_controls_before_launch(
        monkeypatch, action, valid):
    from coding4game_gui.benchmark.windows.diagnostics import smoke

    monkeypatch.setattr(smoke, "load_game_spec", lambda *_a, **_k: {
        "driver_game": "demo", "runtime": {"tics_per_second": 10},
        "action_catalog": [{"name": "NEUTRAL"}]})

    def launch(*_args, **_kwargs):
        raise RuntimeError("validated analog control reached launch")

    monkeypatch.setattr(smoke, "WindowsGameEnv", launch)
    if valid:
        with pytest.raises(RuntimeError, match="validated analog control reached launch"):
            smoke.run_smoke(scenario="demo", windows_input="gamepad", action=action)
    else:
        with pytest.raises(ValueError):
            smoke.run_smoke(scenario="demo", windows_input="gamepad", action=action)


def test_probe_failure_preserves_partial_evidence_and_closes_game(monkeypatch, tmp_path):
    from PIL import ImageGrab
    from coding4game_gui.benchmark.windows.diagnostics import smoke

    closed = []
    fake = SimpleNamespace(
        _episode_id=-1, tic=6, done=False, end_reason=None,
        reset=lambda **_kw: None, act=lambda *_args: None,
        frame=lambda: np.zeros((2, 2, 3), dtype=np.uint8),
        close=lambda: closed.append(True))
    monkeypatch.setattr(smoke, "WindowsGameEnv", lambda *_args, **_kw: fake)
    monkeypatch.setattr(smoke, "load_game_spec", lambda *_args, **_kw: {
        "driver_game": "demo", "runtime": {"tics_per_second": 10},
        "action_catalog": [{"name": "NEUTRAL"}]})

    def fail(_env):
        raise RuntimeError("probe surface covered")

    monkeypatch.setattr(smoke, "_capture_frozen_probe", fail)
    monkeypatch.setattr(ImageGrab, "grab", lambda **_kw: Image.new("RGB", (3, 3)))
    evidence = tmp_path / "failed_probe"
    with pytest.raises(RuntimeError, match="covered"):
        smoke.run_smoke(scenario="demo", evidence_dir=evidence)
    assert closed == [True]
    report = json.loads((evidence / "partial_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed" and report["episodes"] == []
    assert report["in_progress_episode"]["consumed_tics"] == 6
    assert "probe surface covered" in report["error"]
    assert (evidence / "failure_desktop.png").is_file()


def test_long_diagnostic_actions_keep_fine_tail_and_exact_total(monkeypatch, tmp_path):
    from coding4game_gui.benchmark.windows.diagnostics import smoke

    pulses, resets = [], []

    class Env:
        tic = 0
        done = False
        total_reward = 0.0
        end_reason = None
        _timing_warned = 0

        def reset(self, *, episode_id):
            resets.append(episode_id)

        def act(self, buttons, tics):
            assert buttons == ["NEUTRAL"]
            pulses.append(tics)
            self.tic += tics
            self.done = self.tic == 350
            self.end_reason = "game_over" if self.done else None

        def frame(self):
            return np.zeros((2, 2, 3), dtype=np.uint8)

        def finalize_score(self):
            return self.total_reward

        def finish_episode(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(smoke, "WindowsGameEnv", lambda *_a, **_kw: Env())
    monkeypatch.setattr(smoke, "load_game_spec", lambda *_a, **_kw: {
        "driver_game": "demo", "runtime": {"tics_per_second": 10},
        "action_catalog": [{"name": "NEUTRAL"}]})
    monkeypatch.setattr(smoke, "_capture_frozen_probe", lambda _env: pytest.fail(
        "a skipped probe must not run or acquire evidence"))
    clock = iter([10.0, 70.0, 80.0, 92.0])
    monkeypatch.setattr(smoke, "time", SimpleNamespace(
        perf_counter=lambda: next(clock), sleep=lambda _seconds: None))
    report = smoke.run_smoke(
        scenario="demo", episodes=1, max_tics=350, action_tics=50,
        tail_tics=100, skip_freeze_probe=True, evidence_dir=tmp_path/"frames")
    assert pulses == [50] * 5 + [10] * 10
    assert sum(pulses) == report["episodes"][0]["tic"] == 350
    assert resets == [0]
    assert report["environment_setup_s"] == 60.0
    assert report["episodes"][0]["cold_start_s"] == 72.0
    assert report["freeze_probe"] == {"skipped": True, "consumed_tics": 0}
    observed = [json.loads(line) for line in
                (tmp_path/"frames/observations.jsonl").read_text().splitlines()]
    assert [row["tic"] for row in observed] == [
        50, 100, 150, 200, 250, 260, 270, 280, 290, 300, 310, 320, 330, 340, 350]


@pytest.mark.parametrize("kwargs", [
    {"action_tics": 0}, {"action_tics": 1001}, {"tail_tics": -1},
])
def test_invalid_diagnostic_pulses_fail_before_game_launch(monkeypatch, kwargs):
    from coding4game_gui.benchmark.windows.diagnostics import smoke

    monkeypatch.setattr(smoke, "WindowsGameEnv", lambda *_a, **_kw: pytest.fail(
        "invalid action duration must not launch a game"))
    with pytest.raises(ValueError):
        smoke.run_smoke(scenario="demo", **kwargs)
