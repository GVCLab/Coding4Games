"""OcrScorer: text-rule phase classification + ported judgment layer.

Perception is faked (injected `ocr` callable) — these tests pin the RULES:
keyword priority, regex value extraction, box-position number assignment,
monotonic/spendable counters, ScorerLost, start_check text matching."""
import sys
import types

import numpy as np
import pytest

from coding4game_gui.benchmark.windows.scorers.ocr import DEFAULT_INPUT_SIZE
from coding4game_gui.benchmark.windows.scorers.ocr import DEFAULT_MIN_CONF
from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer
from coding4game_gui.benchmark.windows.scorers.ocr import PaddleOcrEngine
from coding4game_gui.benchmark.windows.scorers.ocr import ScorerLost

W, H = 200, 100
MAT_RECT = [10, 40, 60, 20]

SPEC = {
    "calibrated_client_size": [W, H],
    "phases": {
        "death":   {"any": ["RUN LO"]},
        "levelup": {"any": ["LEVEL UP"]},
        "shop":    {"any": ["SHOP"]},
        "combat":  {"pattern": r"WAVE\s*\d"},
    },
    "regions": {
        "wave":      {"pattern": r"WAVE\s*(\d+)"},
        "materials": {"rect": MAT_RECT, "kind": "number", "spendable": True},
    },
    "weights": {"wave": 100, "materials": 1},
}


def _item(text, box=(150, 5, 40, 12), conf=0.9):
    return {"text": text, "conf": conf, "box": list(box)}


def combat_items(wave, mats=None, glued=False):
    items = [_item(f"WAVE{wave}" if glued else f"WAVE {wave}")]
    if mats is not None:
        items.append(_item(str(mats), box=(20, 45, 20, 10)))  # inside MAT_RECT
    return items


class FakeOcr:
    """frame[0,0,0] is the scenario tag -> scripted OCR items."""

    def __init__(self, by_tag: dict):
        self.by_tag = by_tag
        self.calls = 0

    def __call__(self, frame):
        self.calls += 1
        return self.by_tag[int(frame[0, 0, 0])]


_TAG = 0


def frame(tag):
    f = np.zeros((H, W, 3), np.uint8)
    f[0, 0, 0] = tag
    return f


def scorer(by_tag):
    return OcrScorer(SPEC, client_size=(W, H), ocr=FakeOcr(by_tag))


def test_paddle_engine_forwards_yaml_language_and_runtime_options(monkeypatch):
    captured = {}

    class FakePaddleOCR:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=FakePaddleOCR))
    engine = PaddleOcrEngine(
        device="cpu",
        config={"lang": "ch", "enable_mkldnn": False, "cpu_threads": 2,
                "text_detection_model_name": "PP-OCRv6_small_det",
                "text_recognition_model_name": "PP-OCRv6_small_rec"},
    )

    assert isinstance(engine._ensure(), FakePaddleOCR)
    assert captured["lang"] == "ch"
    assert captured["device"] == "cpu"
    assert captured["enable_mkldnn"] is False
    assert captured["cpu_threads"] == 2
    assert captured["text_detection_model_name"] == "PP-OCRv6_small_det"
    assert captured["text_recognition_model_name"] == "PP-OCRv6_small_rec"


def test_region_text_recognition_trims_padding_and_rejects_missing_text(monkeypatch):
    captured = {}

    class Recognizer:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.confidence = 0.99

        def predict(self, image):
            captured["shape"] = image.shape
            return [{"rec_text": "1", "rec_score": self.confidence}]

    monkeypatch.setitem(sys.modules, "paddleocr",
                        types.SimpleNamespace(TextRecognition=Recognizer))
    engine = PaddleOcrEngine(device="cpu", min_conf=0.8,
                            config={"text_recognition_model_name": "selected_rec"})
    image = np.zeros((35, 50, 3), dtype=np.uint8)
    assert engine.recognize(image, trim_light_text=True) == []
    assert captured == {}  # Missing foreground does not load or call a model.
    image[10:25, 5:10] = 255
    result = engine.recognize(image, trim_light_text=True)
    assert captured["model_name"] == "selected_rec"
    assert captured["device"] == "cpu"
    assert captured["shape"] == (21, 11, 3)
    assert result == [{"text": "1", "conf": 0.99, "box": [2, 7, 11, 21]}]
    engine._text_recognizer.confidence = 0.2
    assert engine.recognize(image, trim_light_text=True) == []


