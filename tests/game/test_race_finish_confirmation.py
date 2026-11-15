"""Current raw-keyboard terminal behavior against native human/pause OCR."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from coding4game_gui.benchmark.windows.scorers.race import RaceScorer

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/trackmania_human_and_pause_20261023.json"


def current_spec():
    return yaml.safe_load((ROOT / "coding4game_gui/benchmark/scenarios/trackmania.yaml").read_text(
        encoding="utf-8"))["scorer"]


class RecordedScorer(RaceScorer):
    def __init__(self, texts):
        self.texts = texts
        self.index = 0
        super().__init__(current_spec(), (1280, 720), ocr=lambda _crop: [])

    def _text_in(self, frame, name):
        value = self.texts[self.index].get(name, "")
        self._texts[name] = value
        return value

    def replay(self):
        rows = []
        total = 0.0
        for self.index in range(len(self.texts)):
            frame = object()
            total += self.delta(frame)
            # Production asks all three questions about each array. Repeated
            # questions must not advance finish confirmation on the same frame.
            terminal = self.is_terminal(frame)
            phase = self.classify_phase(frame)
            assert self.delta(frame) == 0
            rows.append((terminal, phase, total, self.last_reading.finish_time))
        return rows


@pytest.fixture(scope="module")
def recorded():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_native_human_lap_keeps_its_time_score_and_latched_finish(recorded):
    rows = RecordedScorer(recorded["human"]["texts"]).replay()
    first = next(i for i, row in enumerate(rows) if row[0])
    assert first == recorded["human"]["first_terminal_frame"]
    assert first >= 321  # The native recording crosses the line at frame 321.
    assert all(not row[0] for row in rows[:first])
    expected = .6 + .4 * 28.61 / 37.26
    for terminal, phase, total, seconds in rows[first:]:
        assert terminal and phase == "finished"
        assert seconds == 37.26
        assert total == pytest.approx(expected)


def test_native_pause_does_not_turn_stationary_play_into_a_finish(recorded):
    rows = RecordedScorer(recorded["pause"]["texts"]).replay()
    assert all(not row[0] and row[2] == 0 for row in rows)


def test_finish_corroboration_expires_when_the_clock_advances():
    crossing = dict(clock="0:37.26", distance="1460m", track="A03-Race",
                    finish_clock="0:37.26", finish_banner="FINISH")
    later = dict(crossing, clock="0:38.00")
    rows = RecordedScorer([crossing] + [copy.deepcopy(later) for _ in range(5)]).replay()
    assert all(not row[0] for row in rows)


@pytest.mark.parametrize("overlay", [
    {"finish_clock": "0:37.26", "finish_banner": "Checkpoint"},
    {"finish_clock": "0:21.94", "finish_banner": "FINISH"},
    {"finish_clock": "", "finish_banner": "FINISH"},
])
def test_still_hud_needs_both_the_finish_banner_and_its_matching_time(overlay):
    # A checkpoint time, a missing time, and a mismatching earlier time cannot
    # corroborate a finish, regardless of how far the odometer has advanced.
    text = dict(clock="0:37.26", distance="1461m", track="A03-Race", **overlay)
    rows = RecordedScorer([copy.deepcopy(text) for _ in range(8)]).replay()
    assert all(not row[0] for row in rows)


@pytest.mark.parametrize("missing", ["finish_clock", "finish_banner"])
def test_partial_finish_calibration_fails_closed(missing):
    spec = current_spec()
    del spec["regions"][missing]
    with pytest.raises(ValueError, match="configured together"):
        RaceScorer(spec, (1280, 720), ocr=lambda _crop: [])


@pytest.mark.parametrize('offset', range(3))
def test_deferred_passive_samples_recover_human_finish_after_a_hidden_endpoint(recorded, offset):
    import numpy as np
    from coding4game_gui.benchmark.windows.deferred_score import DeferredScore

    class PixelIndexedScorer(RecordedScorer):
        def _text_in(self, frame, name):
            self.index = int(frame[0, 0, 0]) + 256 * int(frame[0, 0, 1])
            return super()._text_in(frame, name)

    texts = recorded['human']['texts']
    def replay(indices):
        scorer = PixelIndexedScorer(texts)
        pending = DeferredScore(scorer, retain_replay_evidence=True)
        for index in indices:
            frame = np.zeros((8, 8, 3), np.uint8)
            frame[0, 0] = (index % 256, index // 256, 0)
            pending.add(frame, tics=index)
            assert pending.value is None and scorer.last_reading.finish_time is None
        result = pending.settle(scorer)
        return result, pending.details, pending.selected_png

    assert replay([0, len(texts) - 1])[0] == 0.
    indices = sorted({0, len(texts) - 1, *range(3 + offset, len(texts), 3)})
    score, details, selected = replay(indices)
    assert score == pytest.approx(.6 + .4 * 28.61 / 37.26)
    assert details['reading']['finished'] and details['reading']['finish_time'] == 37.26
    assert details['frame_tics'] == indices and selected is not None
    assert 321 <= details['selected_frame_tic'] <= 350
