from types import SimpleNamespace
import sys

import pytest

from coding4game_gui.benchmark.windows import startup
from coding4game_gui.benchmark.windows.drivers.windows_xinput import lifecycle


@pytest.mark.parametrize("responds", [True, False])
def test_geometry_request_is_nonblocking_and_observation_is_bounded(monkeypatch, responds):
    clock = [0.0]
    state = {"posted": False, "polls": 0}
    requests = []

    def client(_hwnd):
        if state["posted"]:
            state["polls"] += 1
        if responds and state["polls"] >= 2:
            return 0, 0, 1280, 720
        return 0, 0, 640, 480

    def post(hwnd, after, x, y, width, height, flags):
        assert flags & 0x4000, "a synchronous request could block on the target thread"
        requests.append((width, height))
        state["posted"] = True

    gui = SimpleNamespace(GetClientRect=client,
                          GetWindowRect=lambda _h: (0, 0, 656, 519),
                          SetWindowPos=post)
    constants = SimpleNamespace(SWP_NOMOVE=2, SWP_NOZORDER=4,
                                SWP_NOACTIVATE=16, SWP_ASYNCWINDOWPOS=0x4000)
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    monkeypatch.setitem(sys.modules, "win32con", constants)
    monkeypatch.setattr(lifecycle, "set_dpi_awareness", lambda: None)
    monkeypatch.setattr(startup.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(startup.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    spec = {"scorer": {"window_setup": {"size": [1280, 720]}}}
    cfg = SimpleNamespace(process="Demo.exe")
    if responds:
        assert startup.enforce_live_window(spec, cfg, hwnd=7, timeout=0.3) == (1280, 720)
        assert clock[0] == pytest.approx(0.1)
    else:
        with pytest.raises(RuntimeError, match="expected.*1280, 720"):
            startup.enforce_live_window(spec, cfg, hwnd=7, timeout=0.3)
        assert 0.3 <= clock[0] <= 0.41
    assert requests and len(requests) <= 3
    assert all(size == (1296, 759) for size in requests)


def test_geometry_corrects_native_one_pixel_rounding(monkeypatch):
    clock = [0.0]
    client = [640, 480]
    outer = [662, 536]
    requests = []
    def post(_h, _after, _x, _y, w, h, _flags):
        requests.append((w, h))
        outer[:] = [w, h]
        client[:] = [w - 23, h - 56]
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        GetClientRect=lambda _h: (0, 0, *client),
        GetWindowRect=lambda _h: (0, 0, *outer), SetWindowPos=post))
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(
        SWP_NOMOVE=2, SWP_NOZORDER=4, SWP_NOACTIVATE=16, SWP_ASYNCWINDOWPOS=0x4000))
    monkeypatch.setattr(lifecycle, "set_dpi_awareness", lambda: None)
    monkeypatch.setattr(startup.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(startup.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    spec = {"scorer": {"window_setup": {"size": [1280, 720]}}}
    assert startup.enforce_live_window(spec, SimpleNamespace(process="Demo.exe"),
                                       hwnd=7, timeout=1) == (1280, 720)
    assert requests == [(1302, 776), (1303, 776)]


@pytest.mark.parametrize("backend", ["printwindow", "printwindow_visible", "visible_client", "dxcam"])
def test_native_capture_targets_the_scaled_physical_client(monkeypatch, backend):
    from coding4game_gui.benchmark.windows.drivers.windows_xinput import renderer
    requested = []
    monkeypatch.setattr(renderer, "printwindow_client_scale", lambda _h: 1.5)
    monkeypatch.setattr(lifecycle, "set_dpi_awareness", lambda: None)
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        GetClientRect=lambda _h: (0, 0, 1920, 1080),
        GetWindowRect=lambda _h: (0, 0, 1944, 1139),
        SetWindowPos=lambda _h, _a, _x, _y, w, h, _f: requested.append((w,h))))
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(
        SWP_NOMOVE=2, SWP_NOZORDER=4, SWP_NOACTIVATE=16, SWP_ASYNCWINDOWPOS=0x4000))
    spec = {"scorer": {"window_setup": {"size": [1280, 720]}}}
    cfg = SimpleNamespace(process="Demo.exe", screenshot_backend=backend)
    assert startup.enforce_live_window(spec, cfg, hwnd=7) == (1920, 1080)
    assert requested == [(1944, 1139)]


@pytest.mark.parametrize('responds', [True, False])
def test_explicit_window_position_is_observed_before_acceptance(monkeypatch, responds):
    clock = [0.0]
    position = [200, 200]
    def post(_h, _after, x, y, _w, _height, flags):
        assert not flags & 2
        assert (x, y) == (0, 0)
        if responds:
            position[:] = [x, y]
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(
        GetClientRect=lambda h: (0, 0, 1280, 720),
        GetWindowRect=lambda h: (*position, position[0]+1296, position[1]+759),
        SetWindowPos=post))
    monkeypatch.setitem(sys.modules, 'win32con', SimpleNamespace(
        SWP_NOMOVE=2, SWP_NOZORDER=4, SWP_NOACTIVATE=16, SWP_ASYNCWINDOWPOS=0x4000))
    monkeypatch.setattr(lifecycle, 'set_dpi_awareness', lambda: None)
    monkeypatch.setattr(startup.time, 'perf_counter', lambda: clock[0])
    monkeypatch.setattr(startup.time, 'sleep', lambda s: clock.__setitem__(0, clock[0]+s))
    spec = {'scorer': {'window_setup': {'size': [1280, 720], 'position': [0, 0]}}}
    if responds:
        assert startup.enforce_live_window(spec, SimpleNamespace(process='Demo.exe'), hwnd=7) == (1280, 720)
        assert position == [0, 0]
    else:
        with pytest.raises(RuntimeError, match='position'):
            startup.enforce_live_window(spec, SimpleNamespace(process='Demo.exe'), hwnd=7, timeout=.3)