def test_ocr_rect_crops_input_and_restores_frame_coordinates():
    seen = []

    def fake_ocr(crop):
        seen.append(crop.shape)
        return [_item("DONE", box=(5, 6, 20, 10))]

    spec = {
        "calibrated_client_size": [W, H],
        "ocr": {"rect": [50, 10, 100, 50]},
        "phases": {"success": {"any": ["DONE"],
                                "rect": [50, 10, 100, 50]}},
        "regions": {},
        "weights": {},
    }
    s = OcrScorer(spec, client_size=(W, H), ocr=fake_ocr)

    assert s.classify_phase(frame(0)) == "success"
    assert seen == [(50, 100, 3)]
    assert s._read(frame(0))[0]["box"] == [55, 16, 20, 10]


def test_results_phase_waits_for_visual_data_to_finish_loading():
    spec = {
        "calibrated_client_size": [W, H],
        "phases": {"results": {
            "pattern": r"Gridshot\s*3x3\b.*\b\d+\b",
            "visual_ready": {"rect": [10, 10, 40, 20],
                             "all_channels_min": 210, "min_pixels": 5},
        }},
        "score_phases": ["results"],
        "terminal_phases": ["results"],
        "regions": {"score": {"rect": [70, 10, 30, 20]}},
        "weights": {"score": 1},
    }
    items = [_item("Gridshot 3X3 0"), _item("0", box=(75, 12, 12, 12))]
    s = OcrScorer(spec, client_size=(W, H), ocr=FakeOcr({1: items}))
    loading = frame(1)
    ready = frame(1)
    ready[12:15, 12:15] = 255

    assert not s.is_terminal(loading)
    assert s.snapshot(loading) is None
    assert s.is_terminal(ready)
    assert s.snapshot(ready)["score"] == 0


def test_results_phase_requires_killed_target_readout_not_large_score():
    spec = {
        "calibrated_client_size": [W, H],
        "phases": {"results": {
            "pattern": r"Gridshot\s*3x3\b",
            "required_regions": ["killed_targets"],
        }},
        "score_phases": ["results"],
        "terminal_phases": ["results"],
        "regions": {"killed_targets": {
            "rect": [120, 40, 45, 20], "baseline": 0,
            "fallback": {"rect": [65, 40, 45, 20],
                         "pattern": r"^(\d+)/\d+$"}}},
        "weights": {"killed_targets": 1},
    }
    observations = {
        1: [_item("Gridshot 3x3"), _item("35", (70, 10, 20, 12))],
        2: [_item("Gridshot 3x3"), _item("35", (70, 10, 20, 12)),
            _item("98", (130, 43, 20, 12))],
        3: [_item("Gridshot 3x3"), _item("0/0", (70, 43, 30, 12))],
    }
    s = OcrScorer(spec, client_size=(W, H), ocr=FakeOcr(observations))

    assert not s.is_terminal(frame(1))
    assert s.snapshot(frame(1)) is None
    assert s.is_terminal(frame(2))
    assert s.snapshot(frame(2))["score"] == 98
    assert s.snapshot(frame(3))["score"] == 0


def test_last_scoring_phase_carries_across_label_hidden_animation():
    spec = {
        "calibrated_client_size": [W, H],
        "phases": {"play": {"any": ["PLAY"]}},
        "score_phases": ["play"],
        "regions": {"score": {"pattern": r"SCORE\s*(\d+)", "baseline": 0}},
        "weights": {"score": 1},
    }
    s = OcrScorer(spec, client_size=(W, H), ocr=FakeOcr({
        1: [_item("PLAY"), _item("SCORE 10")],
        2: [_item("SCORE 20")],
    }))

    assert s.delta(frame(1)) == 10
    assert s.delta(frame(2)) == 10
    assert s.total == 20


