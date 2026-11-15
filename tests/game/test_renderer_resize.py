"""The capture geometry must follow the window, not a snapshot of it.

Geometry was computed once at init on the assumption that the window never
changes. Declaring a scenario's own window size breaks that: the game is
relaunched windowed at a new size while the renderer keeps sizing its bitmap
to the old fullscreen dimensions, so the frame comes back the RIGHT SIZE BY
COINCIDENCE with the game in its top-left corner and black padding elsewhere.
_conform then sees a frame that already matches the calibrated size, does
nothing, and every scorer rect is silently pointed at the wrong pixels.
"""
from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def renderer(monkeypatch):
    state = {"rect": (0, 0, 1536, 864), "client": (0, 0, 1536, 864),
             "origin": (0, 0), "computes": 0}
    fake_gui = types.SimpleNamespace(
        IsWindow=lambda h: True,
        GetWindowRect=lambda h: state["rect"],
        GetClientRect=lambda h: state["client"],
        ClientToScreen=lambda h, pt: state["origin"],
        GetWindowText=lambda h: "Balatro",
        EnumWindows=lambda cb, x: None,
        IsWindowVisible=lambda h: True,
    )
    monkeypatch.setitem(sys.modules, "win32gui", fake_gui)
    monkeypatch.setitem(sys.modules, "win32ui", types.SimpleNamespace())
    from coding4game_gui.benchmark.windows.drivers.windows_xinput import renderer as rmod

    state["scale"] = 1.0
    fake_user32 = types.SimpleNamespace(
        GetWindowDpiAwarenessContext=lambda h: 0,
        GetAwarenessFromDpiAwarenessContext=lambda c: 0,   # UNAWARE game
        GetDpiForSystem=lambda: int(96 * state["scale"]),
        PrintWindow=lambda *a: 1)
    monkeypatch.setattr(rmod.windll, "user32", fake_user32, raising=False)

    r = rmod.PrintWindowRenderer.__new__(rmod.PrintWindowRenderer)
    r._win32gui = fake_gui
    r.hwnd = 1
    r.pid = None
    real_compute = rmod.PrintWindowRenderer._compute_geometry

    def counting_compute(self):
        state["computes"] += 1
        real_compute(self)

    monkeypatch.setattr(rmod.PrintWindowRenderer, "_compute_geometry",
                        counting_compute)
    r._compute_geometry()
    monkeypatch.setattr(rmod.PrintWindowRenderer, "_grab",
                        lambda self: "frame")
    return r, state


def test_fullscreen_start_has_no_crop(renderer):
    r, _ = renderer
    assert (r.width, r.height) == (1536, 864)
    assert r._crop is None, "client == window means nothing to crop"


def test_geometry_follows_a_resize(renderer):
    r, state = renderer
    # the scenario forced Windowed 1280x720; Balatro settled at 1228x691
    state["rect"] = (146, 55, 1390, 785)     # window 1244x730
    state["client"] = (0, 0, 1228, 691)
    state["origin"] = (154, 86)
    r.screenshot()
    assert (r.width, r.height) == (1244, 730), "bitmap must follow the window"
    assert r._crop == (8, 31, 1236, 722), \
        "must crop the title bar and borders away, not pad the frame"
    assert state["computes"] == 2, "geometry recomputed exactly once"


def test_no_recompute_while_the_window_is_stable(renderer):
    r, state = renderer
    r.screenshot()
    r.screenshot()
    assert state["computes"] == 1, "a stable window must not re-measure"


def test_a_stale_crop_is_detected_even_at_the_same_window_size(renderer):
    """The invariant is frame == CLIENT size, not "the window rect changed".

    A renderer built while the game was briefly fullscreen keeps a crop that
    emits 1535x863 (1920x1080 DPI-divided by 1.25) for a 1228x691 client —
    the frame is then the calibrated size by coincidence, _conform does
    nothing, and every scorer rect reads the wrong pixels.
    """
    r, state = renderer
    r._crop = (0, 0, 1535, 863)          # stale crop from a fullscreen boot
    r.width, r.height = 1244, 730
    state["rect"] = (146, 55, 1390, 785)
    state["client"] = (0, 0, 1228, 691)
    state["origin"] = (154, 86)
    r.screenshot()
    assert r._crop == (8, 31, 1236, 722), "a stale crop must be recomputed"


def test_client_and_dpi_corrections_compose(renderer):
    """Windowed + a DPI-unaware game needs BOTH crops, not whichever runs first.

    The client area sits at an offset inside the window AND the captured
    surface is the game's UNSTRETCHED logical size. Applying only the client
    crop leaves black padding; the frame then comes back at the calibrated
    size by coincidence, _conform does nothing, and every scorer rect reads
    the wrong pixels. Measured: the same window read as 1228x691 by a plain
    probe and 1535x863 by the DPI-aware bench — exactly the scale factor.
    """
    r, state = renderer
    state["scale"] = 1.25
    state["rect"] = (0, 0, 1551, 894)      # window, physical px
    state["client"] = (0, 0, 1535, 863)    # client, physical px
    state["origin"] = (8, 31)
    r.screenshot()
    # 1535/1.25 = 1228, 863/1.25 = 690 — the logical surface the game drew
    assert r._crop == (8, 31, 8 + 1228, 31 + 690), \
        f"both corrections must apply, got {r._crop}"


def test_no_dpi_correction_when_we_see_logical_pixels(renderer):
    """A DPI-unaware READER already gets virtualised sizes; correcting twice
    would crop away real game pixels."""
    r, state = renderer
    state["scale"] = 1.0                   # GetDpiForSystem -> 96 for us
    state["rect"] = (146, 55, 1390, 785)
    state["client"] = (0, 0, 1228, 691)
    state["origin"] = (154, 86)
    r.screenshot()
    assert r._crop == (8, 31, 8 + 1228, 31 + 691)


def test_a_stable_window_is_not_re_measured_under_dpi_scaling(renderer):
    """The emitted frame is in LOGICAL pixels and the client rect in physical
    ones, so comparing those two recomputes the geometry on every frame — the
    first version of this check did exactly that."""
    r, state = renderer
    state["scale"] = 1.25
    state["rect"] = (0, 0, 1551, 894)
    state["client"] = (0, 0, 1535, 863)
    state["origin"] = (8, 31)
    r.screenshot()
    before = state["computes"]
    r.screenshot()
    r.screenshot()
    assert state["computes"] == before, "a stable window must not re-measure"
