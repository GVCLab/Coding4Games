from functools import partial
from types import SimpleNamespace

import pytest
from PIL import Image

from coding4game_gui.benchmark.windows.driver_env import Env
from coding4game_gui.benchmark.windows import games_config


@pytest.fixture
def load_driver_metadata(monkeypatch):
    """Use registered driver metadata in tests that never prepare game state."""
    loader = partial(games_config.load_game_config, validate_files=False)
    monkeypatch.setattr(games_config, "load_game_config", loader)


@pytest.fixture
def make_driver_env():
    """Exercise the real pulse controller with in-memory Windows driver doubles."""
    def make(*, use_speedhack=True):
        held = {}
        input_driver = SimpleNamespace(
            apply_state=lambda index, **state: held.__setitem__(index, state),
            reset_pad=lambda index: held.pop(index, None),
            last_state=lambda index: held.get(index, {"buttons": []}),
            reset=held.clear,
            close=lambda: None,
        )
        renderer = SimpleNamespace(
            bbox=(0, 0, 64, 64),
            screenshot=lambda: Image.new("RGB", (64, 64)),
            close=lambda: None,
        )
        timectl = SimpleNamespace(
            available=use_speedhack,
            pause=lambda: None,
            unpause=lambda: None,
        )
        return Env(input_=input_driver, renderer=renderer, timectl=timectl,
                   lifecycle=SimpleNamespace(suspend=lambda: True))

    return make