def test_full_episode_arc():
    s = scorer({
        1: combat_items(1, 5), 2: combat_items(1, 8),
        3: [_item("LEVEL UP")], 4: [_item("Shop (Wave 2)")],
        5: combat_items(2, 2), 6: combat_items(2, 4),
        7: [_item("RUN LO"), _item("WAVE 2")],   # death keeps the HUD visible
    })
    assert s.delta(frame(1)) == 0.0                    # baseline
    assert s.delta(frame(2)) == 3.0                    # +materials
    assert s.delta(frame(3)) == 0.0                    # levelup
    assert s.delta(frame(4)) == 0.0                    # shop
    # back in combat: wave 1->2 = +100; materials 8->2 = legal post-shop spend
    assert s.delta(frame(5)) == 100.0
    assert s.delta(frame(6)) == 2.0
    assert not s.is_terminal(frame(6))
    assert s.is_terminal(frame(7))                     # death wins over combat
    assert s.total == 105.0


def test_phase_priority_is_declaration_order():
    s = scorer({1: [_item("RUN LO"), _item("WAVE 3")],
                2: [_item("Shop (Wave 3)")]})
    assert s.classify_phase(frame(1)) == "death"
    assert s.classify_phase(frame(2)) == "shop"        # not combat: shop first


def test_keyword_matching_survives_ocr_spacing():
    s = scorer({1: [_item("LEVELUP")],                 # glued
                2: combat_items(4, glued=True)})       # "WAVE4"
    assert s.classify_phase(frame(1)) == "levelup"
    assert s.classify_phase(frame(2)) == "combat"
    s.delta(frame(2))
    assert s._readings["wave"] == 4


def test_in_combat_single_frame_regression_is_dropped():
    s = scorer({1: combat_items(1, 9), 2: combat_items(1, 3),
                3: combat_items(1, 11)})
    s.delta(frame(1))
    assert s.delta(frame(2)) == 0.0                    # misread drop ignored
    assert s.delta(frame(3)) == 2.0                    # 9 -> 11


def test_wave_decrease_after_menu_is_rejected():
    s = scorer({1: combat_items(12, 5), 2: [_item("SHOP")],
                3: combat_items(2, 5), 4: combat_items(12, 5)})
    s.delta(frame(1))
    s.delta(frame(2))
    assert s.delta(frame(3)) == 0.0    # wave is NOT spendable: misread
    assert s.delta(frame(4)) == 0.0    # must not re-score from the misread


def test_number_outside_rect_is_not_materials():
    s = scorer({1: [_item("WAVE 1"), _item("47", box=(150, 80, 20, 10))]})
    s.delta(frame(1))
    assert "materials" not in s._readings
    assert s._readings["wave"] == 1


SPIKE_SPEC = dict(SPEC, regions={
    "wave":      {"pattern": r"WAVE\s*(\d+)", "max_jump": 2},
    "materials": {"rect": MAT_RECT, "kind": "number", "spendable": True,
                  "max_jump": 50},
})


def test_overlay_spike_is_not_adopted():
    # a damage number drifting through the rect reads as a huge value ONCE;
    # it must neither score nor poison the baseline (frame 262 incident)
    s = OcrScorer(SPIKE_SPEC, client_size=(W, H), ocr=FakeOcr({
        1: combat_items(2, 16), 2: combat_items(2, 1810),
        3: combat_items(2, 17)}))
    s.delta(frame(1))
    assert s.delta(frame(2)) == 0.0          # spike held for confirmation
    assert s._readings["materials"] == 16    # baseline NOT poisoned
    assert s.delta(frame(3)) == 1.0          # true +1 still counts


def test_genuine_big_jump_confirms_on_second_read():
    s = OcrScorer(SPIKE_SPEC, client_size=(W, H), ocr=FakeOcr({
        1: combat_items(2, 16), 2: combat_items(2, 116),
        3: combat_items(2, 118)}))
    s.delta(frame(1))
    assert s.delta(frame(2)) == 0.0          # first sight: pending
    assert s.delta(frame(3)) == 102.0        # persists (±tol) -> full credit
    assert s._readings["materials"] == 118


BASELINE_SPEC = dict(SPEC, regions={
    "wave":      {"pattern": r"WAVE\s*(\d+)", "max_jump": 2, "baseline": 0},
    "materials": {"rect": MAT_RECT, "kind": "number", "spendable": True},
})

STRICT_WAVE_SPEC = dict(SPEC, regions={
    "wave": {"pattern": r"WAVE\s*(\d+)", "baseline": 0,
             "max_increment": 1},
    "materials": {"rect": MAT_RECT, "kind": "number", "spendable": True,
                  "max_jump": 50},
})


