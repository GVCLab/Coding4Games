"""Frames are conformed to the calibrated geometry before anything reads them.

Calibrated scorer rects and click coordinates live in the game's RENDER size,
which nobody sets directly: a DPI-unaware game in fullscreen renders at
monitor_resolution / dpi_scale. The reference box is 1920x1080 @125% => 1536x864,
so a second machine matched only if BOTH its resolution and its scaling did.

Uniform scaling removes that coupling for same-aspect displays. A different
ASPECT ratio genuinely cannot be rescaled — the game re-lays out its UI — and
must fail loudly rather than misclick.

These run anywhere: pure array maths, no game.
"""
from __future__ import annotations

import numpy as np
import pytest

from coding4game_gui.benchmark.windows.env import _calibrated_size
from coding4game_gui.benchmark.windows.env import _conform

CAL = (1536, 864)          # balatro's calibration, 16:9


def _frame(w, h):
    return np.zeros((h, w, 3), dtype=np.uint8)


def test_matching_size_is_passed_through_untouched():
    f = _frame(*CAL)
    assert _conform(f, CAL) is f, "a no-op resize must not copy"


def test_same_aspect_is_rescaled():
    """1920x1080 and 2560x1440 are the same 16:9 shape at other scalings."""
    for w, h in ((1920, 1080), (2560, 1440), (1280, 720)):
        out = _conform(_frame(w, h), CAL)
        assert out.shape[:2] == (CAL[1], CAL[0]), f"{w}x{h} -> {out.shape}"


def test_different_aspect_raises():
    """2560x1600 is 16:10 — a real mismatch, not a scaling difference."""
    with pytest.raises(ValueError, match="ASPECT RATIO"):
        _conform(_frame(2560, 1600), CAL)


def test_no_calibration_means_no_conforming():
    f = _frame(800, 600)
    assert _conform(f, None) is f


def test_calibrated_size_reads_the_scenario():
    spec = {"scorer": {"calibrated_client_size": [1536, 864]}}
    assert _calibrated_size(spec) == (1536, 864)
    assert _calibrated_size({}) is None
    assert _calibrated_size({"scorer": {}}) is None
