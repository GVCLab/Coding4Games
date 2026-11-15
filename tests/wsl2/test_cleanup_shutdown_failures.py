from types import SimpleNamespace
import subprocess

import pytest

from coding4game_gui.wsl2.native import WslNative
from coding4game_gui.wsl2.runtime_error import WslTransportError
from coding4game_gui.wsl2.supervisor import WslSupervisor


def test_close_retains_failed_child_and_retries_without_reclosing_transport():
    supervisor = object.__new__(WslSupervisor)
    supervisor.closed = False
    supervisor.distro = "test-only"
    path = "/sys/fs/cgroup/c4g0123456789ab/child-" + "a" * 32
    supervisor._child_cgroups = {"owned": path}
    calls = []
    failures = [True, False]

    def cleanup(distro, group):
        calls.append((distro, group))
        if failures.pop(0):
            raise RuntimeError("owned child still populated")

    transport_closes = []
    supervisor.sock = SimpleNamespace(close=lambda: transport_closes.append(True))
    supervisor.runtime = SimpleNamespace(native=SimpleNamespace(kill_child_cgroup=cleanup))
    with pytest.raises(WslTransportError, match="cleanup"):
        supervisor.close()
    assert supervisor._child_cgroups == {"owned": path}
    supervisor.close()
    assert supervisor._child_cgroups == {}
    assert calls == [("test-only", path)] * 2
    assert transport_closes == [True]
    supervisor.close()
    assert len(calls) == 2


def test_close_removes_successful_handles_but_reports_failed_cleanup():
    supervisor = object.__new__(WslSupervisor)
    supervisor.closed = False
    supervisor.distro = "test-only"
    supervisor._child_cgroups = {"good": "good-group", "bad": "bad-group"}

    def cleanup(distro, group):
        if group == "bad-group":
            raise OSError("cleanup failed")

    supervisor.runtime = SimpleNamespace(native=SimpleNamespace(kill_child_cgroup=cleanup))
    with pytest.raises(WslTransportError, match="cleanup"):
        supervisor.close()
    assert supervisor._child_cgroups == {"bad": "bad-group"}


@pytest.mark.parametrize(("stderr", "diagnostic"), [
    (b"OSError: [Errno 39] Directory not empty: secret-value", "errno=39"),
    (b"TimeoutError: owned cgroup remains populated\nsecret-value", "populated_timeout"),
    (b"unrecognized secret-value", "unclassified"),
])
def test_run_cleanup_failure_retains_safe_classification_without_stderr(stderr, diagnostic, monkeypatch):
    native = WslNative()
    calls = []

    def execute(name, argv, **kwargs):
        calls.append(argv)
        raise subprocess.CalledProcessError(1, argv, stderr=stderr)

    monkeypatch.setattr(native, "exec", execute)
    with pytest.raises(RuntimeError, match=diagnostic) as caught:
        native.teardown("test-only", {"table": "c4g0123456789ab", "cgroup": "/sys/fs/cgroup/c4g0123456789ab"})
    assert "secret-value" not in str(caught.value)
    assert len(calls) == 1