def test_declared_baseline_credits_first_reading():
    # wave with baseline 0: score is literally 100 x wave, wave 1 included
    s = OcrScorer(BASELINE_SPEC, client_size=(W, H), ocr=FakeOcr({
        1: combat_items(1, 5), 2: combat_items(2, 5)}))
    assert s.delta(frame(1)) == 100.0        # wave 1 itself pays
    assert s.delta(frame(2)) == 100.0        # crossing to wave 2
    assert s._readings["wave"] == 2


def test_declared_baseline_bogus_first_read_held():
    # a huge FIRST reading (misread "WAVE 7" at spawn) must not pay out
    # unconfirmed; the true wave 1 read afterwards pays normally
    s = OcrScorer(BASELINE_SPEC, client_size=(W, H), ocr=FakeOcr({
        1: combat_items(7, 5), 2: combat_items(1, 5)}))
    assert s.delta(frame(1)) == 0.0          # 7 - 0 > max_jump: pending
    assert "wave" not in s._readings
    assert s.delta(frame(2)) == 100.0        # real wave 1 confirms nothing,
    assert s._readings["wave"] == 1          # scores from baseline


def test_wave_jump_is_rejected_even_when_repeated():
    s = OcrScorer(STRICT_WAVE_SPEC, client_size=(W, H), ocr=FakeOcr({
        1: combat_items(1, 5),
        2: combat_items(11, 5),
        3: combat_items(11, 5),
        4: combat_items(2, 5),
        5: combat_items(3, 5),
    }))
    assert s.delta(frame(1)) == 100.0
    assert s.delta(frame(2)) == 0.0
    assert s.delta(frame(3)) == 0.0
    assert s._readings["wave"] == 1
    assert s.delta(frame(4)) == 100.0
    assert s.delta(frame(5)) == 100.0
    assert s._readings["wave"] == 3
    assert s.total == 300.0


def test_initial_wave_jump_is_rejected_until_wave_one_appears():
    s = OcrScorer(STRICT_WAVE_SPEC, client_size=(W, H), ocr=FakeOcr({
        1: combat_items(11, 5),
        2: combat_items(11, 5),
        3: combat_items(1, 5),
    }))
    assert s.delta(frame(1)) == 0.0
    assert s.delta(frame(2)) == 0.0
    assert "wave" not in s._readings
    assert s.delta(frame(3)) == 100.0
    assert s._readings["wave"] == 1


def test_brotato_wave_config_requires_single_increment():
    from coding4game_gui.benchmark.spec import load_game_spec

    spec = load_game_spec("brotato")
    wave = spec["scorer"]["regions"]["wave"]
    assert wave["max_increment"] == 1
    assert "max_jump" not in wave
    assert spec["scorer"]["weights"]["wave"] == 100


def test_brotato_material_spike_is_rejected_even_when_repeated():
    import copy
    from coding4game_gui.benchmark.spec import load_game_spec

    sc = copy.deepcopy(load_game_spec("brotato")["scorer"])
    assert sc["regions"]["materials"]["max_increment"] == 50
    sc["calibrated_client_size"] = [W, H]
    sc["regions"]["materials"]["rect"] = MAT_RECT
    s = OcrScorer(sc, client_size=(W, H), ocr=FakeOcr({
        1: combat_items(1, 10),
        2: combat_items(1, 1010),
        3: combat_items(1, 1010),
        4: combat_items(1, 20),
    }))
    assert s.delta(frame(1)) == 100.0
    assert s.delta(frame(2)) == 0.0
    assert s.delta(frame(3)) == 0.0
    assert s.delta(frame(4)) == 10.0


def test_small_gains_flow_through_unconfirmed():
    s = OcrScorer(SPIKE_SPEC, client_size=(W, H), ocr=FakeOcr({
        1: combat_items(2, 16), 2: combat_items(2, 40)}))
    s.delta(frame(1))
    assert s.delta(frame(2)) == 24.0         # under max_jump: immediate


def test_scorer_lost_after_consecutive_misses():
    s = scorer({1: combat_items(1, 1), 2: []})         # 2: nothing recognized
    s.delta(frame(1))
    with pytest.raises(ScorerLost):
        for i in range(10):
            f = frame(2)
            f[0, 1, 0] = i                             # defeat the memo cache
            s.delta(f)


