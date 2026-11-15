"""Failure diagnostics through the real pause path, with all native calls faked."""
from types import SimpleNamespace

import pytest

from coding4game_gui.benchmark.windows.drivers.windows_xinput import proc_freeze, lifecycle


def setup_pulse(monkeypatch, make_driver_env, *, status=0, open_handle=123):
    events, reads, now = [], [], [0.0]
    kernel = SimpleNamespace(
        OpenProcess=lambda *args: events.append('open') or open_handle,
        CloseHandle=lambda handle: events.append('close'))
    native = SimpleNamespace(NtSuspendProcess=lambda handle: events.append('suspend') or status)
    monkeypatch.setattr(proc_freeze, '_AVAILABLE', True)
    monkeypatch.setattr(proc_freeze, '_kernel32', kernel, raising=False)
    monkeypatch.setattr(proc_freeze, '_ntdll', native, raising=False)
    monkeypatch.setattr(proc_freeze.ctypes, 'get_last_error', lambda: 5, raising=False)
    monkeypatch.setattr(lifecycle.psutil, 'Process', lambda pid: reads.append(pid) or SimpleNamespace(
        create_time=lambda: 100.0, status=lambda: 'running'))
    controller = object.__new__(lifecycle.WindowsProcessLifecycle)
    controller.process_name = 'fixture.exe'
    controller._found_cache = SimpleNamespace(pid=42, extras={'hwnd': 9})
    env = make_driver_env(use_speedhack=False)
    env.lifecycle = controller
    monkeypatch.setattr('coding4game_gui.benchmark.windows.driver_env.time.perf_counter', lambda: now[0])
    env._hold_until = lambda end, **kwargs: now.__setitem__(0, end) or False
    env.input.apply_state = lambda *args, **kwargs: events.append('press')
    env.input.reset_pad = lambda *args: events.append('release')
    return env, events, reads


@pytest.mark.parametrize('status,open_handle,expected', [
    (-1073741790, 123, '0xC0000022'),
    (0, 0, '"winerror": 5'),
])
def test_failed_pause_retains_os_failure_and_identity_without_retry(monkeypatch, make_driver_env, status, open_handle, expected):
    env, events, reads = setup_pulse(monkeypatch, make_driver_env, status=status, open_handle=open_handle)
    with pytest.raises(RuntimeError, match='failed to hard-pause game after input pulse') as caught:
        env.step(buttons=[], duration_seconds=.1)
    message = str(caught.value)
    assert expected in message
    assert '"pid": 42' in message and '"process_create_time": 100.0' in message
    assert reads == [42]
    assert events[:3] == ['press', 'release', 'open']
    assert events.count('open') == 1 and events.count('suspend') == bool(open_handle)
    assert events.count('close') == bool(open_handle)
    assert env._suspended is False
    assert env.last_step_timing.live_window_s == pytest.approx(.1)


def test_successful_pause_has_no_failure_identity_query_or_additional_pulse(monkeypatch, make_driver_env):
    env, events, reads = setup_pulse(monkeypatch, make_driver_env)
    env.step(buttons=[], duration_seconds=.1)
    assert events == ['press', 'release', 'open', 'suspend', 'close']
    assert reads == [] and env._suspended is True
    assert env.last_step_timing.live_window_s == pytest.approx(.1)


def test_capture_and_suspend_failures_remain_distinguishable_without_message_leak(monkeypatch, make_driver_env):
    env, events, _ = setup_pulse(monkeypatch, make_driver_env, status=-1073741790)
    def capture():
        raise RuntimeError('private-host-path-and-secret')
    env.renderer.screenshot = capture
    with pytest.raises(RuntimeError) as caught:
        env.step(buttons=[], duration_seconds=.1)
    message = str(caught.value)
    assert '0xC0000022' in message and '"capture_error_type": "RuntimeError"' in message
    assert 'private-host-path-and-secret' not in message
    assert events.count('suspend') == 1


def test_unreadable_failure_identity_preserves_the_original_os_status(monkeypatch, make_driver_env):
    env, _, _ = setup_pulse(monkeypatch, make_driver_env, status=-1073741790)
    def denied(pid):
        raise lifecycle.psutil.AccessDenied(pid)
    monkeypatch.setattr(lifecycle.psutil, 'Process', denied)
    with pytest.raises(RuntimeError) as caught:
        env.step(buttons=[], duration_seconds=.1)
    assert '0xC0000022' in str(caught.value)
    assert '"identity_error": "AccessDenied"' in str(caught.value)


def test_missing_pid_is_reported_without_opening_a_native_handle(monkeypatch, make_driver_env):
    env, events, reads = setup_pulse(monkeypatch, make_driver_env)
    env.lifecycle._found_cache = None
    env.lifecycle.find = lambda: None
    with pytest.raises(RuntimeError) as caught:
        env.step(buttons=[], duration_seconds=.1)
    assert '"stage": "missing_pid"' in str(caught.value)
    assert events == ['press', 'release'] and reads == []


def test_later_success_clears_stale_failure_context(monkeypatch, make_driver_env):
    env, _, reads = setup_pulse(monkeypatch, make_driver_env, status=-1073741790)
    with pytest.raises(RuntimeError):
        env.step(buttons=[], duration_seconds=.1)
    monkeypatch.setattr(proc_freeze._ntdll, 'NtSuspendProcess', lambda handle: 0)
    env.step(buttons=[], duration_seconds=.1)
    assert env._last_suspend_failure is None and env.lifecycle.last_suspend_failure is None
    assert reads == [42]
