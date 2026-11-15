"""Exercise the recorder's guarded mouse queue and Tk worker lifecycle."""

from __future__ import annotations

import os
import functools
import queue
import subprocess
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.calibration.record_pad_to_yaml import CaptureWindow
from scripts.calibration.record_pad_to_yaml import RecorderWorker
from scripts.calibration.record_pad_to_yaml import WM_LBUTTONDOWN
from scripts.calibration.record_pad_to_yaml import WM_LBUTTONUP
from scripts.calibration.record_pad_to_yaml import WM_RBUTTONDOWN


def _fresh_recorder_process(test):
    """Match the TUI's one recorder command per interpreter lifecycle.

    Conda Tcl can fail loading its library after several destroyed Tk roots
    in one interpreter. Each probe still runs its real UI/worker assertions;
    a child failure or timeout fails the parent test without retrying.
    """
    @functools.wraps(test)
    def run():
        if os.environ.get("C4G_RECORDER_PROBE_CHILD") == "1":
            return test()
        child = subprocess.run(
            [sys.executable, "-c",
             "import runpy,sys; runpy.run_path(sys.argv[1])[sys.argv[2]]()",
             __file__, test.__name__],
            env=dict(os.environ, C4G_RECORDER_PROBE_CHILD="1"),
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=25,
        )
        assert child.returncode == 0, child.stdout + child.stderr
    return run


def _new_window(delivered=None):
    """Create a fresh window + worker + driver triplet for a test."""
    q: queue.Queue = queue.Queue()
    win = CaptureWindow(q)
    def apply_state(index, **state):
        if delivered is not None:
            delivered.append((index, state))
    driver = SimpleNamespace(apply_state=apply_state)
    worker = RecorderWorker(q, driver, win)
    worker.start()
    win.root.update_idletasks()
    win.root.update()
    return q, win, worker


def _pump(win, seconds: float, until=None):
    """Pump the Tk main loop for `seconds`, or until `until()` returns True."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        win.root.update_idletasks()
        win.root.update()
        if until is not None and until():
            return
        time.sleep(0.005)


def _teardown(win, worker, q):
    try:
        q.put({'type': 'system', 't': time.monotonic(), 'token': 'shutdown'})
        worker.join(timeout=2.0)
    finally:
        try:
            win.root.destroy()
        except Exception:
            pass
        # Force GC between tests so the previous Tk interpreter is fully
        # released before the next test creates a new one (multiple
        # Tk() instances in one process leak otherwise).
        import gc
        gc.collect()
        time.sleep(0.1)


def _send_to_control(win, widget, *messages):
    x = widget.winfo_rootx() + widget.winfo_width() // 2
    y = widget.winfo_rooty() + widget.winfo_height() // 2
    for message in messages:
        win.queue_guarded_mouse(message, x, y)


@_fresh_recorder_process
def test_right_click_toggle_persists_until_clicked_again():
    q, win, worker = _new_window()
    lb = win.pad1._widgets['LB']
    a = win.pad1._widgets['A']
    _send_to_control(win, lb, WM_RBUTTONDOWN)
    win.root.after(100, lambda: _send_to_control(win, a, WM_LBUTTONDOWN))
    win.root.after(300, lambda: _send_to_control(win, a, WM_LBUTTONUP))
    win.root.after(450, lambda: _send_to_control(win, lb, WM_RBUTTONDOWN))
    try:
        _pump(win, 1.5, until=lambda: len(worker.tracker.steps) == 3)
        steps = worker.tracker.steps
        assert [s.buttons for s in steps] == [['LB'], ['A', 'LB'], ['LB']]
        assert 0.15 <= steps[1].duration_seconds <= 0.5
        assert not worker.tracker.pads[1].buttons
    finally:
        _teardown(win, worker, q)


@_fresh_recorder_process
def test_release_all_drops_toggle_held():
    """Toggle a button on, then RELEASE ALL — visual state and tracker both go neutral."""
    q, win, worker = _new_window()
    lb = win.pad1._widgets['LB']
    try:
        _send_to_control(win, lb, WM_RBUTTONDOWN)
        state = worker.tracker.pads[1]
        _pump(win, 0.8, until=lambda: state.buttons == {'LB'})
        assert state.buttons == {'LB'}
        win._enqueue_ctrl('release_all')
        _pump(win, 0.8, until=lambda: not state.buttons)
        assert not state.buttons and not win.pad1._toggled['LB']
    finally:
        _teardown(win, worker, q)


@_fresh_recorder_process
def test_cancel_via_button_stops_worker():
    q, win, worker = _new_window()
    try:
        win._enqueue_ctrl("cancel")
        _pump(win, 1.0, until=lambda: not worker.is_alive())
        assert worker.stop_reason == 'cancel'
        assert not worker.is_alive()
    finally:
        _teardown(win, worker, q)


@_fresh_recorder_process
def test_guarded_cursor_records_analog_and_dpad_and_saves():
    delivered = []
    q, win, worker = _new_window(delivered)
    stick = win.pad1.left_stick
    stick_x = stick.winfo_rootx() + stick.winfo_width() // 2
    stick_y = stick.winfo_rooty() + 5
    try:
        win.queue_guarded_mouse(WM_LBUTTONDOWN, stick_x, stick_y)
        win.queue_guarded_mouse(WM_LBUTTONUP, stick_x, stick_y)
        _pump(win, 0.35, until=lambda: len(worker.tracker.steps) == 1)
        _send_to_control(win, win.pad1._widgets['DPAD_UP'], WM_LBUTTONDOWN, WM_LBUTTONUP)
        _pump(win, 0.35, until=lambda: any(s.buttons for s in worker.tracker.steps))
        _pump(win, 0.35, until=lambda: win.label.cget('text').startswith('recording'))
        assert win.label.cget('text').startswith('recording')
        win._enqueue_ctrl('stop_save')
        _pump(win, 1.0, until=lambda: not worker.is_alive())
        assert worker.stop_reason == 'save'
        assert not worker.is_alive()
        inputs = [(s.buttons, s.left_stick) for s in worker.tracker.steps
                  if s.buttons or s.left_stick != (0.0, 0.0)]
        assert inputs == [([], (0.0, 1.0)), (['DPAD_UP'], (0.0, 0.0))]
        assert all(index == 0 for index, _ in delivered)
        assert [(state['buttons'], state['left_stick']) for _, state in delivered] == [
            ([], (0.0, 1.0)), ([], (0.0, 0.0)),
            (['DPAD_UP'], (0.0, 0.0)), ([], (0.0, 0.0)),
        ]
    finally:
        _teardown(win, worker, q)
