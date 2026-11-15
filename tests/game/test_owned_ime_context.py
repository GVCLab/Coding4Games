import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from coding4game_gui.benchmark.windows.drivers.windows_xinput import ime_context as ime
from coding4game_gui.benchmark.windows.games_config import parse_game_config


def test_mode_is_explicit_and_invalid_values_are_rejected():
    data = dict(process='Demo.exe', actions={'idle': {'description': 'Neutral'}})
    assert parse_game_config(data).ime_context_mode == 'system'
    assert parse_game_config(data | {'ime_context_mode': 'disabled'}).ime_context_mode == 'disabled'
    for invalid in (None, '', True, 'global', 'Disabled'):
        with pytest.raises(ValueError, match='ime_context_mode'):
            parse_game_config(data | {'ime_context_mode': invalid})


def test_shipped_component_manifest_matches_sources():
    manifest = json.loads((ime._NATIVE / 'manifest.json').read_text())
    assert manifest['architecture'] == 'x86'
    for name in ('owned_ime_context.c', 'OwnedImeController.cs'):
        assert hashlib.sha256((ime._NATIVE / name).read_bytes()).hexdigest() == manifest['sha256'][name]


def test_local_native_components_match_manifest():
    binaries = ('OwnedImeController.exe', 'owned_ime_context32.dll')
    if not all((ime._NATIVE / name).is_file() for name in binaries):
        pytest.skip("Local x86 IME components have not been built")
    assert all(path.is_file() for path in ime._verified_components())


def test_component_tampering_is_rejected_before_native_work(tmp_path, monkeypatch):
    monkeypatch.setattr(ime, '_NATIVE', tmp_path)
    (tmp_path / 'manifest.json').write_text(json.dumps({'sha256': {'OwnedImeController.exe': '0' * 64}}))
    (tmp_path / 'OwnedImeController.exe').write_bytes(b'changed')
    with pytest.raises(RuntimeError, match='identity differs'):
        ime._verified_components()


@pytest.mark.parametrize('arch,paused', [('x64', False), ('x86', True)])
def test_unsupported_or_frozen_target_never_starts_helper(monkeypatch, arch, paused):
    inspect = Mock(side_effect=AssertionError('unsupported target inspected'))
    monkeypatch.setattr(ime, '_target', inspect)
    with pytest.raises(RuntimeError, match='live x86'):
        ime.prepare_owned_ime_context(SimpleNamespace(hard_pause=paused, game_arch=arch), 'Demo.exe')
    inspect.assert_not_called()


@pytest.mark.parametrize('failure', [None, 'hook', 'owner', 'layout', 'timeout'])
def test_request_requires_cleanup_identity_and_layout_without_retry(monkeypatch, tmp_path, failure):
    target = dict(hwnd=12, pid=42, tid=7, created=123.5, process_name='Demo.exe',
                  window_class='Demo', game_hkl=0x8040804, host_hkl=0x8040804)
    helper, library = tmp_path / 'helper.exe', tmp_path / 'helper.dll'
    helper.write_bytes(b'helper')
    library.write_bytes(b'library')
    monkeypatch.setattr(ime, '_verified_components', lambda: (helper, library))
    inspect = Mock(side_effect=[target, target | ({'game_hkl': 0x4110411} if failure == 'layout' else {})])
    monkeypatch.setattr(ime, '_target', inspect)
    events = []
    monkeypatch.setattr(ime, 'emit', lambda *a, **k: events.append(k))

    def execute(args, **options):
        assert options['timeout'] == 3 and options['capture_output']
        assert args[1:4] == ['12', '42', 'Demo']
        assert args[7:] == ['123.500000', 'Demo.exe']
        if failure == 'timeout':
            raise ime.subprocess.TimeoutExpired(args, 3)
        Path(args[6]).write_text(json.dumps(dict(
            pid=43 if failure == 'owner' else 42, tid=7, hwnd=12,
            status='verified', state=1, hook_removed=failure != 'hook', layout_preserved=True)))
        return SimpleNamespace(returncode=0)

    execute = Mock(side_effect=execute)
    monkeypatch.setattr(ime.subprocess, 'run', execute)
    if failure:
        with pytest.raises((RuntimeError, ime.subprocess.TimeoutExpired)):
            ime._request(target, 1, foreground=True)
        assert events[-1]['status'] == 'failed'
    else:
        assert ime._request(target, 1, foreground=True)['native']['hook_removed']
    assert execute.call_count == 1


@pytest.mark.parametrize('mode', ['open_loop', 'closed_loop'])
def test_declared_mode_prepares_after_menu_before_freeze(monkeypatch, mode):
    from coding4game_gui.benchmark.windows import auto_nav, games_config
    from coding4game_gui.benchmark.windows.env import ResetController
    cfg = SimpleNamespace(process='Demo.exe', menu_skip=[], ime_context_mode='disabled')
    monkeypatch.setattr(games_config, 'load_game_config', lambda name: cfg)
    controller = ResetController({'driver_game': 'demo', 'startup': {'menu_skip_replay': mode}})
    calls = []
    monkeypatch.setattr(controller, '_force_foreground', lambda env: True)
    monkeypatch.setattr(auto_nav, 'run_pad_sequence', lambda *a, **k: calls.append('menu'))
    monkeypatch.setattr(ime, 'prepare_owned_ime_context',
                        lambda env, name: calls.append(('ime', env.hard_pause)))
    env = SimpleNamespace(hard_pause=True, screenshot_backend_name='dxcam',
                          unpause=lambda: None, pause=lambda: calls.append('freeze'),
                          render=lambda: SimpleNamespace(size=(2, 2)))
    controller.replay_menu_skip(env)
    assert calls[-2:] == [('ime', False), 'freeze']
    if mode == 'open_loop':
        assert calls[-3] == 'menu'


def test_shutdown_restores_after_existing_resume_before_owned_destruction(monkeypatch):
    from coding4game_gui.benchmark.windows import save_state
    from coding4game_gui.benchmark.windows.env import ResetController
    controller = object.__new__(ResetController)
    calls = []
    controller.cfg = SimpleNamespace(process='Demo.exe')
    controller._env = SimpleNamespace(game_pid=42, _owned_ime_context={'pid': 42},
        unpause=lambda: calls.append('resume'), close=lambda: calls.append('close'))
    monkeypatch.setattr(controller, 'alive', lambda: True)
    monkeypatch.setattr(ime, 'restore_owned_ime_context', lambda env: calls.append('restore'))
    monkeypatch.setattr(save_state, 'kill_process', lambda name: calls.append('destroy'))
    controller.shutdown()
    assert calls == ['resume', 'restore', 'close', 'destroy']


def test_system_mode_has_no_helper_side_effects(monkeypatch):
    from coding4game_gui.benchmark.windows.env import ResetController
    controller = object.__new__(ResetController)
    controller.cfg = SimpleNamespace(process='Demo.exe', ime_context_mode='system')
    monkeypatch.setattr(controller, '_force_foreground', lambda env: True)
    prepare = Mock(side_effect=AssertionError('unconfigured native operation'))
    monkeypatch.setattr(ime, 'prepare_owned_ime_context', prepare)
    controller._ensure_menu_handoff_foreground(SimpleNamespace(screenshot_backend_name='dxcam'))
    prepare.assert_not_called()
