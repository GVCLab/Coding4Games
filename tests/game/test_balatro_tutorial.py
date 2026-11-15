"""Tutorial preparation must succeed explicitly, never just print success."""
import pytest
from coding4game_gui.benchmark.windows import game_settings as settings


@pytest.mark.parametrize('field', ['', '["tutorial_complete"] = false,'])
def test_tutorial_flag_missing_or_spaced(tmp_path, monkeypatch, field):
    root = tmp_path/'Balatro'
    root.mkdir()
    path = root/'settings.jkr'
    settings._write(path, 'return {'+field+'["other"]=42,}')
    monkeypatch.setenv('APPDATA', str(tmp_path))
    settings.force_balatro_window((1600,900))
    assert '["tutorial_complete"]=true' in settings._read(path)


def test_only_top_level_tutorial_state_changes():
    other = '["other"]={["tutorial_complete"]=false,["text"]="a,}b"}'
    text = 'return {' + other + ',["tutorial_progress"]={["nested"]={1,2,},},["tutorial_complete"]=false,}'
    result = settings.normalize_balatro_tutorial(text)
    assert other in result
    assert 'tutorial_progress' not in result
    assert result.endswith('["tutorial_complete"]=true,}')
    assert settings.normalize_balatro_tutorial(result) == result


@pytest.mark.parametrize('text', ['bad', 'return {["x"]={}', 'return {["x"]="bad}'])
def test_malformed_settings_rejected(text):
    with pytest.raises(ValueError):
        settings.normalize_balatro_tutorial(text)


def test_readback_catches_failed_persistence(tmp_path, monkeypatch):
    root = tmp_path/'Balatro'
    root.mkdir()
    path = root/'settings.jkr'
    settings._write(path, 'return {["tutorial_complete"]=false,}')
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setattr(settings, '_write', lambda *args: None)
    with pytest.raises(RuntimeError, match='not disabled'):
        settings.force_balatro_window((1600,900))
