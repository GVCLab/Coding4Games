"""Exercise pointer delivery through real helpers with the OS boundary faked."""
import ctypes
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows mouse driver')

@pytest.fixture
def pointer(monkeypatch):
    from coding4game_gui.benchmark.windows import env
    from coding4game_gui.benchmark.windows import mouse_driver
    events = []
    position = [None]
    def move(x, y):
        position[0] = (x, y)
        events.append(('move', (x, y)))
        return 1
    os = Mock()
    os.SetCursorPos.side_effect = move
    os.mouse_event.side_effect = lambda flag, x, y, data, extra: events.append(
        ('button', flag, position[0], data))
    monkeypatch.setattr(mouse_driver, '_user32', os)
    monkeypatch.setattr(mouse_driver.time, 'sleep', lambda _: None)
    drv = mouse_driver.MouseDriver('test', (1280, 960))
    monkeypatch.setattr(drv, 'client_on_screen', lambda: (651, 365, 1280, 960))
    monkeypatch.setattr(drv, 'ensure_restored', lambda: True)
    monkeypatch.setattr(drv, 'cursor_pos', lambda: position[0])
    monkeypatch.setitem(env._MOUSE_DRIVERS, 'test', drv)
    return env, drv, events

@pytest.mark.parametrize('helper,flags', [
    ('_click_game_points', [2, 4]),
    ('_right_click_game_points', [8, 16]),
    ('_middle_click_game_points', [32, 64]),
    ('_double_click_game_points', [2, 4, 2, 4]),
])
def test_click_remains_at_target(pointer, helper, flags):
    env, drv, events = pointer
    kwargs = {'atomic': True} if helper == '_click_game_points' else {}
    getattr(env, helper)('test', None, [(1090, 773)], (1280, 960), **kwargs)
    target = (1741, 1138)
    assert events == [('move', target)] + [('button', flag, target, 0) for flag in flags]


def test_move_has_no_button_events(pointer):
    env, drv, events = pointer
    env._move_game_points('test', None, [(20, 30)], (1280, 960))
    assert events == [('move', (671, 395))]


def test_public_pointer_mapping_uses_both_live_client_axes(pointer, monkeypatch):
    """Preserve calibrated coordinates and map unequal live axes separately.

    The raw-action resolver already maps policy y=469 to calibrated y=440.
    This helper receives that resolved point; it must not convert it twice.
    """
    env, drv, events = pointer
    monkeypatch.setattr(drv, 'client_on_screen',
                        lambda: (645, 479, 1280, 720))

    assert drv.mapped_to_screen(0, 0, (1280, 720)) == (645, 479)
    assert drv.mapped_to_screen(1279, 719, (1280, 720)) == (1924, 1198)
    assert drv.mapped_to_screen(142, 440, (1280, 720)) == (787, 919)

    env._move_game_points(
        'test', None, [(142, 440)], (1280, 720),
        coordinate_size=(1280, 720))
    assert events == [('move', (787, 919))]

    # Unlike the legacy calibration mapper, a differently shaped live client
    # still covers its full height rather than reusing the width scale.
    monkeypatch.setattr(drv, 'client_on_screen',
                        lambda: (10, 20, 1000, 800))
    assert drv.mapped_to_screen(1279, 719, (1280, 720)) == (1009, 819)


@pytest.mark.parametrize('helper,flags', [
    ('_click_game_points', [2, 4]),
    ('_right_click_game_points', [8, 16]),
    ('_middle_click_game_points', [32, 64]),
    ('_double_click_game_points', [2, 4, 2, 4]),
])
def test_every_public_click_uses_direct_client_mapping(
        pointer, monkeypatch, helper, flags):
    env, drv, events = pointer
    monkeypatch.setattr(drv, 'client_on_screen',
                        lambda: (645, 479, 1280, 720))
    kwargs = {'atomic': True} if helper == '_click_game_points' else {}
    getattr(env, helper)(
        'test', None, [(142, 440)], (1280, 720),
        coordinate_size=(1280, 720), **kwargs)
    target = (787, 919)
    assert events == [('move', target)] + [
        ('button', flag, target, 0) for flag in flags]


