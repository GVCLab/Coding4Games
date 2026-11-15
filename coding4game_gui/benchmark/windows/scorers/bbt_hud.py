"""Read BattleBlock Theater's in-level collectible HUD from a frame.

WHAT IT READS, AND WHY IT IS THE SCORE AND NOT A PROXY. BBT gates the level
exit on GEMS: collect the level's quota and the exit opens ("EXIT IS OPEN"
banner). So "gems collected / gems required" is the game's OWN definition of
progress toward the goal, not something we invented and have to defend. The
yarn ball is a bonus -- it affects the end-of-level grade and NOT the exit,
measured on the 2026-09-05 human run of 1-1: the yarn was collected at tic
324 and nothing opened; the third gem landed at tic 450 and the exit opened
immediately.

WHERE IT LOOKS. The collectibles hang from strings near the top-centre as
theatre props, and they are SCREEN-FIXED: across 12 consecutive frames whose
camera dx swung from -19.6 to +19.7 px, the icons held the same screen x while
the background scrolled. The ROI was not eyeballed -- it was found by
motion-compensated differencing (a screen-fixed overlay is the region that
matches WITHOUT warping while the background matches only AFTER warping),
which returned x=806 w=181. ROI_X is widened to 740..1030 because gems
accumulate LEFTWARD from x~896 and a third gem sits near x~780.

NOT TO BE CONFUSED WITH THE TOP-LEFT COUNTER. There is a second collectible
display in the top-left corner showing icons and NUMBERS (e.g. 48 gems, 5
yarn). That one is the save-wide holding, not this level's count -- the yarn
settles it, since a level holds one yarn ball and it read 5 on entry. It is
not what this module reads, and mistaking it for the in-level HUD on
2026-09-22 produced a confident, wrong "the ROI is in the wrong place"
diagnosis. The in-level counter IS NOT DRAWN AT ZERO, so a probe that never
collects anything sees an empty ROI and an empty top-centre, which looks
exactly like a mis-placed ROI.

WHY TEMPLATES AND NOT A COLOUR THRESHOLD. A colour threshold was tried first
and failed: the level is full of green foliage and gold-brown blocks, and at
the moment the exit opens the WHOLE SCREEN goes green with green sparks. It
reported up to 14 gems and 57 yarn balls. Matching the icon's shape survives
that frame; a hue range never will.

MONOTONICITY HOLDS ONLY WITHIN ONE ATTEMPT, and that is measured, not assumed.
The pause menu can restart the level, and a restart ZEROES the real HUD:
probed on 2026-09-05, the count ran 0->1->2, a red curtain covered frames
403-415, and the count was 0 again AFTER the curtain lifted and climbed from
scratch. The load-bearing evidence is the post-curtain value, not the drop
during it -- during a curtain the HUD is simply hidden.

So the "collecting is irreversible" axiom is FALSE at episode scope, and an
episode ledger that took a union or a maximum over the whole episode could be
farmed: collect gem A, restart, collect gem B, restart, walk to the exit. We
score the FIRST attempt only -- a restart ENDS the episode (see
`is_curtain`). That is a benchmark rule, not a reader detail.

THE READER JITTERS, SO A DROP ALONE IS NOT A RESTART. In the same 630-frame
probe, three drops survived debouncing and only ONE was the restart; the other
two had no curtain anywhere near them. Since a restart now ends an episode, a
false positive would kill a valid run, so the two causes get different
responses: a drop DURING/just after a curtain is a restart, and a drop in open
play is a READER FAULT that should invalidate the episode rather than end it.

`debounce` only removes single-frame jitter and deliberately does not clamp
with a running maximum, which would erase the evidence of both.

CALIBRATION STATUS. Thresholds were fitted on ONE run of ONE level and
validated against that level's own results screen (3 gems + yarn = A++), which
is a real oracle but a single one. Only THR=0.55 satisfied "no drops AND
passes through 0/1/2/3 AND agrees with the oracle" -- a narrow window. Treat
a second recording as required before trusting this on a scored run.
"""
from __future__ import annotations

import os

import cv2
import numpy as np

# Screen-fixed HUD region, in EPISODE coordinates (1536x810 -- what the bench
# renderer delivers after the DPI crop, NOT the padded 1920x1056 buffer that
# an ad-hoc screenshot script returns).
ROI_X = (740, 1030)
ROI_Y = (20, 120)
CALIBRATED_FRAME_SIZE = (1536, 810)

