import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from coding4game_gui.benchmark.windows import startup


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".agents" / "skills" / "add-new-game" / "scripts" / "record_scorer_session.py"


def test_process_only_preparation_kills_without_touching_saves(monkeypatch):
    killed = []
    monkeypatch.setattr(startup.ss, "kill_process", killed.append)
    monkeypatch.setattr("coding4game_gui.benchmark.windows.game_settings.clear_saved_run",
                        lambda *_a, **_k: pytest.fail("process-only must not delete saves"))
    monkeypatch.setattr(startup.ss, "resolve_for_run",
                        lambda *_a, **_k: pytest.fail("process-only must not resolve saves"))
    cfg = SimpleNamespace(process="KOF.exe", save_state=None, auto_launch=None)
    result = startup.prepare_launch_state({"driver_game": "kof98",
        "startup": {"no_save_cleanup": "process_kill"}}, cfg)
    assert killed == ["KOF.exe"]
    assert result == {"mode": "no_save_process_kill"}


@pytest.mark.parametrize("settings,save_state", [
    ({"no_save_cleanup": "typo"}, None),
    ({"no_save_cleanup": "process_kill", "golden_checkpoint": "golden"}, None),
    ({"no_save_cleanup": "process_kill"}, object()),
])
def test_invalid_process_only_reset_fails_before_any_kill(monkeypatch, settings, save_state):
    monkeypatch.setattr(startup.ss, "kill_process", lambda *_a: pytest.fail("unexpected kill"))
    cfg = SimpleNamespace(process="KOF.exe", save_state=save_state)
    with pytest.raises(ValueError):
        startup.prepare_launch_state({"driver_game": "kof98", "startup": settings}, cfg)


