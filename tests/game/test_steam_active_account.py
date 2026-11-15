import datetime

import pytest

from coding4game_gui.benchmark.windows import game_settings, steam_paths


@pytest.fixture
def session(tmp_path, monkeypatch):
    root = tmp_path / 'Steam'
    (root / 'logs').mkdir(parents=True)
    started = datetime.datetime(2026, 10, 30, 3, 0).timestamp()
    monkeypatch.setattr(steam_paths, '_registry_active_account_id', lambda: None)
    monkeypatch.setattr(steam_paths, 'steam_install_roots', lambda: [root])
    monkeypatch.setattr(steam_paths, '_steam_client_start_times', lambda _root: [started])
    return root, root / 'logs/connection_log.txt'


def test_logged_on_live_client_resolves_and_only_clears_its_brotato_run(session, tmp_path, monkeypatch):
    _root, log = session
    log.write_text('[2026-10-30 03:01:00] [Logged On, 4, 1] [U:1:42] RecvMsgClientLogOnResponse() : processing complete\n')
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    account = tmp_path / 'Roaming/Brotato/76561197960265770'
    account.mkdir(parents=True)
    (account / 'run_v3_0.json').write_text('run')
    (account / 'save_v3_0.json').write_text('progression')
    assert steam_paths.active_steam_account_id() == 42
    assert steam_paths.steam_active_userdata_root() == _root / 'userdata/42'
    assert game_settings.clear_brotato_saved_run()
    assert not (account / 'run_v3_0.json').exists()
    assert (account / 'save_v3_0.json').read_text() == 'progression'


@pytest.mark.parametrize('ending', [
    '[2026-10-30 03:02:00] [Logged On, 4, 1] [U:1:42] RecvMsgClientLoggedOff(\'Service Unavailable\')\n',
    '[2026-10-30 03:02:00] [Logged On, 4, 1] [U:1:42] AsyncDisconnect( bDontWaitOnTCPShutdown: false )\n',
    '[2026-10-30 03:02:00] [Logging On, 4, 2] [U:1:99] logon request sent\n',
])
def test_newer_disconnect_or_login_in_progress_rejects_old_identity(session, ending):
    _root, log = session
    log.write_text('[2026-10-30 03:01:00] [Logged On, 4, 1] [U:1:42] RecvMsgClientLogOnResponse() : processing complete\n' + ending)
    assert steam_paths.active_steam_account_id() is None


@pytest.mark.parametrize('starts', [[], [1, 2]])
def test_absent_or_ambiguous_live_clients_do_not_select_cached_account(session, monkeypatch, starts):
    _root, log = session
    log.write_text('[2026-10-30 03:01:00] [Logged On, 4, 1] [U:1:42] RecvMsgClientLogOnResponse() : processing complete\n')
    monkeypatch.setattr(steam_paths, '_steam_client_start_times', lambda _root: starts)
    assert steam_paths.active_steam_account_id() is None


def test_log_from_before_current_process_is_rejected(session):
    _root, log = session
    log.write_text('[2026-10-29 03:01:00] [Logged On, 4, 1] [U:1:42] RecvMsgClientLogOnResponse() : processing complete\n')
    assert steam_paths.active_steam_account_id() is None


def test_latest_completed_login_owns_identity_and_incomplete_line_cannot_override(session):
    _root, log = session
    log.write_text('[2026-10-30 03:01:00] [Logged On, 4, 1] [U:1:42] RecvMsgClientLogOnResponse() : processing complete\n'
                   '[2026-10-30 03:02:00] [Logged On, 4, 2] [U:1:99] RecvMsgClientLogOnResponse() : processing complete\n')
    assert steam_paths.active_steam_account_id() == 99
    log.write_text(log.read_text() + '[2026-10-30 03:03:00] [Logging On, 4, 3] [U:1:123]')
    assert steam_paths.active_steam_account_id() is None
