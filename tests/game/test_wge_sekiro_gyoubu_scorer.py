from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from coding4game_gui.benchmark.windows.scorers.sekiro_boss import (
    SekiroBossScorer,
)


SIZE = (100, 100)
REAL_SIZE = (1920, 1080)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sekiro_gyoubu"
SCENARIO = (Path(__file__).resolve().parents[2] /
            "coding4game_gui/benchmark/scenarios/sekiro_gyoubu.yaml")
HEALTH = {
    "bar_frac": [0.1, 0.9, 0.80, 0.90],
    "bgr_min": [0, 0, 150],
    "bgr_max": [80, 80, 255],
    "column_fill_ratio": 0.5,
    "smooth_columns": 1,
    "max_prefix_gap": 1,
    "full_fill_threshold": 0.98,
    "confirm_frames": 1,
}
START = {
    "require_boss_health": True,
    "regions": [{
        "frac": [0.0, 0.1, 0.0, 0.1],
        "bgr_min": [0, 150, 0],
        "bgr_max": [80, 255, 80],
        "min_ink_ratio": 0.9,
    }],
}
HUD = {
    "regions": [{
        "frac": [0.1, 0.2, 0.0, 0.1],
        "bgr_min": [0, 150, 0],
        "bgr_max": [80, 255, 80],
        "min_ink_ratio": 0.9,
    }],
}
PHASE_2 = {
    "regions": [{
        "frac": [0.2, 0.3, 0.0, 0.1],
        "bgr_min": [150, 0, 0],
        "bgr_max": [255, 80, 80],
        "min_ink_ratio": 0.9,
    }],
}
VICTORY = {
    "confirm_frames": 2,
    "regions": [{
        "frac": [0.4, 0.5, 0.0, 0.1],
        "bgr_min": [200, 200, 200],
        "bgr_max": [255, 255, 255],
        "min_ink_ratio": 0.9,
    }],
}
DEFEAT = {
    "confirm_frames": 2,
    "regions": [{
        "frac": [0.5, 0.6, 0.0, 0.1],
        "bgr_min": [100, 100, 100],
        "bgr_max": [180, 180, 180],
        "min_ink_ratio": 0.9,
    }],
}
RESURRECTION = {
    "regions": [{
        "frac": [0.6, 0.7, 0.0, 0.1],
        "bgr_min": [0, 150, 0],
        "bgr_max": [80, 255, 80],
        "min_ink_ratio": 0.9,
    }],
}


def _scorer(**overrides) -> SekiroBossScorer:
    spec = {
        "calibrated_client_size": list(SIZE),
        "total_lives": 2,
        "scale": 100,
        "boss_health": dict(HEALTH),
        "start_screen": START,
        "hud_screen": HUD,
        "phase_signatures": [{
            "completed_lives": 1,
            "confirm_frames": 2,
            "signature": PHASE_2,
        }],
        "victory_screen": VICTORY,
        "defeat_screen": DEFEAT,
        "resurrection_screen": RESURRECTION,
    }
    spec.update(overrides)
    return SekiroBossScorer(spec, SIZE)


def _frame(fill: float = 1.0, *, start=False, hud=True, phase=1,
           victory=False, defeat=False, resurrection=False) -> np.ndarray:
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    width = round(80 * fill)
    frame[80:90, 10:10 + width] = [0, 0, 220]
    if start:
        frame[0:10, 0:10] = [0, 220, 0]
    if hud:
        frame[0:10, 10:20] = [0, 220, 0]
    if phase == 2:
        frame[0:10, 20:30] = [220, 0, 0]
    if victory:
        frame[0:10, 40:50] = [240, 240, 240]
    if defeat:
        frame[0:10, 50:60] = [140, 140, 140]
    if resurrection:
        frame[0:10, 60:70] = [0, 220, 0]
    return frame


def test_two_lives_each_contribute_half_with_continuous_health_progress():
    scorer = _scorer()

    scorer.delta(_frame(0.5))
    assert scorer.total == pytest.approx(25)

    second = _frame(1.0, phase=2)
    scorer.delta(second)
    scorer.delta(second.copy())
    assert scorer.completed_lives == 1
    assert scorer.total == pytest.approx(50)

    scorer.delta(_frame(0.5, phase=2))
    assert scorer.total == pytest.approx(75)


def test_health_refill_without_deathblow_signature_never_advances_a_life():
    scorer = _scorer()

    scorer.delta(_frame(0.2))
    earned = scorer.total
    scorer.delta(_frame(1.0))
    scorer.delta(_frame(0.5))

    assert scorer.completed_lives == 0
    assert scorer.total == pytest.approx(earned)


def test_calibrated_full_fill_threshold_starts_at_zero_score():
    scorer = _scorer()

    assert scorer.read_boss_hp(_frame(0.99)) == 1.0
    assert scorer.delta(_frame(0.99)) == 0
    assert scorer.total == 0


