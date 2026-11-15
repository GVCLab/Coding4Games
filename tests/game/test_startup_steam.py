from pathlib import Path
import pytest


def test_steam_connection_state_uses_newest_authoritative_event(tmp_path):
    from coding4game_gui.benchmark.windows.startup import steam_connection_state

    log = tmp_path / "connection_log.txt"
    log.write_text(
        "RecvMsgClientLogOnResponse() : processing complete\n"
        "CCMInterface::OnNetworkDeviceStateChange -- Lost device\n"
        "CCMInterface - received heartbeat response, connection still good\n",
        encoding="utf-8",
    )
    assert steam_connection_state(log) == "online"

    with log.open("a", encoding="utf-8") as stream:
        stream.write("Sending SteamServersDisconnected_t\n")
    assert steam_connection_state(log) == "offline"


def test_steam_connection_state_missing_log_is_unknown(tmp_path):
    from coding4game_gui.benchmark.windows.startup import steam_connection_state

    assert steam_connection_state(tmp_path / "missing.txt") == "unknown"


def test_ensure_steam_logged_on_leaves_healthy_session_alone(monkeypatch, tmp_path):
    from coding4game_gui.benchmark.windows import startup

    steam = tmp_path / "steam.exe"
    steam.touch()
    monkeypatch.setattr(startup, "_steam_running", lambda _path: True)
    monkeypatch.setattr(startup, "steam_connection_state", lambda _path: "online")
    monkeypatch.setattr(
        startup.subprocess, "run",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not restart")),
    )

    assert startup.ensure_steam_logged_on(steam) == {
        "initial": "online", "refreshed": False, "final": "online"}


@pytest.mark.parametrize("initial", ["offline", "unknown"])
@pytest.mark.parametrize("reconnected", [False, True])
def test_existing_steam_waits_without_shutdown_or_restart(
        monkeypatch, tmp_path, initial, reconnected):
    from coding4game_gui.benchmark.windows import startup

    steam = tmp_path / "steam.exe"
    steam.touch()
    monkeypatch.setattr(startup, "_steam_running", lambda _path: True)
    monkeypatch.setattr(startup, "steam_connection_state", lambda _path: initial)
    waits = []

    def wait(path, expected, timeout):
        waits.append((path, expected, timeout))
        return reconnected

    def forbidden(*args, **kwargs):
        pytest.fail("existing Steam must never be shut down or relaunched")

    monkeypatch.setattr(startup, "_wait_steam_state", wait)
    monkeypatch.setattr(startup.subprocess, "run", forbidden)
    monkeypatch.setattr(startup.subprocess, "Popen", forbidden)
    if reconnected:
        assert startup.ensure_steam_logged_on(steam, timeout=7) == {
            "initial": initial, "refreshed": False, "final": "online"}
    else:
        with pytest.raises(RuntimeError, match="client was left running"):
            startup.ensure_steam_logged_on(steam, timeout=7)
    assert waits == [(str(steam.resolve()), "online", 7)]
