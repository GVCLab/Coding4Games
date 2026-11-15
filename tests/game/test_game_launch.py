from pathlib import Path
from types import SimpleNamespace

import pytest

from coding4game_gui.benchmark.windows.game_launch import DirectLaunchError
from coding4game_gui.benchmark.windows.game_launch import launch_game
from coding4game_gui.benchmark.windows.game_launch import resolve_launch_args
from coding4game_gui.benchmark.windows.game_launch import resolve_launch_path
from coding4game_gui.benchmark.windows.games_config import load_game_config, parse_game_config


@pytest.mark.parametrize('tail,tracked', [
    ('', True),
    ('[2026-10-26 21:08:50] AppID 714010 adding PID 52556 as a tracked process "game"\n', True),
    ('[2026-10-26 21:12:24] AppID 714010 no longer tracking PID 52556, exit code 15\n', True),
    ('[2026-10-26 21:12:24] Remove 714010 from running list\n', False),
    ('[2026-10-26 21:12:24] Remove 1714010 from running list\n', True),
])
def test_steam_running_list_waits_for_client_removal(tmp_path, tail, tracked):
    from coding4game_gui.benchmark.windows.game_launch import _steam_client_tracks_app
    logs = tmp_path / 'logs'
    logs.mkdir()
    (logs / 'gameprocess_log.txt').write_text(
        '[2026-10-26 21:00:00] AppID 714010 adding PID 10 as a tracked process "game"\n'
        + tail, encoding='utf-8')
    assert _steam_client_tracks_app('714010', str(tmp_path / 'steam.exe')) is tracked


def test_steam_running_list_missing_is_not_evidence_of_idle(tmp_path):
    from coding4game_gui.benchmark.windows.game_launch import _steam_client_tracks_app, SteamLaunchPendingError
    with pytest.raises(SteamLaunchPendingError, match='running-list'):
        _steam_client_tracks_app('714010', str(tmp_path / 'steam.exe'))


def _spec(path):
    return SimpleNamespace(launch_path=str(path) if path is not None else None)


def _exe(tmp_path, name="Demo.exe", folder="Game Folder"):
    executable = tmp_path / folder / name
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"MZ")
    return executable


def _record_popen(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "coding4game_gui.benchmark.windows.game_launch.subprocess.Popen",
        lambda *args, **kwargs: calls.append((args, kwargs)) or object(),
    )
    return calls


def test_direct_launcher_uses_exe_directory_as_working_directory(tmp_path, monkeypatch):
    executable = tmp_path / "Game Folder" / "Demo.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"MZ")
    calls = []
    monkeypatch.setattr(
        "coding4game_gui.benchmark.windows.game_launch.subprocess.Popen",
        lambda *args, **kwargs: calls.append((args, kwargs)) or object(),
    )

    launch_game(_spec(executable))

    assert calls == [
        (([str(executable)],), {"cwd": str(executable.parent), "close_fds": True})
    ]


def test_desktop_launch_passes_only_the_validated_executable_and_arguments(tmp_path, monkeypatch):
    executable = _exe(tmp_path)
    calls = _record_popen(monkeypatch)
    desktop_calls = []
    monkeypatch.setattr("coding4game_gui.benchmark.windows.game_launch._launch_desktop_executable",
                        lambda path, args: desktop_calls.append((path, args)))
    spec = SimpleNamespace(launch_path=str(executable), launch_method="desktop_shell",
                           launch_args=["argument with spaces", 'quote"inside'])
    assert launch_game(spec) is None
    assert desktop_calls == [(executable, spec.launch_args)] and not calls


