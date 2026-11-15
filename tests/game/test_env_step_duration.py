"""step(): per-call duration override + in-run frame sampling (fake bundle)."""
import time

import pytest

def test_step_default_duration_unchanged(make_driver_env):
    env = make_driver_env()
    t0 = time.perf_counter()
    env.step(buttons=[])
    assert 0.08 < time.perf_counter() - t0 < 0.5


@pytest.mark.parametrize('failed', ['reset', 'close'])
def test_close_releases_renderer_even_when_input_cleanup_fails(failed, make_driver_env):
    env = make_driver_env()
    events = []

    def cleanup(name):
        events.append(name)
        if name == failed:
            raise RuntimeError('input cleanup failed')

    env.input.reset = lambda: cleanup('reset')
    env.input.close = lambda: cleanup('close')
    env.renderer.close = lambda: events.append('renderer')
    with pytest.raises(RuntimeError, match='input cleanup failed'):
        env.close()
    assert events == ['reset', 'close', 'renderer']


def test_failed_pre_suspend_capture_freezes_and_reports_error_without_old_frame(monkeypatch, make_driver_env):
    from PIL import Image
    env = make_driver_env(use_speedhack=False)
    now, calls = [0.0], []
    env._last_live_frame = Image.new('RGB', (8, 8), 'red')
    monkeypatch.setattr('coding4game_gui.benchmark.windows.driver_env.time.perf_counter', lambda: now[0])
    env._hold_until = lambda end, **kw: now.__setitem__(0, end) or False
    env.lifecycle.suspend = lambda: True
    def fail():
        calls.append('capture')
        raise RuntimeError('desktop capture client is obscured')
    env.renderer.screenshot = fail
    with pytest.raises(RuntimeError, match='obscured'):
        env.step(buttons=[], duration_seconds=.1)
    assert env._suspended and env._last_live_frame is None
    assert calls == ['capture']


def test_step_duration_override_shortens_the_hold(make_driver_env):
    env = make_driver_env()
    t0 = time.perf_counter()
    env.step(buttons=[], duration_seconds=0.02)
    assert time.perf_counter() - t0 < 0.09


def test_passive_samples_cannot_stop_or_repress_a_continuous_input(monkeypatch, make_driver_env):
    env = make_driver_env(use_speedhack=False)
    env.lifecycle.suspend = lambda: True
    now, edges, elapsed = [0.0], [], []
    def clock():
        now[0] += .001
        return now[0]
    monkeypatch.setattr('coding4game_gui.benchmark.windows.driver_env.time.perf_counter', clock)
    env.input.apply_state = lambda *a, **kw: edges.append(('down', kw['buttons']))
    env.input.reset_pad = lambda *a: edges.append(('up', []))
    def sample(frame, seconds):
        elapsed.append(seconds)
        return True  # A passive consumer does not have cancellation authority.
    env.step(buttons=['SOUTH'], duration_seconds=9.9,
             sample_interval=.3, on_sample=sample)
    assert edges == [('down', ['SOUTH']), ('up', [])]
    assert 30 <= len(elapsed) <= 33
    assert elapsed == sorted(elapsed) and min(elapsed) >= .3 and max(elapsed) < 9.9
    assert not env.last_step_timing.interrupted
    assert 9.9 <= env.last_step_timing.live_window_s < 9.95


@pytest.mark.parametrize("stop_early", [False, True])
def test_long_pulse_streams_without_repressing(monkeypatch, stop_early, make_driver_env):
    env = make_driver_env(use_speedhack=False)
    env.lifecycle.suspend = lambda: True
    now = [0.0]
    def clock():
        now[0] += 0.001
        return now[0]
    monkeypatch.setattr("coding4game_gui.benchmark.windows.driver_env.time.perf_counter", clock)
    edges, samples = [], []
    env.input.apply_state = lambda *a, **kw: edges.append(("down", kw["buttons"]))
    env.input.reset_pad = lambda *a: edges.append(("up", []))
    def consume(frame):
        samples.append(frame.size)
        return stop_early and len(samples) == 3
    env.step(buttons=["SOUTH"], duration_seconds=10,
             sample_interval=0.1, on_frame=consume)
    assert edges == [("down", ["SOUTH"]), ("up", [])]
    assert env.last_step_timing.interrupted is stop_early
    if stop_early:
        assert len(samples) == 3
        assert env.last_step_timing.live_window_s < 1
    else:
        assert len(samples) > 90
        assert env.last_step_timing.live_window_s >= 10


def test_stream_failure_releases_and_freezes(make_driver_env):
    env = make_driver_env(use_speedhack=False)
    edges = []
    env.input.reset_pad = lambda *a: edges.append("release")
    env.lifecycle.suspend = lambda: edges.append("freeze") or True
    def fail(frame):
        raise RuntimeError("scan failed")
    with pytest.raises(RuntimeError, match="scan failed"):
        env.step(buttons=["SOUTH"], duration_seconds=1,
                 sample_interval=0.001, on_frame=fail)
    assert edges == ["release", "freeze"]


