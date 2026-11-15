"""The registered TrackMania finish survives OCR errors and clears on a new lap."""
import pytest

from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.scorers.race import RaceScorer

SPEC = load_game_spec("trackmania")["scorer"]


class TextScorer(RaceScorer):
    """Fed raw OCR strings, so a fixture can say exactly what a crop said."""

    def __init__(self, samples, spec=None):
        self._samples = samples
        self._i = 0
        super().__init__(spec or SPEC, (1280, 720), ocr=lambda crop: [])

    def _text_in(self, frame, name):
        text = self._samples[self._i].get(name, "")
        self._texts[name] = text
        return text

    def play(self):
        out = []
        for _ in self._samples:
            out.append(self.read(object()))
            self._i += 1
        return out


def _finish(clock):
    return {"clock": clock, "distance": "839m",
            "finish_clock": clock, "finish_banner": "FINISH"}


def _lap_to_the_line():
    frames = [{"clock": f"0:{t / 100:05.2f}", "distance": f"{d}m"}
              for t, d in ((5500, 800), (5600, 815), (5700, 830))]
    frames += [_finish("0:57.36")] * 4
    return frames


def test_a_garbage_odometer_frame_cannot_un_finish_a_lap():
    frames = _lap_to_the_line()
    frames.append({"clock": "0:57.36", "distance": "15:4m"})   # ep1's #647
    readings = TextScorer(frames).play()
    assert readings[-2].finished, "the lap did finish; check the fixture"
    assert readings[-1].finished, "one bad crop must not undo a finish"
    assert readings[-1].finish_time == pytest.approx(57.36)


def test_a_new_run_does_un_finish_it():
    """The one thing that legitimately clears a finish.

    The last frame is what makes this a test rather than a tautology, and the
    first version did not have it. It ended at 0:30.00, which is BELOW the
    57.36 finish, so it read as restarted whether the clearing latched or not
    — a red-team pass proved it passed identically with the latch replaced by
    `pass`. The clock has to climb back PAST the old finish time, because
    that is the case the latch exists for: without it the scorer starts
    reporting the previous lap's finish partway through the next lap.
    """
    frames = _lap_to_the_line()
    frames += [{"clock": "-0:01.55", "distance": "0m"}] * 2    # the countdown
    frames += [{"clock": "0:20.00", "distance": "300m"}]
    frames += [{"clock": "0:58.00", "distance": "800m"}]       # PAST 57.36
    readings = TextScorer(frames).play()
    assert readings[-2].finished is False, "a restarted race is not finished"
    assert readings[-1].finished is False, (
        "the old finish time must be FORGOTTEN, not merely skipped — a new "
        "lap climbs past it within seconds")


def test_one_low_clock_read_does_not_throw_away_a_finish():
    """The mirror image, and the more expensive direction to get wrong.

    This game's clock drops the minutes digit past a minute (a documented
    real capture), so a lap finishing at 1:05.42 can produce a single frame
    reading 0:05.42. Believing that means clearing a real finish PERMANENTLY,
    because the clearing latches: measured at 0.6000 -> 0.5357, a completed
    lap scored below the floor that exists to rank finishers above it.

    A restart must therefore be corroborated by the odometer — a new run
    starts at zero, and no single-field misread moves both — and debounced.
    """
    frames = [{"clock": f"1:{t / 100:05.2f}", "distance": f"{d}m"}
              for t, d in ((300, 800), (400, 815), (500, 830))]
    frames += [_finish("1:05.42")] * 4
    frames += [{"clock": "0:05.42", "distance": "839m"}]         # minutes lost
    frames += [{"clock": "", "distance": ""}] * 3                # cinematic
    readings = TextScorer(frames).play()
    assert readings[6].finished, "the lap did finish; check the fixture"
    assert readings[7].finished, "one dropped minutes digit is not a restart"
    assert all(r.finished for r in readings[-3:]), (
        "and the finish must survive into the replay, as it did before")


def test_the_retired_distance_gate_is_refused_loudly():
    """A scenario still declaring finish_distance_ratio must fail, not be
    silently ignored: it would read as a guard that is no longer applied."""
    with pytest.raises(ValueError, match="finish_distance_ratio is retired"):
        TextScorer([], spec=dict(SPEC, finish_distance_ratio=0.9))