def test_desktop_shell_preserves_cwd_quotes_and_com_lifetime(tmp_path, monkeypatch):
    import sys
    from coding4game_gui.benchmark.windows.game_launch import _launch_desktop_executable
    import subprocess
    events = []
    executable = _exe(tmp_path)
    arguments = ["space inside", 'embedded"quote']
    monkeypatch.setattr("coding4game_gui.benchmark.windows.game_launch.os", SimpleNamespace(name="nt"))
    monkeypatch.setitem(sys.modules, "pythoncom", SimpleNamespace(
        COINIT_APARTMENTTHREADED=2,
        CoInitializeEx=lambda value: events.append(("initialize", value)),
        CoUninitialize=lambda: events.append(("uninitialize",))))
    shell = SimpleNamespace(ShellExecute=lambda *args: events.append(("launch", args)))
    desktop = SimpleNamespace(Document=SimpleNamespace(Application=shell))
    windows = SimpleNamespace(FindWindowSW=lambda *args: desktop)
    client = SimpleNamespace(Dispatch=lambda name: SimpleNamespace(Windows=lambda: windows))
    monkeypatch.setitem(sys.modules, "win32com", SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    _launch_desktop_executable(executable, arguments)
    assert events == [("initialize", 2), ("launch", (
        str(executable), subprocess.list2cmdline(arguments), str(executable.parent), "open", 1)),
        ("uninitialize",)]


@pytest.mark.parametrize("method", ["unknown", True, None, {}])
def test_config_rejects_unknown_launch_methods(method):
    with pytest.raises(ValueError, match="launch_method"):
        parse_game_config({"name": "Demo", "process": "Demo.exe", "actions": {"neutral": {}},
                           "auto_launch": {"launch_path": "C:/Demo.exe", "launch_method": method}})


@pytest.mark.parametrize(
    "value, message",
    [
        (None, "launch_path is required"),
        ("steam://rungameid/1", "not a URI"),
        ("relative/Game.exe", "must be absolute"),
        (str(Path("C:/Games/readme.txt")), "must name a Windows .exe"),
    ],
)
def test_direct_launcher_rejects_non_executable_targets(value, message):
    with pytest.raises(DirectLaunchError, match=message):
        resolve_launch_path(_spec(value))


def test_game_config_rejects_legacy_steam_url():
    with pytest.raises(ValueError, match="steam_url is disabled"):
        parse_game_config({
            "name": "Demo",
            "process": "Demo.exe",
            "actions": {"confirm": {"buttons": ["A"]}},
            "auto_launch": {"steam_url": "steam://rungameid/1"},
        })


def test_aimlab_launches_through_the_steam_library(tmp_path, monkeypatch):
    cfg = load_game_config("aimlab", validate_files=False)
    assert Path(cfg.auto_launch.launch_path).name.lower() == "steam.exe"
    assert cfg.process.lower() == "aimlab_tb.exe"

    client = _exe(tmp_path, name="steam.exe", folder="Steam")
    monkeypatch.setattr(
        "coding4game_gui.benchmark.windows.game_launch.resolve_launch_path",
        lambda spec: client,
    )
    calls = _record_popen(monkeypatch)

    launch_game(cfg.auto_launch)

    assert calls == [
        (([str(client), "-applaunch", "714010"],),
         {"cwd": str(client.parent), "close_fds": True})
    ]


def test_launch_without_args_is_unchanged_by_the_new_field(tmp_path, monkeypatch):
    """Regression guard for the eight games that boot from their own exe: an
    absent, empty, or None launch_args must produce the exact same Popen call
    as before the field existed."""
    executable = _exe(tmp_path)
    for spec in (
        SimpleNamespace(launch_path=str(executable)),               # no attr
        SimpleNamespace(launch_path=str(executable), launch_args=None),
        SimpleNamespace(launch_path=str(executable), launch_args=[]),
    ):
        calls = _record_popen(monkeypatch)
        launch_game(spec)
        assert calls == [
            (([str(executable)],),
             {"cwd": str(executable.parent), "close_fds": True})
        ], spec


def test_launch_args_rejects_a_bare_string():
    with pytest.raises(DirectLaunchError, match="must be a list of strings"):
        resolve_launch_args(
            SimpleNamespace(launch_path="C:/x.exe",
                            launch_args="-applaunch 1551360"))


def test_launch_args_rejects_non_string_entries():
    with pytest.raises(DirectLaunchError, match="must be strings"):
        resolve_launch_args(
            SimpleNamespace(launch_path="C:/x.exe",
                            launch_args=["-applaunch", 1551360]))


def test_game_config_parses_launch_args():
    cfg = parse_game_config({
        "name": "Demo",
        "process": "Demo.exe",
        "actions": {"confirm": {"buttons": ["A"]}},
        "auto_launch": {
            "launch_path": "D:/App/STEAM/steam.exe",
            "launch_args": ["-applaunch", "1551360"],
        },
    })
    assert cfg.auto_launch.launch_args == ["-applaunch", "1551360"]


def test_game_config_defaults_launch_args_to_empty():
    cfg = parse_game_config({
        "name": "Demo",
        "process": "Demo.exe",
        "actions": {"confirm": {"buttons": ["A"]}},
        "auto_launch": {"launch_path": "C:/Games/Demo.exe"},
    })
    assert cfg.auto_launch.launch_args == []


@pytest.mark.parametrize("value", ["-applaunch 1551360", {"a": 1}, [1, 2]])
def test_game_config_rejects_malformed_launch_args(value):
    with pytest.raises(ValueError, match="launch_args"):
        parse_game_config({
            "name": "Demo",
            "process": "Demo.exe",
            "actions": {"confirm": {"buttons": ["A"]}},
            "auto_launch": {
                "launch_path": "C:/Games/Demo.exe",
                "launch_args": value,
            },
        })
