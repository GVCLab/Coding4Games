"""Native profile writes must be verified before save preparation."""
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from coding4game_gui.benchmark.windows import native_profile_io as native
from coding4game_gui.benchmark.windows import save_state


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16'])
def test_sekiro_display_inspection_preserves_bytes_and_limits_fields(tmp_path, encoding):
    xml = ('<?xml version="1.0" encoding="' + encoding + '"?>'
           '<config><ScreenMode>WINDOW</ScreenMode>'
           '<Resolution-WindowScreenWidth>2560</Resolution-WindowScreenWidth>'
           '<Resolution-WindowScreenHeight>1600</Resolution-WindowScreenHeight>'
           '<TextureQuality>MAX</TextureQuality></config>')
    path = tmp_path / 'GraphicsConfig.xml'
    original = xml.encode(encoding)
    path.write_bytes(original)

    result = native._inspect_sekiro_presentation(tmp_path)

    assert result['exists'] and result['root_tag'] == 'config'
    assert result['display_fields'] == {
        'ScreenMode': 'WINDOW',
        'Resolution-WindowScreenWidth': '2560',
        'Resolution-WindowScreenHeight': '1600',
    }
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_sekiro_display_inspection_does_not_create_a_missing_config(tmp_path):
    assert native._inspect_sekiro_presentation(tmp_path)['exists'] is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("launch_error", [False, True])
def test_native_call_initializes_com_on_each_request_thread(tmp_path, monkeypatch, launch_error):
    state = threading.local()
    events = []

    def initialize(model):
        assert model == 2
        state.initialized = True
        events.append("initialize")

    def uninitialize():
        assert state.initialized
        state.initialized = False
        events.append("uninitialize")

    def shell():
        if not getattr(state, "initialized", False):
            raise RuntimeError("CO_E_NOTINITIALIZED: request thread has no COM apartment")
        events.append("dispatch")
        return SimpleNamespace(ShellExecute=execute)

    def execute(*args):
        assert state.initialized
        events.append("execute")
        if launch_error:
            raise OSError("desktop worker launch failed")
        request = next((tmp_path / "out" / "native_profile_io").glob("*/request.json"))
        data = json.loads(request.read_text())
        request.with_name("response.json").write_text(json.dumps({
            "id": data["id"], "ok": True, "result": "prepared"}))

    monkeypatch.setitem(sys.modules, "pythoncom", SimpleNamespace(
        COINIT_APARTMENTTHREADED=2, CoInitializeEx=initialize, CoUninitialize=uninitialize))
    monkeypatch.setattr(native, "REPO_ROOT", tmp_path)
    (tmp_path / "pythonw.exe").touch()
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(native, "_desktop_shell", shell)
    result, errors = [], []

    def request():
        try:
            result.append(native.native_call("kingdom_presentation", {}, timeout=.1))
        except Exception as exc:
            errors.append(exc)
        assert not getattr(state, "initialized", False)

    thread = threading.Thread(target=request)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert events == ["initialize", "dispatch", "execute", "uninitialize"]
    if launch_error:
        assert len(errors) == 1 and str(errors[0]) == "desktop worker launch failed"
    else:
        assert errors == [] and result == ["prepared"]


def test_write_probe_detects_redirect_and_removes_only_its_probe(tmp_path, monkeypatch):
    existing = tmp_path / "slot_3.lua"
    existing.write_bytes(b"existing progress")
    monkeypatch.setattr(native, "_physical_path", lambda path: tmp_path / "overlay" / path.name)
    assert native._redirected_write(tmp_path)
    assert list(tmp_path.iterdir()) == [existing]
    assert existing.read_bytes() == b"existing progress"


def test_write_probe_also_cleans_up_on_path_inspection_failure(tmp_path, monkeypatch):
    def fail(path):
        raise OSError("cannot inspect destination")
    monkeypatch.setattr(native, "_physical_path", fail)
    with pytest.raises(OSError, match="cannot inspect"):
        native._redirected_write(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_redirected_restore_delegates_without_extracting_local_overlay(tmp_path, monkeypatch):
    spec = save_state.SaveStateSpec(tmp_path / "profile", golden=tmp_path / "golden.zip")
    monkeypatch.setattr(native, "needs_native_io", lambda directory: True)
    expected = {"extracted": True, "native_io": {"method": "desktop_shell"}}
    calls = []
    def restore(spec_arg, process):
        calls.append((spec_arg, process))
        return expected
    monkeypatch.setattr(native, "restore_native", restore)
    assert save_state.restore(spec, "test-game.exe") == expected
    assert calls == [(spec, "test-game.exe")]
    assert not spec.restore_to.exists()


def _request(tmp_path, operation, payload):
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"id": "test-request", "operation": operation,
                                "payload": payload}), encoding="utf-8")
    return path


