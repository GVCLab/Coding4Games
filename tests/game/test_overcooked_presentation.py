"""Only display preferences change; these fakes never touch the native registry."""
from types import SimpleNamespace
import sys

import pytest

from coding4game_gui.benchmark.windows.game_settings import prepare_overcooked_preferences, force_overcooked_settings


def registry(monkeypatch, fail=False):
    values = {'Screenmanager Resolution Width_h182942802': 2560,
              'Screenmanager Resolution Height_h2627697771': 1600,
              'Screenmanager Is Fullscreen mode_h3981298716': 1,
              'UntouchedProgress': 123}
    class Key:
        def __enter__(self): return self
        def __exit__(self, *args): return False
    failed = []
    def set_value(key, name, reserved, kind, value):
        if fail and not failed and 'Height' in name and value == 1200:
            failed.append(True)
            raise OSError('registry write failed')
        assert reserved == 0 and kind == 4
        values[name] = value
    fake = SimpleNamespace(HKEY_CURRENT_USER=1, KEY_QUERY_VALUE=1, KEY_SET_VALUE=2, REG_DWORD=4,
        OpenKey=lambda *a: Key(), QueryValueEx=lambda key, name: (values[name], 4),
        SetValueEx=set_value)
    monkeypatch.setitem(sys.modules, 'winreg', fake)
    monkeypatch.setattr('psutil.process_iter', lambda *a: [])
    return values


def test_only_three_measured_display_values_are_written(monkeypatch):
    values = registry(monkeypatch)
    result = prepare_overcooked_preferences((1920, 1200))
    assert result['before']['Screenmanager Is Fullscreen mode_h3981298716'] == 1
    assert result['after']['Screenmanager Is Fullscreen mode_h3981298716'] == 0
    assert values['UntouchedProgress'] == 123 and len(values) == 4


def test_partial_write_failure_restores_all_original_values(monkeypatch):
    values = registry(monkeypatch, fail=True)
    before = dict(values)
    with pytest.raises(OSError, match='registry write failed'):
        prepare_overcooked_preferences((1920, 1200))
    assert values == before


def test_packaged_caller_delegates_to_existing_native_worker(monkeypatch, tmp_path):
    from coding4game_gui.benchmark.windows import native_profile_io as native
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setattr(native, 'needs_native_io', lambda *a: True)
    calls = []
    monkeypatch.setattr(native, 'native_call', lambda op, payload:
        calls.append((op, payload)) or {'description': 'Windowed 1920x1200'})
    assert force_overcooked_settings((1920, 1200)) == 'Windowed 1920x1200'
    assert calls == [('overcooked_presentation', {'size': [1920, 1200], 'mode': 'Windowed'})]