THRESHOLD = 0.55          # see CALIBRATION STATUS above
MIN_SEPARATION = 32       # px between two accepted icon centres
MAX_ICONS = 8             # a sanity cap; a real level never shows this many
YARN_MIN_GOLD_FRACTION = 0.25  # template gold pixels supported by the candidate

# ONE ENTRY PER FRAME SIZE WE HAVE ACTUALLY MEASURED ON. A size that is not in
# here raises; nothing is derived for an unseen size, because the second entry
# below proves that deriving would be wrong.
#
# 1280x720 (mr-why: 2880x1800 at DPI 2x; the calibrated 1536-wide frame is
# unreachable there, it would need a 3072-wide client against a 2880-wide
# screen). Measured 2026-09-22 on a human playthrough captured through the
# bench's own renderer -- the gem icon's box, found by template match:
#
#     fixtures @1536x810   icon x 801..936   y 32..103
#     ours     @1280x720   icon x 717..775   y 11..65
#
# X SCALES AND Y DOES NOT, and that asymmetry is the whole reason this is a
# table and not a formula. x maps by 1280/1536 = 0.8333 exactly (801 -> 667,
# 936 -> 780, both inside the scaled ROI). y does not: the same factor sends
# 32..103 to 27..86, while the icon actually sits at 11..65 -- about 21 px
# HIGHER than proportional scaling predicts. The cause is not established
# (letterbox arithmetic does not account for it in either direction), so the
# measured box is stored and the theory is not. A formula fitted to one of
# these sizes would silently mis-place the ROI on the other, and a mis-placed
# ROI does not raise -- it reports 0 gems forever, which reads as "collected
# nothing".
#
# SEPARABILITY AT 1280x720, on frames labelled by TIME rather than by the
# matcher (the first gem was picked up at t=218.3s, so everything before it is
# a true negative from the same level and session):
#
#     before any gem   n=4    max 0.147
#     gem on the HUD   n=25   min 0.538   median 0.772   max 0.823
#
# THE THRESHOLD IS PER SIZE TOO, and 0.55 is wrong at 1280x720. Measured on a
# 20-minute human session (2026-09-22, 1990 frames, keystrokes logged
# separately so "was a person playing" is a fact rather than an inference):
#
#   ROI score, TRUE NEGATIVES -- 1394 frames before the person touched a key,
#   level idle, nothing collected:
#       median 0.151   p99 0.156   MAX 0.211
#   ROI score while gems were on the HUD:
#       0.47 .. 0.82, median 0.508
#
# So the two classes are separated by an empty band from 0.211 to 0.47, and
# 0.55 was drawn INSIDE the positive cluster. The icons hang on strings and
# sway while the level scrolls behind them, and TM_CCOEFF_NORMED counts that
# moving background, so a held gem crosses 0.55 only about a fifth of the
# time. What that looks like downstream is a reader that reports 0 gems for a
# player who is carrying two.
#
# What read() returns across the session, per threshold:
#
#   0.55   (0,0) x167, (2,0) x6      misses nearly everything
#   0.45   (0,0) x62,  (2,0) x109    sees the two gems that were held
#   0.35   (2,1) x9, (3,0) x5        counts start inflating
#   0.30   (4,0), (2,3)              clearly degraded
#
# Negatives stayed perfectly clean (369/369 read (0,0)) at every one of those,
# so the cost of going lower is over-counting, not false alarms. 0.45 takes
# the detections and stops before the inflation.
#
# 1536x810 KEEPS 0.55. It was fitted there against that level's own results
# screen ("3 gems + yarn = A++"), which is a real oracle, and nothing measured
# here is evidence about that size.
_GEOMETRY = {
    # (frame w, h): (roi_x, roi_y, template_scale, threshold)
    (1536, 810): (ROI_X, ROI_Y, 1.0, THRESHOLD),
    # The 2026-10-25 human capture places complete icon templates below y=80.
    # This measured union retains the earlier native captures and reaches the
    # current hanging icons. See tests/fixtures/battleblock_20261025/provenance.json.
    (1280, 720): ((616, 858), (0, 120), 1280 / 1536, 0.45),
}


