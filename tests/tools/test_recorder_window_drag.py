"""Window movement for the mouse-driven pad recorder.

The default recorder installs a low-level mouse hook and consumes physical
button presses before Windows/Tk sees them.  A native title-bar drag therefore
cannot work while isolation is active.  The recorder's explicit move handle
translates the guarded pointer stream into window geometry changes.

Run:  pytest tests/tools/test_recorder_window_drag.py
"""

from __future__ import annotations

from scripts.calibration import record_pad_to_yaml as rec


class _Widget:
    def __init__(self, master=None):
        self.master = master


class _Root:
    def __init__(self, x=40, y=40):
        self.x = x
        self.y = y
        self.geometry_calls: list[str] = []

    def winfo_x(self):
        return self.x

    def winfo_y(self):
        return self.y

    def geometry(self, value):
        self.geometry_calls.append(value)


class _DragHarness:
    """Bind CaptureWindow's real drag/dispatch methods without creating Tk."""

    def __init__(self):
        self.root = _Root()
        self._window_drag_handle = _Widget()
        self._window_drag_origin = None
        self._guard_pressed = {}
        self.hit_widget = self._window_drag_handle
        self.cursor_anchor_releases = 0

    def _guard_widget_at(self, _x, _y):
        return self.hit_widget

    @staticmethod
    def _pad_control_owner(_widget):
        return None, None

    def _release_cursor_anchor(self):
        self.cursor_anchor_releases += 1

    _is_window_drag_widget = rec.CaptureWindow._is_window_drag_widget
    _start_window_drag = rec.CaptureWindow._start_window_drag
    _move_window_drag = rec.CaptureWindow._move_window_drag
    _finish_window_drag = rec.CaptureWindow._finish_window_drag
    _dispatch_guard_mouse = rec.CaptureWindow._dispatch_guard_mouse


def test_guarded_drag_moves_the_window_without_emitting_a_pad_action():
    win = _DragHarness()

    win._dispatch_guard_mouse(rec.WM_LBUTTONDOWN, 100, 100)
    win._dispatch_guard_mouse(rec.WM_MOUSEMOVE, 150, 140)
    win._dispatch_guard_mouse(rec.WM_LBUTTONUP, 160, 150)

    assert win.root.geometry_calls == ["+90+80", "+100+90"]
    assert win._window_drag_origin is None
    assert win.cursor_anchor_releases == 1


def test_drag_geometry_supports_monitors_with_negative_coordinates():
    win = _DragHarness()
    win.root = _Root(x=-300, y=50)

    win._start_window_drag(100, 100)
    win._move_window_drag(50, 70)

    assert win.root.geometry_calls == ["-350+20"]


def test_clicking_a_non_handle_widget_does_not_move_the_window():
    win = _DragHarness()
    win.hit_widget = _Widget()

    win._dispatch_guard_mouse(rec.WM_LBUTTONDOWN, 100, 100)
    win._dispatch_guard_mouse(rec.WM_MOUSEMOVE, 150, 140)
    win._dispatch_guard_mouse(rec.WM_LBUTTONUP, 160, 150)

    assert win.root.geometry_calls == []
    assert win._window_drag_origin is None
