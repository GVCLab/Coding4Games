from pathlib import Path

import cv2
import numpy as np
import pytest

from coding4game_gui.benchmark.windows.scorers.black_myth import BlackMythScorer


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "black_myth"
CLIENT_SIZE = (1280, 720)
CROP_X0, CROP_Y0 = 440, 595

VICTORY_SCREEN = {
    "min_luma": 140,
    "neutral_tolerance": 35,
    "regions": [
        {"frac": [205 / 1280, 315 / 1280, 82 / 720, 170 / 720],
         "min_ink_ratio": 0.020, "max_ink_ratio": 0.040},
        {"frac": [220 / 1280, 300 / 1280, 92 / 720, 132 / 720],
         "min_ink_ratio": 0.065, "max_ink_ratio": 0.095},
    ],
    "background_regions": [
        {"frac": [20 / 1280, 600 / 1280, 70 / 720, 680 / 720],
         "max_mean_luma": 30},
    ],
}


def _item(text, box=(580, 340, 120, 40), conf=0.99):
    return {"text": text, "conf": conf, "box": list(box)}


class FakeOcr:
    def __init__(self, by_tag):
        self.by_tag = by_tag
        self.calls = []

    def __call__(self, frame):
        tag = int(frame[0, 0, 0])
        self.calls.append(tag)
        return self.by_tag.get(tag, [])


def _frame(name: str) -> np.ndarray:
    crop = cv2.imread(str(FIXTURES / name))
    assert crop is not None, name
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    h, w = crop.shape[:2]
    frame[CROP_Y0:CROP_Y0 + h, CROP_X0:CROP_X0 + w] = crop
    return frame


def _frame_tag(tag: int) -> np.ndarray:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[0, 0, 0] = tag
    return frame


def _scorer(**health_overrides) -> BlackMythScorer:
    health = {
        "confirm_frames": 2,
        "scale": 100,
        **health_overrides,
    }
    return BlackMythScorer(
        {"calibrated_client_size": list(CLIENT_SIZE),
         "boss_health": health},
        CLIENT_SIZE,
    )


def _terminal_scorer() -> BlackMythScorer:
    return BlackMythScorer(
        {
            "calibrated_client_size": list(CLIENT_SIZE),
            "boss_health": {"confirm_frames": 1},
            "terminal_menu": {
                "min_luma": 85,
                "neutral_tolerance": 50,
                "regions": [
                    {"frac": [55 / 1280, 340 / 1280, 100 / 720, 142 / 720],
                     "min_ink_ratio": 0.018, "max_ink_ratio": 0.032},
                    {"frac": [75 / 1280, 165 / 1280, 100 / 720, 140 / 720],
                     "min_ink_ratio": 0.060, "max_ink_ratio": 0.100},
                    {"frac": [85 / 1280, 150 / 1280, 105 / 720, 136 / 720],
                     "min_ink_ratio": 0.120, "max_ink_ratio": 0.180},
                ],
            },
            "victory_screen": VICTORY_SCREEN,
        },
        CLIENT_SIZE,
    )


def _text_terminal_scorer(ocr: FakeOcr) -> BlackMythScorer:
    return BlackMythScorer(
        {
            "calibrated_client_size": list(CLIENT_SIZE),
            "boss_health": {"confirm_frames": 1},
            "victory_splash_gate": {
                "min_luma": 0,
                "neutral_tolerance": 255,
                "regions": [
                    {"frac": [0.4, 0.6, 0.4, 0.6],
                     "min_ink_ratio": 0.0, "max_ink_ratio": 1.0},
                ],
            },
            "victory_text": {
                "pattern": "已击破",
                "rect": [540, 300, 220, 140],
            },
        },
        CLIENT_SIZE,
        ocr=ocr,
    )


def _synthetic_defeat_menu() -> np.ndarray:
    """Match the three nested ink ratios measured on the real 39s menu."""
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    # The real title contributes about 292 neutral-bright pixels, all inside
    # the innermost crop. That is simultaneously 14.5%, 8.1%, and 2.4% of the
    # three calibrated nested regions.
    title = np.zeros((31, 65, 3), dtype=np.uint8)
    ink = title.reshape(-1, 3)
    ink[:292] = 180
    frame[105:136, 85:150] = title
    return frame


def _synthetic_victory_screen() -> np.ndarray:
    """Match the stable result-title ratios measured on three victories."""
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    # 250 bright pixels occupy 7.8% of the tight title crop and 2.6% of the
    # enclosing crop, matching the fixed result-title ink while allowing the
    # boss name, timing, and combat statistics elsewhere on the page to vary.
    title = np.zeros((40, 80, 3), dtype=np.uint8)
    title.reshape(-1, 3)[:250] = 180
    frame[92:132, 220:300] = title
    return frame


def _synthetic_boss_frame(fill: float) -> np.ndarray:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    x0, x1, y0, y1 = 460, 825, 610, 620
    filled = max(0, min(x1 - x0, round((x1 - x0) * fill)))
    frame[y0:y1, x0:x0 + filled] = 220
    return frame


def test_real_frames_separate_full_and_damaged_boss_health():
    scorer = _scorer(confirm_frames=1)

    full = scorer.read_boss_hp(_frame("boss_full.png"))
    damaged = scorer.read_boss_hp(_frame("boss_damaged.png"))

    assert full is not None and full > 0.97
    assert damaged is not None and 0.50 < damaged < 0.62


