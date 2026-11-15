from pathlib import Path

import numpy as np
import pytest

from coding4game_gui.benchmark.windows.scorers.forza_horizon_5 import DEFAULT_PATTERN, ForzaHorizon5Scorer
from coding4game_gui.benchmark.windows.games_config import load_game_config


SPEC = {
    "calibrated_client_size": [2560, 1536],
    "distance": {
        "rect": [160, 1020, 220, 130],
        "baseline_m": 726,
        "max_m": 50_000,
        "pattern": DEFAULT_PATTERN,
    },
    "arrival": {
        "threshold_m": 50,
        "success_threshold_m": 5,
        "confirm_missing_observations": 2,
    },
}


def _item(text, box=(204, 1066, 122, 42), conf=0.99):
    return {"text": text, "conf": conf, "box": list(box)}


class FakeOcr:
    def __init__(self, by_tag):
        self.by_tag = by_tag

    def __call__(self, frame):
        return self.by_tag[int(frame.max())]


def _frame(tag, size=(2560, 1536)):
    width, height = size
    frame = np.zeros((height, width, 3), np.uint8)
    frame[0, 0, 0] = tag
    frame[int(height * 1072 / 1536), int(width * 240 / 2560), 0] = tag
    return frame


def _scorer(by_tag, size=(2560, 1536)):
    return ForzaHorizon5Scorer(SPEC, client_size=size, ocr=FakeOcr(by_tag))


def test_score_is_baseline_minus_latest_distance_in_both_directions():
    scorer = _scorer({
        1: [_item("726M")],
        2: [_item("500 M")],
        3: [_item("650M")],
    })

    assert scorer.delta(_frame(1)) == 0.0
    assert scorer.delta(_frame(2)) == 226.0
    assert scorer.total == 226.0
    assert scorer.delta(_frame(3)) == -150.0
    assert scorer.total == 76.0
    assert scorer.last_distance_m == 650.0


@pytest.mark.parametrize(("text", "expected"), [
    ("1.2 KM", 1200.0),
    ("1,5KM", 1500.0),
    ("42m", 42.0),
])
def test_distance_units_are_normalized_to_metres(text, expected):
    scorer = _scorer({1: [_item(text)]})
    assert scorer.read_distance_m(_frame(1)) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("1726M", 726.0),
    ("11000 M", 1000.0),
])
def test_map_pin_ocr_prefix_is_not_counted_as_distance(text, expected):
    scorer = _scorer({1: [_item(text)]})
    assert scorer.read_distance_m(_frame(1)) == expected


def test_roi_rejects_unrelated_hud_numbers():
    scorer = _scorer({1: [
        _item("41GPU:70%", box=(2390, 2, 166, 26)),
        _item("49", box=(968, 288, 70, 50)),
        _item("726M", box=(800, 800, 122, 42)),
    ]})
    assert scorer.read_distance_m(_frame(1)) is None
    assert scorer.delta(_frame(1)) == 0.0


def test_missing_read_keeps_last_trustworthy_score_and_never_terminates():
    scorer = _scorer({1: [_item("500M")], 2: []})
    assert scorer.delta(_frame(1)) == 226.0
    assert scorer.delta(_frame(2)) == 0.0
    assert scorer.total == 226.0
    assert scorer.classify_phase(_frame(2)) is None
    assert not scorer.is_terminal(_frame(1))
    assert not scorer.is_terminal(_frame(2))


def test_near_destination_then_stably_missing_awards_full_score_and_arrives():
    scorer = _scorer({
        1: [_item("726M")],
        2: [_item("37M")],
        3: [],
        4: [],
        5: [],
    })

    assert scorer.delta(_frame(1)) == 0.0
    assert scorer.delta(_frame(2)) == 689.0
    assert scorer.total == 689.0

    first_missing = _frame(3)
    assert scorer.delta(first_missing) == 0.0
    assert not scorer.is_terminal(first_missing)
    assert scorer.total == 689.0

    confirmed = _frame(4)
    assert scorer.delta(confirmed) == 37.0
    assert scorer.total == 726.0
    assert scorer.classify_phase(confirmed) == "arrival"
    assert scorer.is_terminal(confirmed)

    assert scorer.delta(_frame(5)) == 0.0
    assert scorer.total == 726.0


@pytest.mark.parametrize("distance_text", ["5M", "0M"])
def test_visible_success_distance_immediately_awards_full_score_and_arrives(
        distance_text):
    scorer = _scorer({1: [_item(distance_text)]})
    frame = _frame(1)

    assert scorer.is_terminal(frame)
    assert scorer.classify_phase(frame) == "arrival"
    assert scorer.delta(frame) == 726.0
    assert scorer.total == 726.0


