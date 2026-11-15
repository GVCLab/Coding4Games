"""BBT collectible-HUD reader, against frames from the 2026-09-05 human 1-1 run.

The fixtures are named by what the game itself showed, and the final state is
cross-checked against that run's results screen (3 gems + yarn = A++). That
makes this a test with an ORACLE rather than a test that pins whatever the
code happened to output.

The fixtures are in EPISODE coordinates (1536x810). The older
tests/fixtures/battleblock/ set is 1920x1056 with black padding -- a frame of
reference no episode ever sees -- which is exactly why a scorer calibrated on
it passed its own tests while reading the wrong pixels.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cv2  # noqa: E402
import pytest  # noqa: E402

from coding4game_gui.benchmark.windows.scorers import bbt_hud  # noqa: E402

# tests moved into tests/game/ during the main migration; fixtures stayed
# shared at tests/fixtures/, so this walks up one more level than before.
FIX = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                   "fixtures", "battleblock_11")

# filename -> (gems, yarn) that the GAME was showing in that frame
EXPECTED = {
    "gem0.png": (0, 0),
    "gem1.png": (1, 0),
    "gem2.png": (2, 0),
    "gem2_yarn1.png": (2, 1),
    "gem3_yarn1_exitopen.png": (3, 1),
}


@pytest.mark.parametrize("name,expected", sorted(EXPECTED.items()))
def test_reads_the_collectibles_the_game_showed(name, expected):
    img = cv2.imread(os.path.join(FIX, name))
    assert img is not None, f"missing fixture {name}"
    assert bbt_hud.read(img) == expected


def test_debounce_does_not_clamp_a_real_drop():
    """A drop means a restart or a broken reader; both must stay visible.
    A running maximum would erase the only evidence of either."""
    counts = [0, 1, 2, 2, 2, 0, 0, 0, 0]
    smoothed = bbt_hud.debounce(counts)
    assert bbt_hud.drops(smoothed), "the drop was silently clamped away"


# --------------------------------------------------------------------------
# in-level restart: probed 2026-09-05, and it zeroes the real HUD
# --------------------------------------------------------------------------
def test_a_restart_zeroes_the_collectible_hud():
    """The load-bearing evidence is the value AFTER the curtain lifts, not the
    reading during it -- a curtain merely hides the HUD. Two gems before, zero
    after, so 'collecting is irreversible' is false at episode scope and a
    union over the whole episode could be farmed by restarting."""
    before = cv2.imread(os.path.join(FIX, "restart_before_gem2.png"))
    after = cv2.imread(os.path.join(FIX, "restart_after_gem0.png"))
    assert bbt_hud.read(before) == (2, 0)
    assert bbt_hud.read(after) == (0, 0)


def test_the_curtain_reads_unreadable_and_not_zero():
    """Returning (0, 0) under the curtain manufactured a fake drop on every
    transition -- and a drop is what now ends an episode."""
    curtain = cv2.imread(os.path.join(FIX, "restart_curtain.png"))
    assert bbt_hud.is_curtain(curtain)
    assert bbt_hud.read(curtain) is None


def test_open_play_is_never_mistaken_for_a_curtain():
    for name in EXPECTED:
        img = cv2.imread(os.path.join(FIX, name))
        assert not bbt_hud.is_curtain(img), name


def test_a_drop_next_to_a_curtain_is_a_restart():
    flags = [False] * 20 + [True] * 13 + [False] * 10
    assert bbt_hud.classify_drop(35, flags) == "restart"


def test_a_drop_far_from_any_curtain_is_our_bug_not_a_restart():
    """Reader jitter must NOT end an episode: with 'restart ends the episode'
    as the rule, a false positive silently deletes a valid run. Two of the
    three drops in the 2026-09-05 probe were exactly this."""
    flags = [False] * 20 + [True] * 13 + [False] * 300
    assert bbt_hud.classify_drop(320, flags) == "reader_fault"


def test_debounce_and_drops_survive_unreadable_gaps():
    counts = [1, 2, None, None, None, 2, 2]
    smoothed = bbt_hud.debounce(counts)
    assert smoothed[2] is None and smoothed[3] is None
    assert not bbt_hud.drops(smoothed), "an UNREADABLE gap faked a drop"


def test_debounce_removes_single_frame_jitter():
    counts = [0, 0, 2, 0, 0, 0, 0]
    assert bbt_hud.drops(counts), "precondition: raw series has a drop"
    assert not bbt_hud.drops(bbt_hud.debounce(counts))


# ---------------------------------------------------------------------------
# A SECOND FRAME SIZE. mr-why renders 1280x720 (2880x1800 at DPI 2x cannot
# produce a 1536-wide frame -- it would need a 3072-wide client). Until
# 2026-09-22 read() raised on every frame that machine produced, so the scorer
# could not read a single one. These fixtures come from a human playthrough
# captured through the bench's own renderer, labelled by WHEN they were taken
# rather than by what the matcher said: the first gem was collected at
# t=218.3s, so the 197.4s frame is a true negative from the same level.
# ---------------------------------------------------------------------------
FIX_1280 = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "fixtures", "battleblock_1280")


def test_reads_a_gem_at_the_second_calibrated_size():
    img = cv2.imread(os.path.join(FIX_1280, "gem1_1280.png"))
    assert img is not None
    assert (img.shape[1], img.shape[0]) == (1280, 720)
    gems, _yarn = bbt_hud.read(img)
    assert gems >= 1, "the gem on this frame's HUD was not counted"


def test_counts_nothing_before_the_first_gem_is_collected():
    """The in-level counter is not drawn at zero, so this must read 0 gems.

    This is the frame that matters most for a false positive: same level,
    same session, same scenery, 20 seconds before the pickup.
    """
    img = cv2.imread(os.path.join(FIX_1280, "nogem_1280.png"))
    assert img is not None
    gems, yarn = bbt_hud.read(img)
    assert gems == 0, "counted a gem on a frame recorded before any was taken"
    assert yarn == 0


def test_an_uncalibrated_size_raises_rather_than_guessing():
    """A derived ROI would not raise -- it would report 0 gems forever, which
    reads as 'collected nothing'. x scales between the two known sizes and y
    does not, so there is no formula to derive one from."""
    img = cv2.imread(os.path.join(FIX_1280, "gem1_1280.png"))
    odd = cv2.resize(img, (1600, 900))
    with pytest.raises(ValueError, match="no HUD calibration"):
        bbt_hud.read(odd)


def test_templates_are_cached_per_scale_not_shared():
    """One cache key per (files, scale). A shared key would hand 1536-sized
    templates to a 1280 frame after the first read and quietly match nothing."""
    a = bbt_hud._templates(bbt_hud._GEM_FILES, 1.0)
    b = bbt_hud._templates(bbt_hud._GEM_FILES, 1280 / 1536)
    assert a[0].shape != b[0].shape
    assert bbt_hud._templates(bbt_hud._GEM_FILES, 1.0)[0].shape == a[0].shape


def test_read_defaults_to_the_threshold_for_the_frames_own_size():
    """The old signature defaulted to the module constant, which would apply
    1536x810's threshold to a 1280x720 frame -- the exact mistake the geometry
    table exists to stop."""
    img = cv2.imread(os.path.join(FIX_1280, "gem1_1280.png"))
    assert bbt_hud.read(img) == bbt_hud.read(img, threshold=0.45)
    # An explicit argument still wins, so calibration sweeps keep working.
    assert bbt_hud.read(img, threshold=0.99) == (0, 0)