def geometry(frame_size: tuple[int, int]):
    """(roi_x, roi_y, template_scale, threshold) for a size, or raise.

    Raising is the point. A frame size we have not calibrated on is a size
    whose ROI we do not know, and guessing one produces confident zeros.
    """
    key = (int(frame_size[0]), int(frame_size[1]))
    if key not in _GEOMETRY:
        known = ", ".join(f"{w}x{h}" for w, h in sorted(_GEOMETRY))
        raise ValueError(
            f"no HUD calibration for a {key[0]}x{key[1]} frame; calibrated "
            f"sizes are {known}. Capture the HUD at this size and add an "
            f"entry -- neither the ROI nor the threshold can be derived, see "
            f"_GEOMETRY.")
    return _GEOMETRY[key]

# The theatre curtain covers the screen on a level transition and on a
# restart. Measured peaks: 0.82 mid-curtain, 0.03-0.11 in open play, so the
# gap is an order of magnitude and 0.35 sits in the middle of it.
CURTAIN_RED_RATIO = 0.35
# A curtain is DARK red; the end-of-level RESULTS SCREEN is BRIGHT red. Red
# alone conflates them -- see is_curtain for the measurement. Curtains topped
# out at 69.7 luma and the results screen sat at 115.7, so 85/90 leaves room on
# both sides without touching either.
CURTAIN_MAX_LUMA = 85.0
RESULTS_MIN_RED = 0.30      # in-level play peaked at 0.118 in a live probe
RESULTS_MIN_LUMA = 90.0

_TPL_DIR = os.path.join(os.path.dirname(__file__), "templates", "battleblock")
# Three gem templates, not one: the first gem sits in a different slot and over
# a different background (clouds) than the second (water). A single template
# cut from the second slot silently never reported the count "1".
_GEM_FILES = ("tpl_gem0.png", "tpl_gem1.png", "tpl_gem2.png")
_YARN_FILES = ("tpl_yarn.png",)

_cache: dict[tuple, list] = {}


def _templates(files, scale: float = 1.0) -> list:
    """The icon templates, resized once per scale and cached.

    INTER_AREA on the way down: these are flat-colour sprites with hard edges,
    and a bilinear shrink softens exactly the edges the match depends on.
    """
    key = (",".join(files), round(float(scale), 6))
    if key not in _cache:
        out = []
        for name in files:
            img = cv2.imread(os.path.join(_TPL_DIR, name))
            if img is None:
                raise FileNotFoundError(f"missing HUD template {name}")
            if scale != 1.0:
                w = max(1, int(round(img.shape[1] * scale)))
                h = max(1, int(round(img.shape[0] * scale)))
                img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
            out.append(img)
        _cache[key] = out
    return _cache[key]


def _count(frame: np.ndarray, tpls: list, threshold: float, geom) -> int:
    roi_x, roi_y, scale, _thr = geom
    roi = frame[roi_y[0]:roi_y[1], roi_x[0]:roi_x[1]]
    best = None
    for t in tpls:
        if roi.shape[0] < t.shape[0] or roi.shape[1] < t.shape[1]:
            continue
        res = cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED)
        if best is None:
            best = res
        else:
            h = min(best.shape[0], res.shape[0])
            w = min(best.shape[1], res.shape[1])
            best = np.maximum(best[:h, :w], res[:h, :w])
    if best is None:
        return 0
    # The suppression radius is a distance between ICONS, so it scales with
    # them. Leaving it at 32 on a shrunken frame would suppress a real second
    # gem along with the first and cap the count at one.
    sep = max(1, int(round(MIN_SEPARATION * scale)))
    work = best.copy()
    n = 0
    while n < MAX_ICONS:
        _, mx, _, loc = cv2.minMaxLoc(work)
        if mx < threshold:
            break
        n += 1
        work[max(0, loc[1] - sep):loc[1] + sep,
             max(0, loc[0] - sep):loc[0] + sep] = -1.0
    return n