def test_resurrection_suppresses_overlapping_defeat_and_victory_is_exact_100():
    scorer = _scorer()
    choice = _frame(defeat=True, resurrection=True)

    assert not scorer.is_terminal(choice)
    assert not scorer.is_terminal(choice.copy())
    assert scorer.classify_phase(choice.copy()) == "resurrection"

    scorer.delta(_frame(0.5))
    victory = _frame(victory=True)
    assert not scorer.is_terminal(victory)
    assert scorer.is_terminal(victory.copy())
    scorer.delta(victory.copy())
    assert scorer.total == pytest.approx(100)


def test_configured_resurrection_terminal_preserves_earned_progress():
    scorer = _scorer(
        terminal_phases=["victory", "defeat", "resurrection"])
    scorer.delta(_frame(0.5))
    assert scorer.total == pytest.approx(25)

    choice = _frame(defeat=True, resurrection=True)
    assert scorer.is_terminal(choice)
    assert scorer.classify_phase(choice.copy()) == "resurrection"
    assert scorer.delta(choice.copy()) == 0
    assert scorer.total == pytest.approx(25)


def test_defeat_requires_multi_frame_confirmation():
    scorer = _scorer()
    defeat = _frame(defeat=True)

    assert not scorer.is_terminal(defeat)
    assert scorer.is_terminal(defeat.copy())


def test_missing_or_uncalibrated_visual_evidence_fails_closed():
    blank = np.zeros((100, 100, 3), dtype=np.uint8)
    scorer = _scorer()

    assert scorer.read_boss_hp(blank) is None
    assert scorer.delta(blank) == 0
    assert scorer.classify_phase(blank) == "non_combat"
    assert not scorer.is_terminal(blank)

    no_hud = _frame(0.1, hud=False)
    assert scorer.read_boss_hp(no_hud) == pytest.approx(0.1)
    assert scorer.delta(no_hud) == 0
    assert scorer.total == 0
    assert scorer.classify_phase(no_hud) == "non_combat"

    provisional = SekiroBossScorer({
        "total_lives": 2,
        "start_screen": {"require_boss_health": True, "regions": []},
        "boss_health": {},
        "victory_screen": {"confirm_frames": 2},
        "defeat_screen": {"confirm_frames": 2},
    }, SIZE)
    assert not provisional.check_start(blank)
    assert provisional.delta(blank) == 0
    assert not provisional.is_terminal(blank)


def test_client_size_is_guarded():
    with pytest.raises(ValueError, match="calibrated"):
        SekiroBossScorer(
            {"calibrated_client_size": [1920, 1080]},
            SIZE,
        )


def _real_frame(*parts):
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    placements = {
        "full_top.png": (100, 40),
        "full_player.png": (620, 960),
        "damaged_top.png": (100, 40),
        "life2_nodes.png": (110, 40),
        "victory_center.png": (650, 200),
        "resurrection_bottom.png": (650, 970),
        "defeat_center.png": (700, 330),
        "resurrection_center.png": (700, 300),
    }
    for name in parts:
        crop = cv2.imread(str(FIXTURES / name))
        assert crop is not None, name
        x, y = placements[name]
        h, w = crop.shape[:2]
        frame[y:y + h, x:x + w] = crop
    return frame


def _real_scorer():
    spec = yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))["scorer"]
    return SekiroBossScorer(spec, REAL_SIZE)


def test_real_hud_fixtures_cover_start_damage_and_second_life():
    scorer = _real_scorer()
    full = _real_frame("full_top.png", "full_player.png")
    damaged = _real_frame("damaged_top.png")

    assert scorer.check_start(full)
    assert scorer.read_boss_hp(full) == pytest.approx(1.0)
    damaged_hp = scorer.read_boss_hp(damaged)
    assert damaged_hp is not None and 0.70 < damaged_hp < 0.85
    scorer.delta(damaged)
    scorer.delta(damaged.copy())
    assert scorer.total > 5

    life_two = _real_frame("full_top.png", "life2_nodes.png")
    scorer.delta(life_two)
    scorer.delta(life_two.copy())
    # Phase confirmation clears the pending vitality sample. A third distinct
    # frame confirms the refilled life-two bar without treating the refill as
    # phase evidence by itself.
    scorer.delta(life_two.copy())
    assert scorer.completed_lives == 1
    assert scorer.total >= 50


def test_real_terminal_and_resurrection_fixtures_remain_distinct():
    scorer = _real_scorer()
    resurrection = _real_frame("resurrection_bottom.png", "resurrection_center.png")
    victory = _real_frame("victory_center.png")

    assert scorer.classify_phase(resurrection) == "resurrection"
    assert scorer.is_terminal(resurrection)
    assert scorer.is_terminal(resurrection.copy())
    assert not scorer.is_terminal(victory)
    assert scorer.is_terminal(victory.copy())
    assert scorer.delta(victory.copy()) == 100

    scorer = _real_scorer()
    defeat = _real_frame("defeat_center.png")
    assert not scorer.is_terminal(defeat)
    assert scorer.is_terminal(defeat.copy())
    assert scorer.classify_phase(defeat.copy()) == "defeat"