def test_client_size_mismatch_raises():
    with pytest.raises(ValueError, match="calibrated"):
        OcrScorer(SPEC, client_size=(W + 8, H), ocr=FakeOcr({}))


def test_reset_clears_readings_and_misses():
    s = scorer({1: combat_items(3, 50), 2: combat_items(1, 5)})
    s.delta(frame(1))
    s.reset()
    assert s.total == 0.0
    assert s.delta(frame(2)) == 0.0                    # re-baseline


def test_ocr_memoized_per_frame():
    s = scorer({1: combat_items(1, 5)})
    f = frame(1)
    s.delta(f)
    s.classify_phase(f)
    s.is_terminal(f)
    assert s.ocr.calls == 1


def test_check_text_full_hp_backreference():
    s = scorer({1: [_item("15/15", box=(30, 20, 40, 12)), _item("WAVE 1")],
                2: [_item("0/15", box=(30, 20, 40, 12))],
                3: [_item("16/16", box=(150, 80, 30, 10))]})  # outside rect
    rect = [0, 0, 100, 50]
    assert s.check_text(frame(1), r"(\d+)/\1", rect=rect)
    assert not s.check_text(frame(2), r"(\d+)/\1", rect=rect)
    assert not s.check_text(frame(3), r"(\d+)/\1", rect=rect)


def test_start_check_joins_only_boxes_inside_its_rectangle():
    s = scorer({
        1: [_item("POINTS", box=(10, 10, 20, 10)),
            _item("00:29", box=(60, 10, 20, 10))],
        2: [_item("POINTS", box=(10, 10, 20, 10)),
            _item("00:30", box=(60, 10, 20, 10))],
        3: [_item("POINTS", box=(10, 10, 20, 10)),
            _item("00:29", box=(150, 80, 20, 10))],
    })
    pattern = r"(?s)(?=.*\bPOINTS\b)(?=.*00\s*[:：]\s*2\d)"
    rect = [0, 0, 100, 50]
    assert not s.check_text(frame(1), pattern, rect=rect)
    assert s.check_text(frame(1), pattern, rect=rect, join_text_boxes=True)
    assert not s.check_text(frame(2), pattern, rect=rect, join_text_boxes=True)
    assert not s.check_text(frame(3), pattern, rect=rect, join_text_boxes=True)


def test_read_region_is_stateless():
    s = scorer({1: combat_items(5, 42)})
    assert s.read_region(frame(1), "wave") == 5
    assert s.read_region(frame(1), "materials") == 42
    assert s._readings == {} and s.total == 0.0    # no judgment side effects
    with pytest.raises(KeyError):
        s.read_region(frame(1), "nope")


def test_terminal_phases_spec_override():
    spec = dict(SPEC, terminal_phases=["death", "victory"],
                phases={**SPEC["phases"], "victory": {"any": ["YOU WON"]}})
    s = OcrScorer(spec, client_size=(W, H),
                  ocr=FakeOcr({1: [_item("YOU WON")]}))
    assert s.is_terminal(frame(1))


def test_phase_rule_can_be_restricted_to_a_rect():
    spec = {
        "calibrated_client_size": [W, H],
        "phases": {
            "success": {"any": ["DONE"], "rect": [50, 0, 50, 50]},
            "task": {"any": ["PLAY"]},
        },
        "regions": {},
        "weights": {},
    }
    s = OcrScorer(spec, client_size=(W, H), ocr=FakeOcr({
        1: [_item("DONE", box=(5, 5, 20, 10)), _item("PLAY")],
        2: [_item("DONE", box=(60, 5, 20, 10))],
    }))

    assert s.classify_phase(frame(1)) == "task"
    assert s.classify_phase(frame(2)) == "success"


def test_phase_reward_is_paid_only_once_per_episode():
    spec = {
        "calibrated_client_size": [W, H],
        "phases": {
            "victory": {"any": ["VICTORY"]},
            "death": {"any": ["YOU DIED"]},
            "combat": {"any": ["BOSS"]},
        },
        "terminal_phases": ["victory", "death"],
        "phase_rewards": {"victory": 1},
        "regions": {},
        "weights": {},
    }
    s = OcrScorer(spec, client_size=(W, H), ocr=FakeOcr({
        1: [_item("BOSS")],
        2: [_item("VICTORY")],
        3: [_item("VICTORY")],
        4: [_item("BOSS")],
        5: [_item("VICTORY")],
    }))

    assert s.delta(frame(1)) == 0
    assert s.delta(frame(2)) == 1
    assert s.delta(frame(3)) == 0
    assert s.delta(frame(4)) == 0
    assert s.delta(frame(5)) == 0
    assert s.total == 1