@pytest.mark.parametrize("sample_seconds", [.07, .18, .25, .31, .6])
def test_slow_sample_does_not_delay_pulse_deadline(monkeypatch, sample_seconds, make_driver_env):
    """A slow sample callback must not start too close to the hold end."""
    env = make_driver_env(use_speedhack=False)
    env.lifecycle.suspend = lambda: True
    now = [0.0]

    def clock():
        now[0] += .001
        return now[0]

    monkeypatch.setattr("coding4game_gui.benchmark.windows.driver_env.time.perf_counter", clock)
    samples = []

    def record(frame):
        samples.append(now[0])
        now[0] += sample_seconds
        return False

    env.step(buttons=[], duration_seconds=2.6,
             sample_interval=.1, on_frame=record)
    assert samples
    assert 2.6 <= env.last_step_timing.live_window_s < 2.65


def test_stop_request_ends_hold_without_waiting_full_duration(make_driver_env):
    env = make_driver_env(use_speedhack=False)
    env.lifecycle.suspend = lambda: True
    start = time.perf_counter()
    env.step(buttons=[], duration_seconds=100,
             should_stop=lambda: True)
    assert time.perf_counter() - start < 1
    assert env.last_step_timing.interrupted


def test_complete_pulse_reports_live_window_separately_from_step_wall(make_driver_env):
    env = make_driver_env()
    env.lifecycle.resume = lambda: True
    env.lifecycle.suspend = lambda: True
    env._suspended = True

    env.step(buttons=[], duration_seconds=0.02)

    timing = env.last_step_timing
    assert timing is not None
    assert timing.requested_s == pytest.approx(0.02)
    assert timing.live_window_s >= 0.02
    assert timing.step_wall_s >= timing.live_window_s
    assert timing.step_overhead_s == pytest.approx(
        timing.step_wall_s - timing.live_window_s)


def test_control_transitions_and_frozen_capture_are_not_running_time(monkeypatch, make_driver_env):
    env = make_driver_env()
    now = [0.]
    monkeypatch.setattr("coding4game_gui.benchmark.windows.driver_env.time.perf_counter", lambda: now[0])
    def delay(seconds):
        now[0] += seconds
    env.timectl.unpause = lambda: delay(.3)
    env.timectl.pause = lambda: delay(.4)
    env.input.apply_state = lambda *a, **kw: delay(.02)
    env.input.reset_pad = lambda *a: None
    env.lifecycle.suspend = lambda: delay(.5) or True
    env._hold_until = lambda end, **kw: now.__setitem__(0, end) or False
    env.step(buttons=[], duration_seconds=.2)
    assert env.last_step_timing.live_window_s == pytest.approx(.2)
    assert env.last_step_timing.step_wall_s == pytest.approx(1.4)


def test_step_wakes_hard_paused_event_loop_before_live_press(monkeypatch, make_driver_env):
    """A resumed event loop must pump neutral before an edge and hard-freeze after."""
    env = make_driver_env()
    events = []

    original_time_unpause = env.timectl.unpause
    original_time_pause = env.timectl.pause
    original_apply = env.input.apply_state
    original_reset = env.input.reset_pad

    def resume():
        events.append("resume_process")
        return True

    def settle(_seconds):
        events.append("settle_at_zero_speed")

    def time_unpause():
        events.append("time_unpause")
        original_time_unpause()

    def time_pause():
        events.append("time_pause")
        original_time_pause()

    def suspend():
        events.append("suspend_process")
        return True

    def apply_state(index, **state):
        events.append(f"apply:{'+'.join(state['buttons'])}")
        original_apply(index, **state)

    def reset_pad(index):
        events.append("reset")
        original_reset(index)

    env.lifecycle.resume = resume
    env.lifecycle.suspend = suspend
    env.timectl.unpause = time_unpause
    env.timectl.pause = time_pause
    env.input.apply_state = apply_state
    env.input.reset_pad = reset_pad
    env._suspended = True
    monkeypatch.setattr("coding4game_gui.benchmark.windows.driver_env.time.sleep", settle)

    env.step(
        buttons=["SOUTH"], duration_seconds=0.0,
        before_input=lambda: events.append("before_input"),
        live_action=lambda: events.append("live_action"),
    )

    assert events == [
        "resume_process",
        "settle_at_zero_speed",
        "time_unpause",
        "before_input",
        "apply:SOUTH",
        "live_action",
        "reset",
        "time_pause",
        "suspend_process",
    ]
    assert env._suspended is True
    assert env.input.last_state(0)["buttons"] == []


def test_complete_pulse_preparation_failure_is_charged_and_publishes_no_input(
        monkeypatch, make_driver_env):
    env = make_driver_env(use_speedhack=False)
    events = []
    now = [0.0]
    monkeypatch.setattr(
        "coding4game_gui.benchmark.windows.driver_env.time.perf_counter", lambda: now[0])
    env.lifecycle.resume = lambda: events.append("resume") or True
    env.lifecycle.suspend = lambda: events.append("freeze") or True
    env.input.apply_state = lambda *_a, **_kw: events.append("publish")
    env.input.reset_pad = lambda _idx: events.append("reset")
    env._suspended = True

    def fail_preparation():
        events.append("prepare")
        now[0] += .35
        raise RuntimeError("focus failed")

    with pytest.raises(RuntimeError, match="focus failed"):
        env.step(buttons=["SOUTH"], duration_seconds=.1, before_input=fail_preparation)

    assert events == ["resume", "prepare", "reset", "freeze"]
    assert env._suspended is True
    assert env.last_step_timing.live_window_s == pytest.approx(.35)