def _count_collectibles(frame, gem_templates, yarn_templates, threshold, geom):
    """Let template classes compete for each icon before shared suppression.

    Independent counters can count a green gem again as yarn: the weaker yarn
    correlation comes from its shared outline/background, not a second icon.
    The human three-gem/no-yarn results screen supplies the negative oracle.
    """
    roi_x, roi_y, scale, _ = geom
    roi = frame[roi_y[0]:roi_y[1], roi_x[0]:roi_x[1]]
    best = classes = None
    for kind, templates in enumerate((gem_templates, yarn_templates)):
        for template in templates:
            if (roi.shape[0] < template.shape[0]
                    or roi.shape[1] < template.shape[1]):
                continue
            response = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
            if best is None:
                best = response.copy()
                classes = np.full(response.shape, kind, dtype=np.uint8)
            else:
                height = min(best.shape[0], response.shape[0])
                width = min(best.shape[1], response.shape[1])
                best = best[:height, :width]
                classes = classes[:height, :width]
                response = response[:height, :width]
                stronger = response > best
                classes[stronger] = kind
                best = np.maximum(best, response)
    counts = [0, 0]
    if best is None:
        return tuple(counts)
    separation = max(1, int(round(MIN_SEPARATION * scale)))
    for _ in range(MAX_ICONS):
        _, maximum, _, (x, y) = cv2.minMaxLoc(best)
        if maximum < threshold:
            break
        kind = int(classes[y, x])
        if kind == 0 or any(_yarn_color_supported(roi, x, y, template)
                            for template in yarn_templates):
            counts[kind] += 1
        best[max(0, y - separation):y + separation,
             max(0, x - separation):x + separation] = -1.0
    return tuple(counts)


def _gold_pixels(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return ((hsv[:, :, 0] >= 8) & (hsv[:, :, 0] <= 40)
            & (hsv[:, :, 1] >= 80) & (hsv[:, :, 2] >= 80))


def _yarn_color_supported(roi, x, y, template):
    """Reject neutral cloud edges that correlate with a yarn silhouette.

    In the full human recording, one gray cloud matched yarn at 0.486 but had
    no gold pixels; the labeled positive fixture supports every template-gold
    pixel. This is a color check on a shape proposal, not a color-only counter.
    """
    height, width = template.shape[:2]
    patch = roi[y:y + height, x:x + width]
    if patch.shape != template.shape:
        return False
    mask = _gold_pixels(template)
    return (bool(mask.any())
            and float(_gold_pixels(patch)[mask].mean()) >= YARN_MIN_GOLD_FRACTION)


def red_ratio(frame: np.ndarray) -> float:
    """Fraction of the frame that is curtain-red."""
    s = cv2.resize(frame, (240, 135))
    b = s[:, :, 0].astype(np.int16)
    g = s[:, :, 1].astype(np.int16)
    r = s[:, :, 2].astype(np.int16)
    return float(((r > 90) & (r > g + 40) & (r > b + 40)).mean())


def mean_luma(frame: np.ndarray) -> float:
    """Average brightness. Cheap, and it is what separates the two red screens."""
    return float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean())


def is_curtain(frame: np.ndarray) -> bool:
    """True while the theatre curtain covers the screen (transition/restart).

    RED ALONE IS NOT ENOUGH, and the frame it confused is the one that matters
    most: the end-of-level RESULTS SCREEN is also mostly red, so a COMPLETED
    level was being reported as a transition, and `read` returned UNREADABLE on
    the single frame that says the episode succeeded.

    Measured 2026-09-05 -- curtains harvested from the cold-path timeline
    (n=20, episode coordinates), results screen from the fixture (n=1):

        curtain   red 0.406-0.825   mean_luma 46.3-69.7
        results   red 0.3825        mean_luma 115.7

    Brightness separates them by 46 units; red separates them by 0.024, which
    is why raising CURTAIN_RED_RATIO would have been the wrong fix. A curtain
    is DARK red.
    """
    return (red_ratio(frame) > CURTAIN_RED_RATIO
            and mean_luma(frame) < CURTAIN_MAX_LUMA)


def is_results(frame: np.ndarray) -> bool:
    """True on the end-of-level results screen -- i.e. the level was COMPLETED.

    This is the only completion signal there is. In-level frame matching was
    disproven for this game (five descriptor families collapse to AUC
    0.79-0.86 on hard negatives), so completion cannot be recognised from the
    level; it has to be recognised from the UI that replaces it.

    NOT A COLLECTIBLE ORACLE. Measured: counting the same ROI on the results
    screen yields (0, 0), because its layout is not the in-level HUD's. Reading
    the official grade off it would need its own calibrated regions -- worth
    doing, since that grade is an oracle nobody else in this space has -- but
    it is not this function, and assuming otherwise is how this file already
    had one wrong claim in it.

    CALIBRATED ON 1-1, ON ONE FRAME. The brightness threshold is a property of
    this level's palette showing through, so another level's results screen
    could sit lower. A second sample is the first thing this needs.
    """
    return (red_ratio(frame) >= RESULTS_MIN_RED
            and mean_luma(frame) >= RESULTS_MIN_LUMA)