def _install_window_ownership(monkeypatch, *, game=10, hit=10,
                              foreground=10, child=False):
    gui = SimpleNamespace(
        WindowFromPoint=lambda _point: hit,
        GetForegroundWindow=lambda: foreground,
        IsChild=lambda parent, window: child and parent == game and window == hit,
        GetAncestor=lambda window, _flag: game if child and window == hit else window,
        GetClassName=lambda window: {10: 'Game', 11: 'Child', 20: 'Overlay'}.get(window, ''),
    )
    process = SimpleNamespace(
        GetWindowThreadProcessId=lambda window: (1, window + 1000))
    monkeypatch.setitem(sys.modules, 'win32gui', gui)
    monkeypatch.setitem(sys.modules, 'win32process', process)
    return gui


def test_guard_rejects_foreign_window_before_button_event(pointer, monkeypatch):
    env, drv, events = pointer
    monkeypatch.setattr(drv, 'hwnd', lambda: 10)
    _install_window_ownership(monkeypatch, hit=20)

    with pytest.raises(RuntimeError, match=r"hit\(hwnd=20 pid=1020 class='Overlay'\)"):
        drv.click(100, 100, move_settle=0, button_hold=0,
                  require_game_target=True)

    assert [event for event in events if event[0] == 'button'] == []


def test_guard_accepts_game_child_window(pointer, monkeypatch):
    env, drv, events = pointer
    monkeypatch.setattr(drv, 'hwnd', lambda: 10)
    _install_window_ownership(monkeypatch, hit=11, child=True)

    assert drv.click(100, 100, move_settle=0, button_hold=0,
                     require_game_target=True)
    assert [event[1] for event in events if event[0] == 'button'] == [2, 4]


def test_guard_rejects_foreground_mismatch(pointer, monkeypatch):
    env, drv, events = pointer
    monkeypatch.setattr(drv, 'hwnd', lambda: 10)
    _install_window_ownership(monkeypatch, hit=10, foreground=20)

    with pytest.raises(RuntimeError, match=r"foreground\(hwnd=20"):
        drv.scroll(100, 100, dx=0, dy=1, require_game_target=True)
    assert [event for event in events if event[0] == 'button'] == []


def test_legacy_click_does_not_apply_public_ownership_guard(pointer, monkeypatch):
    env, drv, events = pointer
    monkeypatch.setattr(drv, 'hwnd', lambda: 10)
    _install_window_ownership(monkeypatch, hit=20, foreground=20)

    assert drv.click(100, 100, move_settle=0, button_hold=0)
    assert [event[1] for event in events if event[0] == 'button'] == [2, 4]


def test_public_drag_and_scroll_use_direct_client_mapping(pointer, monkeypatch):
    env, drv, events = pointer
    monkeypatch.setattr(drv, 'client_on_screen',
                        lambda: (645, 479, 1280, 720))

    env._drag_game_paths(
        'test', None, [((0, 0), (1279, 719))], (1280, 720),
        duration=0.1, coordinate_size=(1280, 720))
    assert events[0:2] == [
        ('move', (645, 479)), ('button', 2, (645, 479), 0)]
    assert events[-2:] == [
        ('move', (1924, 1198)), ('button', 4, (1924, 1198), 0)]

    events.clear()
    env._scroll_game_points(
        'test', None, [((142, 440), 2, -3)], (1280, 720),
        coordinate_size=(1280, 720))
    assert events == [
        ('move', (787, 919)),
        ('button', 0x1000, (787, 919), ctypes.c_ulong(240).value),
        ('button', 0x0800, (787, 919), ctypes.c_ulong(-360).value),
    ]