def test_score_is_zero_at_full_and_rises_toward_one_hundred():
    scorer = _scorer()
    full = _frame("boss_full.png")
    damaged = _frame("boss_damaged.png")

    assert scorer.delta(full) == 0
    assert scorer.delta(full) == 0
    assert scorer.delta(damaged) == 0
    reward = scorer.delta(damaged)

    assert 38 < reward < 50
    assert scorer.total == pytest.approx(reward)


def test_less_boss_health_always_produces_a_higher_monotonic_score():
    scorer = _scorer(confirm_frames=1)

    scorer.delta(_synthetic_boss_frame(0.8))
    score_at_eighty_percent_hp = scorer.total
    scorer.delta(_synthetic_boss_frame(0.4))
    score_at_forty_percent_hp = scorer.total

    assert 15 < score_at_eighty_percent_hp < 25
    assert 55 < score_at_forty_percent_hp < 65
    assert score_at_forty_percent_hp > score_at_eighty_percent_hp

    # Healing or a noisier later read must never lower the earned peak score.
    scorer.delta(_synthetic_boss_frame(0.7))
    assert scorer.total == pytest.approx(score_at_forty_percent_hp)


def test_repeated_or_recovered_health_cannot_repeat_or_remove_score():
    scorer = _scorer(confirm_frames=1)
    full = _frame("boss_full.png")
    damaged = _frame("boss_damaged.png")

    first = scorer.delta(damaged)
    assert first > 38
    assert scorer.delta(damaged) == 0
    assert scorer.delta(full) == 0
    assert scorer.total == pytest.approx(first)


def test_missing_bar_is_unknown_and_never_rewards_or_terminates():
    scorer = _scorer(confirm_frames=1)
    missing = np.zeros((720, 1280, 3), dtype=np.uint8)

    assert scorer.read_boss_hp(missing) is None
    assert scorer.delta(missing) == 0
    assert scorer.classify_phase(missing) == "non_combat"
    assert not scorer.is_terminal(missing)


def test_calibrated_rematch_menu_is_defeat_terminal_and_keeps_score():
    scorer = _terminal_scorer()
    damaged = _frame("boss_damaged.png")
    menu = _synthetic_defeat_menu()

    earned = scorer.delta(damaged)
    assert earned > 38
    assert scorer.classify_phase(menu) == "defeat"
    assert scorer.is_terminal(menu)
    assert scorer.delta(menu) == 0
    assert scorer.total == pytest.approx(earned)


def test_calibrated_victory_screen_awards_exactly_one_hundred_and_terminates():
    scorer = _terminal_scorer()
    damaged = _frame("boss_damaged.png")
    victory = _synthetic_victory_screen()

    earned = scorer.delta(damaged)
    assert 38 < earned < 50
    assert scorer.is_victory_screen(victory)
    assert scorer.classify_phase(victory) == "victory"
    assert scorer.is_terminal(victory)

    reward = scorer.delta(victory)
    assert reward == pytest.approx(100 - earned)
    assert scorer.total == pytest.approx(100)
    assert scorer.delta(victory) == 0


def test_target_boss_name_is_available_to_the_start_check():
    scorer = _text_terminal_scorer(FakeOcr({
        1: [_item("黑熊精", box=(455, 610, 70, 22))],
        2: [_item("石猿", box=(455, 610, 50, 22))],
        3: [_item("黑熊精", box=(900, 135, 70, 22))],
    }))
    boss_name_rect = [400, 580, 220, 100]

    assert scorer.check_text(_frame_tag(1), "黑熊精", rect=boss_name_rect)
    assert not scorer.check_text(_frame_tag(2), "黑熊精", rect=boss_name_rect)
    # The loading-page title must not be mistaken for an entered encounter.
    assert not scorer.check_text(_frame_tag(3), "黑熊精", rect=boss_name_rect)


def test_victory_banner_ends_immediately_and_awards_exactly_one_hundred():
    scorer = _text_terminal_scorer(FakeOcr({
        1: [_item("已击破")],
        2: [_item("黑熊精")],
        3: [_item("已击破", box=(40, 40, 120, 40))],
    }))

    assert scorer.is_terminal(_frame_tag(1))
    assert scorer.classify_phase(_frame_tag(1)) == "victory"
    assert scorer.delta(_frame_tag(1)) == 100
    assert scorer.total == 100
    assert not scorer.is_terminal(_frame_tag(2))
    assert not scorer.is_terminal(_frame_tag(3))


def test_terminal_detector_ignores_selection_changes_outside_title():
    scorer = _terminal_scorer()
    menu = _synthetic_defeat_menu()
    # Simulate a bright selected row well below the fixed title regions.
    menu[200:240, 60:350] = 240

    assert scorer.is_terminal(menu)


def test_defeat_overlay_and_near_miss_title_density_are_not_terminal():
    scorer = _terminal_scorer()
    overlay = np.zeros((720, 1280, 3), dtype=np.uint8)
    near_miss = _synthetic_defeat_menu()
    # Push only the tight title crop outside its calibrated range, matching
    # the kind of top-left bright detail seen in pre-terminal video frames.
    title = near_miss[105:136, 85:150].copy()
    title.reshape(-1, 3)[292:380] = 180
    near_miss[105:136, 85:150] = title

    assert not scorer.is_terminal(overlay)
    assert not scorer.is_terminal(near_miss)
    assert scorer.classify_phase(overlay) == "non_combat"


def test_client_size_is_guarded():
    with pytest.raises(ValueError, match="calibrated"):
        BlackMythScorer(
            {"calibrated_client_size": [1280, 720]},
            (1920, 1080),
        )