def test_phase_only_combat_is_a_healthy_read_without_numeric_regions():
    spec = {
        "calibrated_client_size": [W, H],
        "phases": {"combat": {"any": ["BOSS"]}},
        "regions": {},
        "weights": {},
    }
    s = OcrScorer(spec, client_size=(W, H),
                  ocr=FakeOcr({1: [_item("BOSS")]}))

    for _ in range(12):
        assert s.delta(frame(1)) == 0


# -- real-perception regression net (opt-in: needs paddle + the GPU) --
import os                                                  # noqa: E402
from pathlib import Path                                   # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "brotato"


@pytest.mark.skipif(os.environ.get("C4G_OCR_TESTS") != "1"
                    or not FIXTURES.is_dir(),
                    reason="set C4G_OCR_TESTS=1 (needs paddleocr + GPU)")
def test_real_brotato_fixtures_with_paddleocr():
    import cv2
    from coding4game_gui.benchmark.spec import load_game_spec
    sc = load_game_spec("brotato")["scorer"]
    scorer = OcrScorer(sc, client_size=tuple(sc["calibrated_client_size"]))
    for p in sorted(FIXTURES.glob("*.png")):
        phase, wave, mats = p.stem.split("_")
        f = cv2.imread(str(p))
        assert scorer.classify_phase(f) == phase, p.name
        items = scorer._read(f)
        if wave != "x":
            v = scorer._region_value("wave", sc["regions"]["wave"], items)
            assert v == int(wave), p.name
        if mats != "x":
            v = scorer._region_value("materials", sc["regions"]["materials"],
                                     items)
            assert v == int(mats), p.name


BALATRO_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "balatro"


def test_balatro_rejects_persistent_counter_jump_and_deck_overlay():
    from coding4game_gui.benchmark.spec import load_game_spec
    spec = load_game_spec('balatro')['scorer']
    def items(ante, round_number, overlay=False):
        result = [_item('Play Hand', (700, 400, 100, 20)),
                  _item('Ante', (211, 744, 50, 26)),
                  _item('Round', (312, 744, 62, 26)),
                  _item('Round', (74, 314, 74, 26)),
                  _item('score', (76, 343, 71, 26)),
                  _item(str(ante), (200, 775, 30, 20)),
                  _item(str(round_number), (320, 775, 30, 20)),
                  _item('0', (240, 330, 30, 20))]
        if overlay:
            result.append(_item('Remaining Full Deck Base Cards', (600, 300, 400, 20)))
        return result
    ocr = FakeOcr({1: items(1, 1), 2: items(9, 8), 3: items(2, 2, True),
                   4: items(2, 2)})
    scorer = OcrScorer(spec, client_size=(1536, 864), ocr=ocr)
    assert scorer.delta(frame(1)) == 0
    for _ in range(3):
        assert scorer.delta(frame(2)) == 0
    assert scorer.delta(frame(3)) == 0
    assert scorer.delta(frame(4)) == 1300


def test_balatro_requires_matching_local_labels_before_reading_numbers():
    from coding4game_gui.benchmark.spec import load_game_spec
    spec = load_game_spec('balatro')['scorer']
    numbers = [_item('1', (200, 775, 30, 20)), _item('1', (320, 775, 30, 20)),
               _item('25', (240, 330, 30, 20)), _item('Play Hand')]
    labels = [_item('Ante', (211, 744, 50, 26)), _item('Round', (312, 744, 62, 26)),
              _item('Round', (74, 314, 74, 26)), _item('score', (76, 343, 71, 26))]
    by_tag = {1: numbers, 2: numbers + [_item('Ante Round score', (600, 200, 200, 30))],
              3: numbers + labels, 4: numbers + [_item('score', (76, 343, 71, 26))]}
    scorer = OcrScorer(spec, client_size=(1536, 864), ocr=FakeOcr(by_tag))
    for tag in (1, 2, 4):
        for name in spec['regions']:
            assert scorer.read_region(frame(tag), name) is None
        assert scorer.delta(frame(tag)) == 0
    assert scorer.read_region(frame(3), 'ante') == 1
    assert scorer.read_region(frame(3), 'round') == 1
    assert scorer.read_region(frame(3), 'round_score') == 25
    assert scorer.delta(frame(3)) == 25


