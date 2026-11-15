"""Verify temporary player-save preservation without launching native software."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from coding4game_gui.benchmark.windows.save_session import begin_save_session, benchmark_save, restore, tree_manifest


def protection(monkeypatch, tmp_path):
    source = tmp_path / 'player' / 'account'
    cfg = SimpleNamespace(process='test-never-launched.exe', save_state=object())
    monkeypatch.setattr('coding4game_gui.benchmark.windows.games_config.load_game_config', lambda _: cfg)
    monkeypatch.setattr('coding4game_gui.benchmark.windows.save_state.resolve_for_run',
                        lambda *args: SimpleNamespace(restore_to=source))
    monkeypatch.setattr('coding4game_gui.benchmark.windows.save_state.kill_process', lambda _: None)
    spec = dict(driver_game='fixture', startup=dict(
        golden_checkpoint='golden', preserve_player_save=True))
    return source, spec


def test_existing_save_is_backed_up_and_restored_after_failure(monkeypatch, tmp_path):
    source, spec = protection(monkeypatch, tmp_path)
    source.mkdir(parents=True)
    (source / 'slot.save').write_bytes(b'original player data')
    (source / 'empty').mkdir()
    original = tree_manifest(source)
    with pytest.raises(RuntimeError, match='native startup failed'):
        with benchmark_save(spec, tmp_path / 'evidence') as state:
            assert tree_manifest(Path(state['backup'])) == original
            (source / 'slot.save').write_bytes(b'generated benchmark data')
            raise RuntimeError('native startup failed')
    assert tree_manifest(source) == original
    assert (tmp_path / 'evidence/generated-save/slot.save').read_bytes() == b'generated benchmark data'
    assert state['status'] == 'restored_verified'
    assert not Path(state['lock']).exists()
    assert restore(tmp_path / 'evidence/save-session.json')['status'] == 'restored_verified'


def test_original_absence_is_restored_and_generated_data_retained(monkeypatch, tmp_path):
    source, spec = protection(monkeypatch, tmp_path)
    with benchmark_save(spec, tmp_path / 'evidence') as state:
        assert state['original_manifest']['kind'] == 'absent'
        source.mkdir()
        (source / 'new.save').write_bytes(b'new data')
    assert not source.exists()
    assert (tmp_path / 'evidence/generated-save/new.save').read_bytes() == b'new data'


@pytest.mark.skipif(os.name != 'nt', reason='Windows extended local paths')
@pytest.mark.parametrize('owner_extended,request_extended', [(True, False), (False, True)])
def test_manual_recovery_releases_same_journal_with_alternate_windows_spelling(
        monkeypatch, tmp_path, owner_extended, request_extended):
    source, spec = protection(monkeypatch, tmp_path)
    source.mkdir(parents=True)
    (source / 'slot.save').write_bytes(b'original player data')
    state = begin_save_session(spec, tmp_path / 'evidence')
    journal = (tmp_path / 'evidence/save-session.json').resolve()
    ordinary = str(journal)
    extended = '\\\\?\\' + ordinary
    lock = Path(state['lock'])
    lock.write_text(extended if owner_extended else ordinary, encoding='utf-8')
    (source / 'slot.save').write_bytes(b'generated benchmark data')
    result = restore(Path(extended) if request_extended else journal)
    assert result['status'] == 'restored_verified'
    assert (source / 'slot.save').read_bytes() == b'original player data'
    assert not lock.exists()


@pytest.mark.parametrize('owner', ['other-session.json', 'invalid\x00owner'])
def test_manual_recovery_preserves_an_unrelated_or_invalid_lock_owner(monkeypatch, tmp_path, owner):
    source, spec = protection(monkeypatch, tmp_path)
    state = begin_save_session(spec, tmp_path / 'evidence')
    lock = Path(state['lock'])
    lock.write_text(owner, encoding='utf-8')
    assert restore(tmp_path / 'evidence/save-session.json')['status'] == 'restored_verified'
    assert lock.read_text(encoding='utf-8') == owner


def test_recovery_rejects_unrelated_move_target(monkeypatch, tmp_path):
    import json
    source, spec = protection(monkeypatch, tmp_path)
    with benchmark_save(spec, tmp_path / 'evidence'):
        pass
    journal = tmp_path / 'evidence/save-session.json'
    state = json.loads(journal.read_text())
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    state['retained'] = str(unrelated)
    journal.write_text(json.dumps(state))
    with pytest.raises(RuntimeError, match='unsafe save recovery paths'):
        restore(journal)
    assert unrelated.is_dir()


def test_late_steam_marker_is_preserved_before_restoring_absence(monkeypatch, tmp_path):
    source, spec = protection(monkeypatch, tmp_path)
    # Exercise the actual recovery phase after a platform has recreated a
    # directory whose original state was absent.
    with benchmark_save(spec, tmp_path / 'evidence'):
        pass
    import json
    journal = tmp_path / 'evidence/save-session.json'
    state = json.loads(journal.read_text())
    source = source.with_name('76561199470060039')
    state.update(source=str(source), status='archived',
        retained=str(source.with_name(source.name + '.c4g-generated-fixture')),
        restore_temporary=str(source.with_name(source.name + '.c4g-restore-fixture')))
    source.mkdir()
    marker = b'"steam_autocloud.vdf"\n{\n "accountid" "1509794311"\n}\n'
    (source / 'steam_autocloud.vdf').write_bytes(marker)
    journal.write_text(json.dumps(state))
    restored = restore(journal)
    assert restored['status'] == 'restored_verified' and not source.exists()
    assert (tmp_path / 'evidence/platform-generated-save/steam_autocloud.vdf').read_bytes() == marker


def test_recreated_game_data_is_never_removed(monkeypatch, tmp_path):
    import json
    source, spec = protection(monkeypatch, tmp_path)
    with benchmark_save(spec, tmp_path / 'evidence'):
        pass
    journal = tmp_path / 'evidence/save-session.json'
    state = json.loads(journal.read_text())
    state['status'] = 'archived'
    source.mkdir()
    (source / 'player.save').write_bytes(b'external player data')
    journal.write_text(json.dumps(state))
    with pytest.raises(RuntimeError, match='non-platform data'):
        restore(journal)
    assert (source / 'player.save').read_bytes() == b'external player data'


def test_redirected_save_session_delegates_before_local_copy(monkeypatch, tmp_path):
    source, spec = protection(monkeypatch, tmp_path)
    spec['runtime'] = {'scenario': 'registered-fixture', 'windows_input': 'gamepad'}
    from coding4game_gui.benchmark.windows import native_profile_io as native
    monkeypatch.setattr(native, 'needs_native_io', lambda path: True)
    calls = []
    journal = tmp_path / 'native-journal.json'
    state = {'status': 'captured', 'journal': str(journal), 'native_io': {'method': 'desktop_shell'}}
    def call(operation, payload):
        calls.append((operation, payload))
        if operation == 'begin_save_session':
            import json
            journal.write_text(json.dumps(state))
            return dict(state)
        return dict(state, status='restored_verified')
    monkeypatch.setattr(native, 'native_call', call)
    with benchmark_save(spec, tmp_path / 'evidence') as result:
        assert result['status'] == 'captured'
        assert not source.exists() and not (tmp_path / 'evidence').exists()
    assert result['status'] == 'restored_verified'
    assert [x[0] for x in calls] == ['begin_save_session', 'restore_save_session']
