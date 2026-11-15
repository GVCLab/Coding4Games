"""A loading HUD must not authorize entrance input before the scene renders."""
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from coding4game_gui.benchmark.windows import env as module
from coding4game_gui.benchmark.windows.games_config import _parse_pad_action_entry

FIXTURES = Path(__file__).parents[1] / "fixtures/palworld_loading"


def test_real_loading_frame_with_entrance_text_keeps_waiting(monkeypatch):
    clock = [0.0]
    captures = []
    reads = []
    frames = iter([Image.open(FIXTURES / "loading.png"),
                   Image.open(FIXTURES / "rendered.png")])
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))

    def render():
        captures.append(clock[0])
        return next(frames)

    reader = SimpleNamespace(check_text=lambda *_a: reads.append(clock[0]) or True)
    assert module._wait_until_text(SimpleNamespace(render=render), "进入地下城", reader,
                                   settle=1, min_scene_luma=30)
    assert captures == [1, 1.4]
    assert reads == [1.4]


@pytest.mark.parametrize("value", [0, -1, 256, True, float("nan"), float("inf"), "30"])
def test_scene_floor_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="min_scene_luma"):
        _parse_pad_action_entry({"expect": "Ready", "min_scene_luma": value}, "wait", "menu")


@pytest.mark.parametrize("extra", [{"buttons": ["X"]}, {"click": [1, 1]}, {"expect": None}])
def test_scene_floor_is_only_neutral_navigation_metadata(extra):
    raw = {"expect": "Ready", "min_scene_luma": 30, **extra}
    with pytest.raises(ValueError, match="min_scene_luma"):
        _parse_pad_action_entry(raw, "wait", "menu")