def _balatro_scorer_factory():
    """(spec, make(frame)) for the balatro fixtures.

    The fixtures are live 1600x900 captures while the rects are calibrated in
    1536x864, so the scorer MUST be built with the frame's own size — that is
    what maps the rects down (see OcrScorer.__init__). Passing the calibrated
    size instead leaves rect_scale at 1.0 and the round counter reads None.
    One OCR engine is shared across sizes so the model loads once.
    """
    from coding4game_gui.benchmark.spec import load_game_spec
    sc = load_game_spec("balatro")["scorer"]
    ocr_cfg = sc.get("ocr") or {}
    engine = PaddleOcrEngine(
        input_size=ocr_cfg.get("input_size", DEFAULT_INPUT_SIZE),
        min_conf=ocr_cfg.get("min_conf", DEFAULT_MIN_CONF),
        device=ocr_cfg.get("device"), config=ocr_cfg)
    cache = {}

    def make(frame):
        size = (frame.shape[1], frame.shape[0])
        if size not in cache:
            cache[size] = OcrScorer(sc, client_size=size, ocr=engine)
        return cache[size]

    return sc, make


@pytest.mark.skipif(os.environ.get("C4G_OCR_TESTS") != "1"
                    or not BALATRO_FIXTURES.is_dir(),
                    reason="set C4G_OCR_TESTS=1 (needs paddleocr + GPU)")
def test_real_balatro_fixtures_with_paddleocr():
    """Real frames from a recorded death (2026-08-03 session 20260803_164923).

    Fixture name: <phase>_<ante>_<round>_<round_score>.png, "x" = don't
    assert, phase "none" = deliberately unclassified. The three gameover
    frames are the fade-in (title still illegible), the frame that used to
    misclassify as `shop` via the panel's localized reroll-count row, and the settled
    panel. `none-roundzero` is the blind-select->table transition where the
    round counter momentarily reads 0: it must NOT classify as a scoring
    phase, or `round`'s baseline is poisoned and the climb back to 1 pays a
    phantom +300.
    """
    import cv2
    sc, make = _balatro_scorer_factory()
    seen = set()
    for p in sorted(BALATRO_FIXTURES.glob("*.png")):
        want_phase, ante, rnd, chips = p.stem.split("_")
        # "-" separates an optional variant note; phase names in filenames are
        # underscore-free ("blindselect") so the field split stays simple.
        want_phase = want_phase.split("-")[0]
        want_phase = {"blindselect": "blind_select"}.get(want_phase, want_phase)
        f = cv2.imread(str(p))
        scorer = make(f)
        phase = scorer.classify_phase(f)
        expected = None if want_phase == "none" else want_phase
        assert phase == expected, f"{p.name}: got {phase!r}"
        seen.add(want_phase)
        # A non-scoring phase must never be treated as one, whatever it read.
        if expected is None or expected not in set(sc["score_phases"]):
            assert expected not in set(sc["score_phases"]), p.name
        items = scorer._read(f)
        for name, want in (("ante", ante), ("round", rnd),
                           ("round_score", chips)):
            if want == "x":
                continue
            got = scorer._region_value(name, sc["regions"][name], items)
            assert got == int(want), f"{p.name}: {name}={got}"
    assert {"gameover", "play", "blind_select", "none"} <= seen, sorted(seen)


@pytest.mark.skipif(os.environ.get("C4G_OCR_TESTS") != "1"
                    or not BALATRO_FIXTURES.is_dir(),
                    reason="set C4G_OCR_TESTS=1 (needs paddleocr + GPU)")
