import pytest

from coding4game_gui.benchmark.spec import load_game_spec
from coding4game_gui.benchmark.windows.terminal import validate_terminal_detection


@pytest.mark.parametrize('enabled', ['false', 0, None])
def test_terminal_toggle_rejects_ambiguous_values(enabled):
    with pytest.raises(ValueError, match='must be a boolean'):
        validate_terminal_detection({'lifecycle': {'terminal_detection': enabled}})


def test_terminal_detection_can_be_explicitly_disabled():
    assert validate_terminal_detection({'lifecycle': {'terminal_detection': False}}) is False
