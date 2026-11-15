"""Riders Republic display preparation and Zen Mode score recognition."""
from pathlib import Path

import numpy as np

from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows import game_settings
from coding4game_gui.benchmark.windows.scorers.riders_republic import RidersRepublicScorer


def _item(text, box, conf=0.999):
    return {"text": text, "box": box, "conf": conf}


def test_calibrated_score_region_reads_game_total_not_speed():
    spec = load_game_spec("riders_republic")["scorer"]
    items = [
        _item("SCORE", [62, 47, 44, 18]),
        _item("1200", [61, 76, 120, 52]),
        _item("分", [181, 93, 16, 18]),
        _item("0", [1477, 719, 43, 63]),
    ]
    scorer = RidersRepublicScorer(
        spec,
        client_size=(1600, 895),
        ocr=lambda _frame: items,
    )
    frame = np.zeros((895, 1600, 3), dtype=np.uint8)

    assert scorer.read_region(frame, "score") == 1200
    assert scorer.delta(frame) == 1200
    assert scorer.total == 1200
    assert not scorer.is_terminal(frame)


def test_label_without_detectable_single_zero_means_zero():
    spec = load_game_spec("riders_republic")["scorer"]
    scorer = RidersRepublicScorer(
        spec,
        client_size=(1600, 895),
        ocr=lambda _frame: [_item("SCORE", [62, 47, 44, 18])],
    )
    frame = np.zeros((895, 1600, 3), dtype=np.uint8)

    assert scorer.read_region(frame, "score") == 0
    assert scorer.delta(frame) == 0
    assert not scorer.is_terminal(frame)


def test_localized_chinese_label_uses_the_same_score_region():
    spec = load_game_spec("riders_republic")["scorer"]
    scorer = RidersRepublicScorer(
        spec,
        client_size=(1600, 895),
        ocr=lambda _frame: [
            _item("得分", [62, 47, 44, 18]),
            _item("1200", [61, 76, 120, 52]),
        ],
    )
    frame = np.zeros((895, 1600, 3), dtype=np.uint8)

    assert scorer.read_region(frame, "score") == 1200


def _score_sequence(values):
    current = {"value": 0}

    def read(_frame):
        value = current["value"]
        if value is None:
            return []
        return [
            _item("SCORE", [62, 47, 44, 18]),
            _item(str(value), [61, 76, 120, 52]),
        ]

    scorer = RidersRepublicScorer(
        load_game_spec("riders_republic")["scorer"],
        client_size=(1600, 895), ocr=read,
    )
    observed = []
    for index, value in enumerate(values, 1):
        current["value"] = value
        frame = np.zeros((895, 1600, 3), dtype=np.uint8)
        frame[40, 50, 0] = index
        observed.append((scorer.delta(frame), scorer.total))
    return observed


def test_single_large_ocr_spike_does_not_replace_the_game_total():
    assert _score_sequence([1200, 900000, 1200]) == [
        (1200, 1200), (0, 1200), (0, 1200),
    ]


def test_persistent_large_game_total_is_confirmed_by_the_next_read():
    assert _score_sequence([1200, 900000, 900000]) == [
        (1200, 1200), (0, 1200), (898800, 900000),
    ]


def test_missing_hud_breaks_large_jump_confirmation():
    assert _score_sequence([1200, 900000, None, 900000]) == [
        (1200, 1200), (0, 1200), (0, 1200), (0, 1200),
    ]


def _write_options(documents: Path) -> Path:
    path = documents / "My Games" / "Riders Republic" / "Default" / "options.ini"
    path.parent.mkdir(parents=True)
    path.write_text(
        "[Language]\n"
        "text=zh-CN\nsubtitles=zh-CN\nsound=en-US\nuplay=zh-CN\n"
        "[Display Settings]\n"
        "WindowMode=1\nWindowedWidth=1280\nWindowedHeight=720\n"
        "[Music]\nvolume=0.00000\n",
        encoding="utf-8",
    )
    return path


def test_force_riders_republic_english_and_geometry(tmp_path):
    path = _write_options(tmp_path)

    result = game_settings.force_riders_republic_settings(
        (1600, 895), "Windowed", documents_dir=tmp_path)
    text = path.read_text(encoding="utf-8")

    assert "text=en-US" in text
    assert "subtitles=en-US" in text
    assert "sound=en-US" in text
    assert "uplay=en-US" in text
    assert "WindowMode=0" in text
    assert "WindowedWidth=1600" in text
    assert "WindowedHeight=895" in text
    assert "volume=0.00000" in text
    assert path.with_suffix(".ini.orig").is_file()
    assert "EN" in result


def test_force_riders_republic_settings_is_idempotent(tmp_path):
    _write_options(tmp_path)
    force = game_settings.FORCERS["riders_republic"]
    force((1600, 895), "Windowed", documents_dir=tmp_path)

    assert force((1600, 895), "Windowed", documents_dir=tmp_path) == "already Windowed 1600x895, EN"