def read(frame: np.ndarray,
         threshold: float | None = None) -> tuple[int, int] | None:
    """(gems, yarn) on the collectible HUD, or None when UNREADABLE.

    None -- not (0, 0) -- while the curtain hides the HUD. Returning 0 there
    was the first version's behaviour and it manufactured a fake drop on every
    transition, which is exactly the event we now use to end an episode.
    "I cannot see" and "there is nothing there" must not share an encoding.

    frame: HWC BGR uint8 in episode coordinates. Raises on a frame whose size
    has no measured calibration -- a silently mis-sized frame would move the
    ROI off the icons and report 0 forever, which reads as "collected
    nothing". See `geometry` and `_GEOMETRY`: more than one size is supported
    now, but only sizes someone has actually captured the HUD at.
    """
    h, w = frame.shape[:2]
    geom = geometry((w, h))
    if is_curtain(frame) or is_results(frame):
        return None
    _roi_x, _roi_y, scale, calibrated_threshold = geom
    # Default to the threshold measured AT THIS SIZE. A module-level default
    # would have quietly applied 1536x810's 0.55 to a 1280x720 frame, which is
    # the bug this table exists to prevent -- an explicit argument still wins,
    # so the calibration scripts can sweep.
    if threshold is None:
        threshold = calibrated_threshold
    return _count_collectibles(frame, _templates(_GEM_FILES, scale),
                               _templates(_YARN_FILES, scale), threshold, geom)


def debounce(counts: list[int], window: int = 5) -> list[int]:
    """Median filter, so one bad frame cannot decide an episode.

    Deliberately NOT a running maximum. The residual drops after debouncing
    are the signal that either the reader is wrong or the level was restarted;
    clamping them away would destroy the only evidence of both.
    """
    out = []
    half = window // 2
    for i in range(len(counts)):
        if counts[i] is None:
            out.append(None)          # UNREADABLE stays unreadable
            continue
        lo = max(0, i - half)
        hi = min(len(counts), i + half + 1)
        near = [c for c in counts[lo:hi] if c is not None]
        out.append(int(np.median(near)) if near else None)
    return out


def drops(counts: list[int]) -> list[tuple[int, int, int]]:
    """(index, before, after) for every decrease, ignoring UNREADABLE gaps.

    Must be empty inside one attempt: collecting is irreversible. A decrease
    means a restart or a broken reader, and the two get different responses --
    see `classify_drop`."""
    out = []
    prev_i = None
    for i, c in enumerate(counts):
        if c is None:
            continue
        if prev_i is not None and c < counts[prev_i]:
            out.append((i, counts[prev_i], c))
        prev_i = i
    return out


# A restart's curtain lasted 13 frames and the count was already back to zero
# a few frames after it lifted, so the drop is credited to a curtain seen
# within this many frames before it. Wide enough for that, far too narrow to
# reach the reader jitter measured 200+ frames away from any curtain.
RESTART_CURTAIN_LOOKBACK = 25


def classify_drop(drop_index: int, curtain_flags: list[bool]) -> str:
    """'restart' or 'reader_fault' for a decrease in the collectible count.

    The distinction is load-bearing because the two demand OPPOSITE responses.
    A restart is the agent discarding its progress, and by benchmark rule it
    ENDS the episode. A reader fault is OUR bug, and ending the episode on it
    would silently delete a valid run -- it has to invalidate the episode
    instead, so it shows up as a scorer problem rather than as a low score.

    Measured on the 2026-09-05 probe: three drops survived debouncing, one sat
    inside the restart curtain and two were 200+ frames from any curtain.
    """
    lo = max(0, drop_index - RESTART_CURTAIN_LOOKBACK)
    window = curtain_flags[lo:drop_index + 1]
    return "restart" if any(window) else "reader_fault"