def test_drag_holds_until_endpoint_and_releases(pointer):
    env, drv, events = pointer
    env._drag_game_paths('test', None, [((0, 0), (1279, 959))], (1280, 960), duration=0.1)
    assert events[0:2] == [('move', (651, 365)), ('button', 2, (651, 365), 0)]
    assert all(e[0] == 'move' for e in events[2:-1])
    assert events[-2:] == [('move', (1930, 1324)), ('button', 4, (1930, 1324), 0)]
    points = [e[1] for e in events if e[0] == 'move']
    assert all(a[0] <= b[0] and a[1] <= b[1] for a, b in zip(points, points[1:]))


@pytest.mark.parametrize('raises', [False, True])
def test_drag_releases_on_midway_failure(pointer, monkeypatch, raises):
    env, drv, events = pointer
    original = drv.move
    calls = 0
    def failing_move(x, y):
        nonlocal calls
        calls += 1
        if calls == 3:
            if raises:
                raise RuntimeError('injected move failure')
            return False
        return original(x, y)
    monkeypatch.setattr(drv, 'move', failing_move)
    with pytest.raises(RuntimeError):
        env._drag_game_paths('test', None, [((0, 0), (100, 100))], (1280, 960), duration=0.1)
    assert [e[1] for e in events if e[0] == 'button'] == [2, 4]
    assert events[-1][0:2] == ('button', 4)


@pytest.mark.parametrize('dx,dy', [(2, 3), (-2, -3)])
def test_structured_scroll_direction_and_target(pointer, dx, dy):
    from coding4game_gui.runtime_client.client import gui_action_plan
    from coding4game_gui.benchmark.windows.raw_actions import resolve_raw_action
    env, drv, events = pointer
    controls, _ = gui_action_plan([dict(type='scroll', x=640, y=384, scroll_x=dx, scroll_y=dy, tics=2)])[0]
    resolved = resolve_raw_action(controls, {}, input_mode='gamepad-kbm', content_size=(1280, 960))
    env._scroll_game_points('test', None, resolved.scrolls, (1280, 960))
    assert events[0] == ('move', (1291, 845))
    assert events[1:] == [('button', 0x1000, (1291, 845), ctypes.c_ulong(dx*120).value),
                          ('button', 0x0800, (1291, 845), ctypes.c_ulong(-dy*120).value)]

@pytest.mark.parametrize('kind', ['click', 'drag', 'scroll'])
def test_failed_initial_move_emits_no_button(pointer, monkeypatch, kind):
    env, drv, events = pointer
    monkeypatch.setattr(drv, 'move', lambda *args: False)
    if kind == 'drag':
        result = drv.drag((0, 0), (100, 100), duration=0.1)
    elif kind == 'scroll':
        result = drv.scroll(100, 100, dx=1, dy=1)
    else:
        result = drv.click(100, 100)
    assert result is False
    assert events == []


def test_drag_duration_and_stationary_endpoint(pointer, monkeypatch):
    from coding4game_gui.benchmark.windows import mouse_driver
    env, drv, events = pointer
    now = [0.0]
    monkeypatch.setattr(mouse_driver.time, 'perf_counter', lambda: now[0])
    monkeypatch.setattr(mouse_driver.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert drv.drag((100, 100), (100, 100), duration=0.3)
    assert now[0] == pytest.approx(0.3)
    assert all(e[1] == (751, 465) for e in events if e[0] == 'move')
    assert [e[1] for e in events if e[0] == 'button'] == [2, 4]


def test_drag_then_click_has_separate_button_pulses(pointer):
    env, drv, events = pointer
    env._drag_game_paths('test', None, [((10, 10), (100, 100))], (1280, 960), duration=0.1)
    env._click_game_points('test', None, [(200, 200)], (1280, 960), atomic=True)
    assert [e[1] for e in events if e[0] == 'button'] == [2, 4, 2, 4]
    assert events[-1] == ('button', 4, (851, 565), 0)


def test_calibrated_navigation_keeps_existing_parking(pointer):
    env, drv, events = pointer
    env._click_game_points('test', None, [(100, 100)], (1280, 960))
    assert events[-1] == ('move', (1891, 405))
