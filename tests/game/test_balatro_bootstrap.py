"""A fresh user profile must not fail the pre-launch tutorial check."""
from coding4game_gui.benchmark.windows import game_settings as settings
from coding4game_gui.benchmark.windows.settings_session import benchmark_settings, restore
import json
import pytest
from types import SimpleNamespace
from pathlib import Path


def test_fresh_profile_bootstraps_without_personal_settings(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    settings.force_balatro_window((1600, 900))
    settings.verify_balatro_tutorial_disabled()
    text = settings._read(tmp_path / 'Balatro/settings.jkr')
    assert '["screenmode"]="Windowed"' in text
    assert '["w"]=1600,["h"]=900' in text
    assert '["language"]="en-us"' in text


@pytest.mark.parametrize('existing', [False, True])
def test_session_restores_original_even_after_failure(tmp_path, monkeypatch, existing):
    monkeypatch.setenv('APPDATA', str(tmp_path / 'appdata'))
    monkeypatch.setattr('coding4game_gui.benchmark.windows.save_state.kill_process', lambda _: None)
    exe = tmp_path / 'game.exe'
    exe.write_bytes(b'game fixture')
    monkeypatch.setattr('coding4game_gui.benchmark.windows.games_config.load_game_config',
                        lambda _: SimpleNamespace(process='fixture.exe',
                            auto_launch=SimpleNamespace(launch_path=str(exe))))
    root = tmp_path / 'appdata/Balatro'
    if existing:
        root.mkdir(parents=True)
        (root / 'settings.jkr').write_bytes(b'personal preferences')
        (root / '1').mkdir()
        (root / '1/save.jkr').write_bytes(b'personal run')
    spec = {'driver_game': 'balatro', 'startup': {'settings_template': 'balatro-v1'},
            'scorer': {'window_setup': {'size': [1600, 900], 'mode': 'Windowed'}}}
    evidence = tmp_path / 'evidence'
    with pytest.raises(ValueError, match='simulated'):
        with benchmark_settings(spec, evidence):
            settings.verify_balatro_tutorial_disabled()
            (root / 'generated.jkr').write_bytes(b'benchmark output')
            with pytest.raises(RuntimeError, match='active or interrupted'):
                with benchmark_settings(spec, tmp_path / 'second'):
                    pass
            raise ValueError('simulated startup failure')
    state = json.loads((evidence / 'settings-session.json').read_text())
    assert state['status'] == 'restored'
    assert not (root / 'generated.jkr').exists()
    if existing:
        assert (root / 'settings.jkr').read_bytes() == b'personal preferences'
        assert (root / '1/save.jkr').read_bytes() == b'personal run'
    else:
        assert not root.exists()
    # Recover an interruption after the restored journal but before unlock.
    Path(state['lock']).write_text(str((evidence / 'settings-session.json').resolve()))
    restore(evidence / 'settings-session.json')
    assert not Path(state['lock']).exists()