def test_balatro_gameover_is_terminal_from_the_fade_in_frame():
    """Every game-over fixture is terminal, including the fade-in frame whose
    localized game-over title is still too faint to recognise. Regression for a
    5-frame window that classified as `shop` instead: the panel is
    dismissable, so a late terminal lets the episode run to max_tics on a
    results page."""
    import cv2
    _sc, make = _balatro_scorer_factory()
    shots = sorted(BALATRO_FIXTURES.glob("gameover-*.png"))
    assert shots, "no gameover fixtures"
    for p in shots:
        f = cv2.imread(str(p))
        assert make(f).is_terminal(f), p.name
    for p in sorted(BALATRO_FIXTURES.glob("*.png")):
        if p.name.startswith("gameover-"):
            continue
        f = cv2.imread(str(p))
        assert not make(f).is_terminal(f), p.name


def test_balatro_scores_progress_only_not_money():
    """Money must not be scored: dollars can be held by never spending, so
    paying for the balance rewards hoarding over playing."""
    from coding4game_gui.benchmark.spec import load_game_spec
    sc = load_game_spec("balatro")["scorer"]
    assert set(sc["weights"]) == {"ante", "round", "round_score"}
    assert "money" not in sc["regions"]


def test_balatro_rebase_phases_can_actually_open_the_gate():
    """`delta` takes the score branch first, so a phase in BOTH score_phases
    and rebase_phases can never reach the `elif` that opens the rebase gate.
    Any rebase phase must therefore be absent from score_phases, or the
    round_score reset silently depends on some other screen appearing."""
    from coding4game_gui.benchmark.spec import load_game_spec
    sc = load_game_spec("balatro")["scorer"]
    dead = set(sc["rebase_phases"]) & set(sc["score_phases"])
    assert not dead, f"rebase membership unreachable for {sorted(dead)}"


def test_balatro_snapshot_requires_all_current_local_labels_and_skips_overlay():
    from coding4game_gui.benchmark.spec import load_game_spec
    spec = load_game_spec('balatro')['scorer']
    assert spec['settlement'] == 'latest_valid_frame'
    items = [
        _item('Sort Hand'),
        _item('Ante', (210, 740, 40, 20)), _item('2/8', (210, 790, 45, 20)),
        _item('Round', (315, 740, 45, 15)), _item('5', (325, 790, 20, 20)),
        _item('Round score', (65, 325, 90, 20)), _item('0', (260, 330, 20, 20)),
    ]
    pixels = np.zeros((864, 1536, 3), np.uint8)
    def read(values):
        return OcrScorer(spec, (1536, 864), ocr=lambda f: values).snapshot(pixels)
    result = read(items)
    assert result['score'] == 2200
    assert result['readings'] == {'ante': 2, 'round': 5, 'round_score': 0}
    assert read([it for it in items if it['text'] != 'Round score']) is None
    assert read(items + [_item('Full Deck')]) is None
    assert read([it for it in items if it['text'] != 'Sort Hand'] + [_item('SHOP')]) is None


def test_balatro_ante_rejects_lone_denominator_from_native_ocr():
    """Captured OCR missed the orange numerator but detected the white /8."""
    from coding4game_gui.benchmark.spec import load_game_spec
    spec = load_game_spec('balatro')['scorer']
    items = [_item('Ante', (222, 777, 50, 27)),
             _item('8', (261, 815, 17, 26))]
    scorer = OcrScorer(spec, (1600, 900), ocr=lambda f: items)
    assert scorer.read_region(np.zeros((900, 1600, 3), np.uint8), 'ante') is None


@pytest.mark.parametrize('size,text,width,want', [
    ((1536, 864), '8', 20, 8),
    ((1536, 864), '2/8', 65, 2),
    ((1600, 900), '18', 65, 1),
    ((1280, 720), '2/8', 65, 2),
])
def test_balatro_ante_accepts_numerator_and_merged_fraction(size, text, width, want):
    from coding4game_gui.benchmark.spec import load_game_spec
    spec = load_game_spec('balatro')['scorer']
    sx, sy = size[0] / 1536, size[1] / 864
    def scaled_item(value, box):
        x, y, w, h = box
        return _item(value, (x * sx, y * sy, w * sx, h * sy))
    items = [scaled_item('Ante', (210, 740, 40, 20)),
             scaled_item(text, (210, 790, width, 20)),
             scaled_item('8', (255, 790, 16, 20))]
    scorer = OcrScorer(spec, size, ocr=lambda f: items)
    assert scorer.read_region(np.zeros((size[1], size[0], 3), np.uint8), 'ante') == want