def test_visible_distance_above_threshold_disarms_arrival_candidate():
    scorer = _scorer({
        1: [_item("37M")],
        2: [],
        3: [_item("80M")],
        4: [],
        5: [],
    })

    assert scorer.delta(_frame(1)) == 689.0
    assert scorer.delta(_frame(2)) == 0.0
    assert scorer.delta(_frame(3)) == -43.0
    assert scorer.delta(_frame(4)) == 0.0
    assert scorer.delta(_frame(5)) == 0.0
    assert scorer.total == 646.0
    assert not scorer.is_terminal(_frame(5))


def test_terminal_scan_before_delta_confirms_arrival_without_losing_reward():
    scorer = _scorer({
        1: [_item("37M")],
        2: [],
        3: [],
    })

    assert scorer.delta(_frame(1)) == 689.0
    assert not scorer.is_terminal(_frame(2))
    confirmed = _frame(3)
    assert scorer.is_terminal(confirmed)
    assert scorer.total == 689.0
    assert scorer.delta(confirmed) == 37.0
    assert scorer.total == 726.0


def test_reset_clears_arrival_state():
    scorer = _scorer({
        1: [_item("37M")],
        2: [],
        3: [],
        4: [_item("500M")],
        5: [],
        6: [],
    })

    scorer.delta(_frame(1))
    scorer.delta(_frame(2))
    scorer.delta(_frame(3))
    assert scorer.total == 726.0

    scorer.reset()
    assert scorer.delta(_frame(4)) == 226.0
    assert scorer.delta(_frame(5)) == 0.0
    assert scorer.delta(_frame(6)) == 0.0
    assert scorer.total == 226.0
    assert not scorer.is_terminal(_frame(6))


def test_roi_scales_from_dxcam_surface_to_internal_render_size():
    half_box = (102, 533, 61, 21)
    scorer = _scorer({1: [_item("726M", box=half_box)]}, size=(1280, 768))
    frame = _frame(1, size=(1280, 768))
    assert scorer.read_distance_m(frame) == 726.0
    assert scorer.check_text(frame, r"^\d+M$", rect=SPEC["distance"]["rect"])


def test_forza_benchmark_is_bound_to_complete_golden_checkpoint():
    from coding4game_gui.benchmark.spec import load_game_spec

    cfg = load_game_config("forza_horizon_5", validate_files=False)
    scenario = load_game_spec("forza_horizon_5")

    assert cfg.save_state is not None
    assert cfg.save_state.golden == (
        Path(__file__).resolve().parents[2] / "snapshots/forza_horizon_5_golden.zip")
    assert cfg.save_state.restore_to.name == "1551360"
    assert cfg.save_state.wipe_before is True
    assert scenario["startup"]["golden_checkpoint"] == "golden"


def test_registered_route_scores_across_km_m_boundary_and_back():
    from coding4game_gui.benchmark.spec import load_game_spec

    spec = load_game_spec("forza_horizon_5")
    texts = ["1.5 KM", "1.1 KM", "1000 M", "999 M", "726 M", "1000 M", "1.1 KM"]
    scorer = ForzaHorizon5Scorer(
        spec["scorer"], client_size=(2560, 1536),
        ocr=FakeOcr({i: [_item(t)] for i, t in enumerate(texts, 1)}))
    assert spec["startup"]["start_check"]["regions"]["distance"] == 1500
    for i, expected in enumerate([0, 400, 500, 501, 774, 500, 400], 1):
        scorer.delta(_frame(i))
        assert scorer.total == expected
        assert not scorer.is_terminal(_frame(i))


@pytest.mark.parametrize("size", [(2560, 1536), (1280, 768)])
def test_text_mask_excludes_pin_preserves_frame_and_refreshes_every_text_pixel(size):
    from coding4game_gui.benchmark.spec import load_game_spec

    spec = load_game_spec("forza_horizon_5")["scorer"]
    calls = []
    def ocr(frame):
        calls.append(frame.copy())
        # A real two-digit KM value must not lose its first digit.
        sx, sy = size[0] / 2560, size[1] / 1536
        return [_item("11.5 KM", box=(240*sx, 1070*sy, 100*sx, 36*sy))]
    scorer = ForzaHorizon5Scorer(spec, size, ocr=ocr)
    frame = np.full((size[1], size[0], 3), 90, dtype=np.uint8)
    original = frame.copy()
    assert scorer.read_distance_m(frame) == 11500
    assert np.array_equal(frame, original)
    assert not calls[0][int(1080*size[1]/1536), int(215*size[0]/2560)].any()
    assert calls[0][int(1080*size[1]/1536), int(245*size[0]/2560)].all()
    scorer.read_distance_m(frame.copy())
    assert len(calls) == 1
    frame[int(1081*size[1]/1536), int(245*size[0]/2560), 0] += 1
    scorer.read_distance_m(frame)
    assert len(calls) == 2
    scorer.reset()
    scorer.read_distance_m(frame)
    assert len(calls) == 3