def test_complete_pulse_charges_successful_preparation_overrun(monkeypatch, make_driver_env):
    env = make_driver_env(use_speedhack=False)
    now = [0.0]
    monkeypatch.setattr(
        "coding4game_gui.benchmark.windows.driver_env.time.perf_counter", lambda: now[0])
    env.lifecycle.resume = lambda: True
    env.lifecycle.suspend = lambda: True
    env._suspended = True

    def prepare():
        now[0] += .35

    env.step(buttons=[], duration_seconds=.1, before_input=prepare)

    assert env.last_step_timing.live_window_s == pytest.approx(.35)


def test_stopped_complete_pulse_does_not_prepare_or_publish(make_driver_env):
    env = make_driver_env(use_speedhack=False)
    events = []
    env.lifecycle.resume = lambda: True
    env.lifecycle.suspend = lambda: True
    env._suspended = True
    env.input.apply_state = lambda *_a, **_kw: events.append("publish")

    env.step(buttons=["SOUTH"], duration_seconds=1,
             before_input=lambda: events.append("prepare"),
             should_stop=lambda: True)

    assert events == []
    assert env.last_step_timing.interrupted is True


def test_stop_during_preparation_prevents_input_publication(make_driver_env):
    env = make_driver_env(use_speedhack=False)
    events = []
    stopped = [False]
    env.lifecycle.resume = lambda: True
    env.lifecycle.suspend = lambda: True
    env._suspended = True
    env.input.apply_state = lambda *_a, **_kw: events.append("publish")

    def prepare():
        events.append("prepare")
        stopped[0] = True

    env.step(buttons=["SOUTH"], duration_seconds=1, before_input=prepare,
             should_stop=lambda: stopped[0])

    assert events == ["prepare"]
    assert env.last_step_timing.interrupted is True


def test_complete_pulse_does_not_add_unaccounted_wait_without_speedhack(
        monkeypatch, make_driver_env):
    env = make_driver_env(use_speedhack=False)
    env.lifecycle.resume = lambda: True
    env.lifecycle.suspend = lambda: True
    env._suspended = True
    monkeypatch.setattr(
        "coding4game_gui.benchmark.windows.driver_env.time.sleep",
        lambda _seconds: pytest.fail("NoopTimeController must not get wake settle"),
    )

    env.step(buttons=["SOUTH"], duration_seconds=0.0)

    assert env._suspended is True
    assert env.input.last_state(0)["buttons"] == []


def test_repeated_complete_pulses_emit_repeated_press_release_edges(monkeypatch, make_driver_env):
    env = make_driver_env()
    edges = []
    original_apply = env.input.apply_state
    original_reset = env.input.reset_pad
    env.lifecycle.resume = lambda: True
    env.lifecycle.suspend = lambda: True
    env._suspended = True
    monkeypatch.setattr("coding4game_gui.benchmark.windows.driver_env.time.sleep", lambda _seconds: None)

    def apply_state(index, **state):
        edges.append(("press", tuple(state["buttons"])))
        original_apply(index, **state)

    def reset_pad(index):
        edges.append(("release", ()))
        original_reset(index)

    env.input.apply_state = apply_state
    env.input.reset_pad = reset_pad

    for _ in range(2):
        env.step(
            buttons=["SOUTH"], duration_seconds=0.0,
        )

    assert edges == [
        ("press", ("SOUTH",)), ("release", ()),
        ("press", ("SOUTH",)), ("release", ()),
    ]


def test_complete_pulse_fails_closed_when_process_resume_fails(make_driver_env):
    env = make_driver_env()
    applied = []
    env.lifecycle.resume = lambda: False
    env.input.apply_state = lambda *_args, **_kwargs: applied.append(True)
    env._suspended = True

    with pytest.raises(RuntimeError, match="failed to resume"):
        env.step(
            buttons=["SOUTH"], duration_seconds=0.0,
        )

    assert env._suspended is True
    assert applied == []


def test_complete_pulse_rehard_pauses_when_timectl_unpause_fails(monkeypatch, make_driver_env):
    env = make_driver_env()
    events = []
    env.lifecycle.resume = lambda: events.append("resume") or True
    env.lifecycle.suspend = lambda: events.append("suspend") or True
    env.input.reset_pad = lambda _idx: events.append("reset")
    env.timectl.unpause = lambda: (_ for _ in ()).throw(RuntimeError("speed write"))
    env.timectl.pause = lambda: events.append("time_pause")
    env._suspended = True
    monkeypatch.setattr("coding4game_gui.benchmark.windows.driver_env.time.sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="speed write"):
        env.step(
            buttons=["SOUTH"], duration_seconds=0.0,
        )

    assert events == ["resume", "reset", "time_pause", "suspend"]
    assert env._suspended is True