def _current_center(name):
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    crop = cv2.imread(str(FIXTURES / name))
    assert crop is not None
    frame[300:740, 700:1200] = crop
    return frame


def test_actual_bright_and_fading_death_confirm_without_dialogue():
    scorer = _real_scorer()
    bright = _current_center("death_current_bright_center.png")
    dim = _current_center("death_current_dim_center.png")
    assert not scorer.is_terminal(bright)
    # Re-inspecting one observation must not satisfy temporal confirmation.
    assert not scorer.is_terminal(bright)
    assert scorer.is_terminal(dim)
    assert scorer.classify_phase(dim) == "defeat"
    assert scorer.total == 0


@pytest.mark.parametrize("brightness", [1.0, 0.7, 0.4])
def test_real_death_shapes_normalize_brightness_and_ignore_dialogue(brightness):
    scorer = _real_scorer()
    frame = _current_center("death_current_bright_center.png")
    frame = (frame.astype(np.float32) * brightness).astype(np.uint8)
    assert not scorer.is_terminal(frame)
    assert scorer.is_terminal(frame.copy())


@pytest.mark.parametrize("missing", ["kanji", "death"])
def test_both_fixed_death_labels_are_required(missing):
    scorer = _real_scorer()
    frame = _current_center("death_current_bright_center.png")
    region = next(r for r in scorer.spec["defeat_screen"]["shape_regions"]
                  if r["name"] == missing)
    scorer._frac_box(frame, region["frac"])[:] = 0
    assert not scorer.is_terminal(frame)
    assert not scorer.is_terminal(frame.copy())


def test_death_confirmation_resets_on_real_combat_and_rejects_blank_noise():
    scorer = _real_scorer()
    death = _current_center("death_current_bright_center.png")
    combat = _current_center("combat_current_center.png")
    assert not scorer.is_terminal(death)
    assert not scorer.is_terminal(combat)
    assert not scorer.is_terminal(death.copy())
    assert not scorer.is_terminal(np.zeros_like(death))
    noise = np.random.default_rng(7).integers(0, 2, death.shape, dtype=np.uint8)
    assert not scorer.is_terminal(noise)
    assert not scorer.is_terminal(noise.copy())


def test_real_resurrection_still_takes_precedence_over_death_shapes():
    scorer = _real_scorer()
    frame = _real_frame("defeat_center.png", "resurrection_bottom.png")
    assert scorer.is_terminal(frame)
    assert scorer.classify_phase(frame) == "resurrection"
    assert scorer.is_terminal(frame.copy())
    assert scorer.classify_phase(frame.copy()) == "resurrection"


def test_native_snow_false_resurrection_requires_actual_death_overlay():
    scorer = _real_scorer()
    frame = _current_center("combat_snow_center.png")
    frame[970:1080, 650:1350] = cv2.imread(str(FIXTURES / "combat_snow_options.png"))
    assert scorer._matches_signature(frame, scorer._resurrection_screen)
    assert not scorer.is_terminal(frame)
    assert not scorer.is_terminal(frame.copy())
    assert scorer.classify_phase(frame) != "resurrection"


@pytest.mark.parametrize("name", ["false_victory_352_center.png", "false_victory_354_center.png"])
def test_real_combat_scenery_never_becomes_victory(name):
    scorer = _real_scorer()
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    frame[200:700, 640:1280] = cv2.imread(str(FIXTURES / name))
    for _ in range(4):
        sample = frame.copy()
        assert not scorer.is_terminal(sample)
        scorer.delta(sample)
        assert scorer.total < 100


@pytest.mark.parametrize("brightness", [1.0, 0.7, 0.4])
def test_victory_shapes_survive_fading_and_keep_two_observations(brightness):
    scorer = _real_scorer()
    frame = _real_frame("victory_center.png")
    frame = np.clip(frame.astype(np.float32) * brightness + 10, 0, 255).astype(np.uint8)
    assert not scorer.is_terminal(frame)
    assert not scorer.is_terminal(frame)
    assert scorer.is_terminal(frame.copy())
    assert scorer.delta(frame.copy()) == 100


@pytest.mark.parametrize("missing", ["kanji", "text"])
def test_victory_requires_both_actual_labels(missing):
    scorer = _real_scorer()
    frame = _real_frame("victory_center.png")
    region = next(r for r in scorer.spec["victory_screen"]["shape_regions"]
                  if r["name"] == missing)
    scorer._frac_box(frame, region["frac"])[:] = 0
    assert not scorer.is_terminal(frame)
    assert not scorer.is_terminal(frame.copy())


def test_victory_confirmation_resets_after_intervening_combat():
    scorer = _real_scorer()
    victory = _real_frame("victory_center.png")
    combat = np.zeros((1080, 1920, 3), dtype=np.uint8)
    combat[200:700, 640:1280] = cv2.imread(str(FIXTURES / "false_victory_354_center.png"))
    assert not scorer.is_terminal(victory)
    assert not scorer.is_terminal(combat)
    assert not scorer.is_terminal(victory.copy())
    assert scorer.is_terminal(victory.copy())