def _module():
    spec = importlib.util.spec_from_file_location("record_scorer_session", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_no_golden_preparation_skips_checkpoint_resolution(monkeypatch):
    mod = _module()
    killed = []
    applied = []
    monkeypatch.setattr("coding4game_gui.benchmark.windows.game_settings.clear_saved_run", lambda *a, **k: None)
    monkeypatch.setattr("coding4game_gui.benchmark.windows.game_settings.verify_balatro_tutorial_disabled", lambda: None)
    monkeypatch.setattr(mod.ss, "kill_process", lambda process: killed.append(process))
    monkeypatch.setattr(
        "coding4game_gui.benchmark.windows.game_settings.apply",
        lambda *args, **kwargs: applied.append((args, kwargs)),
    )

    def fail_resolve(*_args, **_kwargs):
        raise AssertionError("no-save mode must not resolve a checkpoint")

    monkeypatch.setattr(mod.ss, "resolve_for_run", fail_resolve)
    cfg = SimpleNamespace(
        process="Balatro.exe",
        save_state=object(),
        auto_launch=SimpleNamespace(launch_path="C:/Balatro/Balatro.exe"),
    )

    result = mod.prepare_launch_state({
        "driver_game": "balatro",
        "startup": {},
        "scorer": {
            "window_setup": {"mode": "Windowed", "size": [1600, 900]},
        },
    }, cfg)

    assert result == {"mode": "no_save_cleanup"}
    assert killed == ["Balatro.exe"]
    assert applied == [(('balatro', [1600, 900], 'Windowed'), {
        "exe_path": "C:/Balatro/Balatro.exe",
    })]


def test_golden_preparation_still_restores_checkpoint(monkeypatch):
    mod = _module()
    checkpoint = ROOT / "snapshots" / "demo_golden.zip"
    restore_spec = SimpleNamespace(golden=checkpoint)
    calls = []
    monkeypatch.setattr(mod.ss, "kill_process", lambda _process: None)
    monkeypatch.setattr(
        mod.ss,
        "resolve_for_run",
        lambda game, save, name: calls.append((game, save, name)) or restore_spec,
    )
    monkeypatch.setattr(
        mod.ss,
        "restore",
        lambda spec, process: {"restored": str(spec.golden), "process": process},
    )
    cfg = SimpleNamespace(
        process="Demo.exe",
        save_state=object(),
        auto_launch=SimpleNamespace(launch_path="C:/Demo/Demo.exe"),
    )

    result = mod.prepare_launch_state({
        "driver_game": "demo",
        "startup": {"golden_checkpoint": "golden"},
        "scorer": {},
    }, cfg)

    assert calls == [("demo", cfg.save_state, "golden")]
    assert result["mode"] == "golden_restore"
    assert result["checkpoint"] == str(checkpoint)


def test_default_markers_are_generic_and_custom_markers_are_supported():
    mod = _module()

    defaults = mod.marker_names(mod.parse_markers(None))
    custom = mod.marker_names(mod.parse_markers([
        "F6=boss_full_hp", "f9=victory",
    ]))

    assert defaults == {
        "F6": "state_1", "F7": "state_2", "F8": "state_3",
        "F9": "state_4", "F10": "state_5",
    }
    assert custom == {"F6": "boss_full_hp", "F9": "victory"}


def test_marker_parser_rejects_reserved_or_duplicate_hotkeys():
    import pytest

    mod = _module()
    with pytest.raises(ValueError, match="expected F6=label"):
        mod.parse_markers(["F12=stop"])
    with pytest.raises(ValueError, match="duplicate"):
        mod.parse_markers(["F6=one", "F6=two"])


def test_operator_stop_preserves_frames_and_writes_clean_end(tmp_path, monkeypatch):
    from PIL import Image

    mod = _module()
    monkeypatch.setattr(mod, "_window_exists", lambda _hwnd: True)
    stop = tmp_path / "stop.request"

    def render():
        stop.touch()
        return Image.new("RGB", (8, 8))

    events, meta = io.StringIO(), {}
    mod._record_frames(SimpleNamespace(render=render), 1, tmp_path, events,
                       fps=10, max_seconds=1, markers={}, stop_file=stop, meta=meta)
    rows = [json.loads(line) for line in events.getvalue().splitlines()]
    assert [row["type"] for row in rows] == ["start", "frame", "end"]
    assert rows[-1]["reason"] == "operator_stop_file"
    assert rows[-1]["frames"] == meta["frames"] == 1
    assert (tmp_path / rows[1]["file"]).is_file()


def test_closed_game_ends_without_attempting_capture(tmp_path, monkeypatch):
    mod = _module()
    monkeypatch.setattr(mod, "_window_exists", lambda _hwnd: False)

    def render():
        raise AssertionError("a closed window must not be captured")

    events, meta = io.StringIO(), {}
    mod._record_frames(SimpleNamespace(render=render), 1, tmp_path, events,
                       fps=10, max_seconds=1, markers={},
                       stop_file=tmp_path / "stop.request", meta=meta)
    rows = [json.loads(line) for line in events.getvalue().splitlines()]
    assert [row["type"] for row in rows] == ["start", "end"]
    assert rows[-1]["reason"] == meta["stop_reason"] == "game_exit"
    assert rows[-1]["frames"] == 0


def test_window_closure_during_capture_is_a_clean_exit(tmp_path, monkeypatch):
    mod = _module()
    exists = iter([True, False])
    monkeypatch.setattr(mod, "_window_exists", lambda _hwnd: next(exists))

    def render():
        raise RuntimeError("window disappeared during capture")

    events, meta = io.StringIO(), {}
    mod._record_frames(SimpleNamespace(render=render), 1, tmp_path, events,
                       fps=10, max_seconds=1, markers={},
                       stop_file=tmp_path / "stop.request", meta=meta)
    assert json.loads(events.getvalue().splitlines()[-1])["reason"] == "game_exit"


def test_capture_failure_with_live_window_is_not_success(tmp_path, monkeypatch):
    import pytest

    mod = _module()
    monkeypatch.setattr(mod, "_window_exists", lambda _hwnd: True)

    def render():
        raise RuntimeError("capture device failed")

    events, meta = io.StringIO(), {}
    with pytest.raises(RuntimeError, match="capture device failed"):
        mod._record_frames(SimpleNamespace(render=render), 1, tmp_path, events,
                           fps=10, max_seconds=1, markers={},
                           stop_file=tmp_path / "stop.request", meta=meta)
    assert [json.loads(line)["type"] for line in events.getvalue().splitlines()] == ["start"]
    assert "stop_reason" not in meta


@pytest.mark.parametrize('visibility_error', [
    'visible capture requires the game foreground',
    'desktop capture requires the game foreground',
    "visible capture client is obscured at (11, 45): class='ApplicationFrameWindow'",
    "desktop capture client is obscured at (11, 45): class='ApplicationFrameWindow'",
    'desktop capture requires a non-minimized game window',
])
def test_visibility_loss_records_a_gap_and_resumes_without_losing_owner(
        tmp_path, monkeypatch, visibility_error):
    from PIL import Image

    mod = _module()
    monkeypatch.setattr(mod, '_window_exists', lambda _hwnd: True)
    stop = tmp_path / 'stop.request'
    calls = []
    def render():
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError(visibility_error)
        if len(calls) == 3:
            stop.touch()
        return Image.new('RGB', (8, 8))
    events, meta = io.StringIO(), {}
    mod._record_frames(SimpleNamespace(render=render), 1, tmp_path, events,
                       fps=100, max_seconds=1, markers={}, stop_file=stop, meta=meta)
    rows = [json.loads(line) for line in events.getvalue().splitlines()]
    assert [row['type'] for row in rows] == [
        'start', 'frame', 'capture_gap_started', 'capture_gap_ended', 'frame', 'end']
    assert meta['frames'] == 2 and meta['capture_gaps'] == 1
    assert meta['stop_reason'] == 'operator_stop_file'
    assert len(list(tmp_path.glob('*.png'))) == 2


def test_operator_can_stop_while_game_is_not_visible(tmp_path, monkeypatch):
    mod = _module()
    monkeypatch.setattr(mod, '_window_exists', lambda _hwnd: True)
    stop = tmp_path / 'stop.request'
    def render():
        stop.touch()
        raise RuntimeError('visible capture requires the game foreground')
    events, meta, progress = io.StringIO(), {}, []
    mod._record_frames(SimpleNamespace(render=render), 1, tmp_path, events,
                       fps=10, max_seconds=None, markers={}, stop_file=stop, meta=meta,
                       on_progress=lambda: progress.append((meta['frames'], meta['capture_status'])))
    assert meta['stop_reason'] == 'operator_stop_file'
    assert meta['capture_gaps'] == 1
    assert meta['capture_gap_active_at_stop']
    assert progress == [(0, 'waiting_for_visible_game')]
    assert not list(tmp_path.glob('*.png'))


def test_metadata_retries_a_transient_replace_permission_error(tmp_path, monkeypatch):
    mod = _module()
    target = tmp_path / 'meta.json'
    replace = Path.replace
    attempts = []

    def temporarily_locked(path, destination):
        attempts.append(True)
        if len(attempts) == 1:
            raise PermissionError('temporary status-file contention')
        return replace(path, destination)

    monkeypatch.setattr(Path, 'replace', temporarily_locked)
    monkeypatch.setattr(mod.time, 'sleep', lambda _seconds: None)
    mod._write_metadata(target, {'status': 'recording', 'frames': 3})
    assert len(attempts) == 2
    assert json.loads(target.read_text(encoding='utf-8')) == {
        'status': 'recording', 'frames': 3}


def test_persistent_metadata_replace_failure_remains_fatal(tmp_path, monkeypatch):
    mod = _module()
    target = tmp_path / 'meta.json'
    attempts = []

    def locked(_path, _destination):
        attempts.append(True)
        raise PermissionError('persistent status-file failure')

    monkeypatch.setattr(Path, 'replace', locked)
    monkeypatch.setattr(mod.time, 'sleep', lambda _seconds: None)
    with pytest.raises(PermissionError, match='persistent status-file failure'):
        mod._write_metadata(target, {'status': 'recording'})
    assert len(attempts) == 5
    assert not target.exists()


@pytest.mark.parametrize("outcome", ["capture_failure", "startup_failure", "operator_stop", "manual_stop", "gated_manual_stop"])
def test_main_cleanup_preserves_human_game_after_recording_failure(
        tmp_path, monkeypatch, outcome):
    mod = _module()
    killed, closed = [], []
    cfg = SimpleNamespace(process="Example.exe", auto_launch=SimpleNamespace(
        launch_path="C:/Example/Example.exe"), use_speedhack=False)
    monkeypatch.setattr(mod.sys, "argv", [str(SCRIPT), "--scenario", "example",
                                         "--out", str(tmp_path)]
                        + (["--until-stopped"] if outcome in {"manual_stop", "gated_manual_stop"} else [])
                        + (["--start-file", str(tmp_path / "start.request")]
                           if outcome == "gated_manual_stop" else []))
    monkeypatch.setattr(mod, "load_game_spec", lambda _slug: {"driver_game": "example"})
    monkeypatch.setattr(mod, "load_game_config", lambda _game: cfg)
    monkeypatch.setattr(mod.ss, "kill_process", killed.append)
    monkeypatch.setattr(mod, "_require_foreground", lambda _hwnd: None)
    monkeypatch.setattr(mod, "_capture_milestone", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_window_exists", lambda _hwnd: True)
    monkeypatch.setattr(mod, "_pressed", lambda _vk: False)
    controller = SimpleNamespace(
        platform="windows", tics_per_second=10, replay_menu_skip=lambda *_a: None,
        _force_foreground=lambda _env: True,
        prepare_policy_handoff=lambda _env: True)
    monkeypatch.setattr(mod, "ResetController", lambda _spec: controller)

    def render():
        if outcome == "capture_failure":
            raise RuntimeError("capture device failed")
        from PIL import Image
        (tmp_path / "stop.request").touch()
        return Image.new("RGB", (8, 8))

    unpaused = []
    env = SimpleNamespace(render=render, hard_pause=True, unpause=lambda: unpaused.append(True),
                          close=lambda: closed.append(True))
    if outcome == "gated_manual_stop":
        def release_after_inspection(_seconds):
            request = tmp_path / "start.request"
            if not request.exists():
                assert env.hard_pause and not unpaused
                meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
                assert meta["status"] == "waiting_for_operator_start"
                request.touch()
        monkeypatch.setattr(mod.time, "sleep", release_after_inspection)

    def launch(*_a, **_k):
        if outcome == "startup_failure":
            raise RuntimeError("startup failed")
        return env, 1, "Example"

    monkeypatch.setattr(mod, "start_auto_launch_session", launch)
    if outcome in ("operator_stop", "manual_stop", "gated_manual_stop"):
        assert mod.main() == 0
    else:
        # Startup failure has no env. Avoid taking a desktop failure screenshot.
        monkeypatch.setattr("PIL.ImageGrab.grab", lambda **_k: render())
        message = "capture device failed" if outcome == "capture_failure" else "startup failed"
        with pytest.raises(RuntimeError, match=message):
            mod.main()

    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    if outcome == "capture_failure":
        assert killed == []
        assert closed == [True]
        assert meta["error"] == "RuntimeError: capture device failed"
        assert meta["cleanup"]["game_termination_requested"] is False
        assert meta["cleanup"]["reason"] == "recording_failed_after_handoff"
    elif outcome in {"manual_stop", "gated_manual_stop"}:
        assert killed == []
        assert closed == [True]
        assert meta["max_seconds"] is None
        assert meta["stop_policy"] == "operator_file_or_game_exit"
        assert meta["status"] == "stopped"
        assert meta["cleanup"]["game_termination_requested"] is False
    else:
        assert killed == ["Example.exe"]
        assert meta["cleanup"]["game_termination_requested"] is True
    if outcome in ("operator_stop", "manual_stop", "gated_manual_stop"):
        assert meta["handoff_at"]
        assert (tmp_path / "frames" / "000000.png").exists()
        assert unpaused == [True]
    else:
        assert "handoff_at" not in meta


@pytest.mark.parametrize("blocked_by", ["stop", "game_exit"])
def test_operator_start_never_releases_after_stop_or_closed_game(tmp_path, monkeypatch, blocked_by):
    mod = _module()
    request, stop = tmp_path / "start.request", tmp_path / "stop.request"
    request.touch()
    if blocked_by == "stop":
        stop.touch()
    monkeypatch.setattr(mod, "_window_exists", lambda _hwnd: blocked_by != "game_exit")
    with pytest.raises(RuntimeError, match="before operator start"):
        mod._wait_for_operator_start(request, stop, 1)


def test_operator_controlled_recording_continues_past_short_test_duration(tmp_path, monkeypatch):
    from PIL import Image

    mod = _module()
    clock, seen, progress = [0.0], [], []
    monkeypatch.setattr(mod.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(mod.time, "sleep", lambda _s: clock.__setitem__(0, clock[0] + 31))
    monkeypatch.setattr(mod, "_window_exists", lambda _hwnd: True)
    stop = tmp_path / "stop.request"

    def render():
        seen.append(clock[0])
        if len(seen) == 3:
            stop.touch()
        return Image.new("RGB", (8, 8))

    events, meta = io.StringIO(), {}
    mod._record_frames(SimpleNamespace(render=render), 1, tmp_path, events,
                       fps=10, max_seconds=None, markers={}, stop_file=stop,
                       meta=meta, on_progress=lambda: progress.append(dict(meta)))
    rows = [json.loads(x) for x in events.getvalue().splitlines()]
    assert seen == [0, 31, 62]
    assert meta["frames"] == 3
    assert rows[-1]["reason"] == "operator_stop_file"
    assert progress[-1]["last_frame_elapsed_seconds"] == 62
    assert progress[-1]["frames"] == 3


def test_manual_mode_rejects_an_accidental_short_duration(monkeypatch):
    mod = _module()
    monkeypatch.setattr(mod.sys, "argv", [str(SCRIPT), "--scenario", "example",
                                         "--until-stopped", "--max-seconds", "30"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 2