def test_worker_refuses_remaining_virtualization_before_restore(tmp_path, monkeypatch):
    request = _request(tmp_path, "restore_save", {
        "spec": {"restore_to": str(tmp_path / "profile")}, "process": "test-game.exe"})
    monkeypatch.setattr(native, "_redirected_write", lambda directory: True)
    monkeypatch.setattr(save_state, "restore", lambda *args: pytest.fail("restore must not run"))
    native._run_request(request)
    result = json.loads((tmp_path / "response.json").read_text())
    assert result["id"] == "test-request"
    assert not result["ok"]
    assert "still redirects" in result["error"]


def test_worker_preserves_restore_spec_and_returns_native_receipt(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    golden = tmp_path / "golden.zip"
    request = _request(tmp_path, "restore_save", {
        "spec": {"restore_to": str(profile), "golden": str(golden),
                 "wipe_before": False, "extra_kill": ["helper.exe"]},
        "process": "test-game.exe"})
    monkeypatch.setattr(native, "_redirected_write", lambda directory: False)
    calls = []
    def restore(spec, process):
        assert native._IN_WORKER
        calls.append((spec, process))
        return {"extracted": True}
    monkeypatch.setattr(save_state, "restore", restore)
    native._run_request(request)
    result = json.loads((tmp_path / "response.json").read_text())
    assert result["ok"]
    assert result["result"]["native_io"]["verified_directory"] == str(profile)
    spec, process = calls[0]
    assert spec.restore_to == profile and spec.golden == golden
    assert spec.wipe_before is False and spec.extra_kill == ["helper.exe"]
    assert process == "test-game.exe"
    assert not native._IN_WORKER


def test_worker_rejects_unknown_operations(tmp_path):
    native._run_request(_request(tmp_path, "arbitrary_command", {}))
    result = json.loads((tmp_path / "response.json").read_text())
    assert not result["ok"]
    assert "unsupported" in result["error"]


def test_kingdom_presentation_uses_native_worker_when_redirected(tmp_path, monkeypatch):
    from coding4game_gui.benchmark.windows.game_settings import force_kingdom_rush_settings
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(native, "needs_native_io", lambda directory: True)
    calls = []
    def call(operation, payload):
        calls.append((operation, payload))
        return "Windowed 2560x1440, zh-Hans"
    monkeypatch.setattr(native, "native_call", call)
    assert force_kingdom_rush_settings((2560, 1440)) == "Windowed 2560x1440, zh-Hans"
    assert calls == [("kingdom_presentation", {"size": [2560, 1440], "mode": "Windowed"})]
    assert not (tmp_path / "kingdom_rush_alliance").exists()


def test_locallow_uses_the_same_native_write_boundary(tmp_path, monkeypatch):
    if native.os.name != 'nt':
        pytest.skip('Windows AppData routing')
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    checked = []
    monkeypatch.setattr(native, '_redirected_write', lambda directory: checked.append(directory) or True)
    target = tmp_path / 'AppData/LocalLow/Team17/Overcooked2'
    assert native.needs_native_io(target)
    assert checked == [target]


def test_save_session_worker_probes_parent_without_creating_original(tmp_path, monkeypatch):
    from coding4game_gui.benchmark.windows import save_session
    source = tmp_path / 'player/account'
    evidence = tmp_path / 'evidence'
    spec = {'driver_game': 'registered-fixture', 'startup': {'golden_checkpoint': 'golden'}}
    request = _request(tmp_path, 'begin_save_session', {
        'scenario': 'registered-fixture', 'windows_input': 'gamepad', 'evidence': str(evidence)})
    monkeypatch.setattr('coding4game_gui.benchmark.spec.load_game_spec', lambda *a, **kw: spec)
    monkeypatch.setattr('coding4game_gui.benchmark.windows.games_config.load_game_config',
                        lambda *a: SimpleNamespace(save_state=None))
    monkeypatch.setattr(save_state, 'resolve_for_run', lambda *a: SimpleNamespace(restore_to=source))
    checked = []
    monkeypatch.setattr(native, '_redirected_write', lambda directory: checked.append(directory) or False)
    def begin(given, directory):
        assert native._IN_WORKER and given is spec and directory == evidence
        assert not source.exists()
        return {'status': 'captured', 'journal': str(evidence / 'save-session.json')}
    monkeypatch.setattr(save_session, 'begin_save_session', begin)
    native._run_request(request)
    response = json.loads((tmp_path / 'response.json').read_text())
    assert response['ok'] and checked == [source.parent]
    assert response['result']['native_io']['method'] == 'desktop_shell'
    assert not source.exists() and not native._IN_WORKER


def test_registered_profile_path_does_not_follow_packaged_overlay(tmp_path, monkeypatch):
    if native.os.name != 'nt':
        pytest.skip('Windows AppData routing')
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    target = tmp_path / 'AppData/LocalLow/Team17/Overcooked2/account'
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        if path == target:
            return tmp_path / 'packaged-overlay/account'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolve)
    assert save_state._expand_path(str(target)) == target
