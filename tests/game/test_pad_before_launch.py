"""Virtual pads must exist and be XInput-bound BEFORE the game launches.

Some engines enumerate controllers once at startup and never listen for
hot-plug. LibGDX (Slay the Spire) is one, and StS additionally persists the
input mode it booted with, so a pad that appears late is ignored for that run
and inherited as disabled by the next. The broken order was

    launch_game -> sleep(settle_seconds) -> make_env -> pads created

which put pad creation tens of seconds after the game had finished
initialising. SDL2 games (balatro, brotato) tolerated it because they handle
hot-plug; StS could not.

WHAT THESE TESTS PIN. The bug was fixed twice, per-entry-point, and came back
both times because the project has FIVE independent launch paths and each fix
only covered the one in front of it. So the ordering now lives in exactly one
place (`coding4game_gui.benchmark.windows.prelaunch.launch_with_pads`) and these tests are
mostly about keeping it that way:

  * the sequence itself, tested once, against the shared implementation;
  * that every launch path delegates to it rather than re-implementing;
  * `test_no_entry_point_calls_launch_game_directly`, which fails when a NEW
    direct `launch_game` call appears anywhere outside the allowlist. That is
    the test that stops a sixth entry point from quietly repeating this.

vgamepad itself is not re-tested here.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from coding4game_gui.benchmark.windows import prelaunch
from coding4game_gui.benchmark.windows.drivers.windows_xinput.bundle import windows_xinput_bundle

ROOT = Path(__file__).resolve().parents[2]


class FakePads:
    """Stands in for XInputDriver; records when it was woken and closed."""

    def __init__(self, n_pads=1):
        self.n_pads = n_pads
        self.wakeups = 0
        self.closed = False

    def wakeup(self, duration: float = 0.1) -> None:
        self.wakeups += 1

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# The shared sequence. Tested by RUNNING it, not by reading its source: the
# ordering is observable through the fakes, so these are real behavioural
# tests rather than the source-grep assertions the delegation tests below
# have to use.
# ---------------------------------------------------------------------------

class _LaunchRecorder:
    """Records the interleaving of pad creation, launch, and window wait."""

    def __init__(self, *, windows_ok=(True,)):
        self.events: list[str] = []
        self.pads: list[FakePads] = []
        self._windows_ok = list(windows_ok)
        self.attempts = 0

    def pad_factory(self, n_pads):
        pads = FakePads(n_pads=n_pads)
        self.pads.append(pads)
        self.events.append(f"pads_created({n_pads})")
        return pads

    def launch(self, _spec):
        self.attempts += 1
        self.events.append("launch")

    def window_ok(self, _proc, _timeout):
        ok = self._windows_ok.pop(0) if self._windows_ok else False
        self.events.append(f"window_wait->{ok}")
        return ok


class _Spec:
    launch_path = r"C:\game\Game.exe"
    window_wait_seconds = 5.0


@pytest.fixture
def rec(monkeypatch):
    """A recorder with `launch_game` patched out, so no process is started."""
    recorder = _LaunchRecorder()
    monkeypatch.setattr("coding4game_gui.benchmark.windows.game_launch.launch_game",
                        recorder.launch)
    return recorder


def test_pads_are_created_and_bound_before_the_game_is_launched(rec):
    """The whole point. Pad creation, the wakeup tap that binds the XInput
    slot, and only THEN the launch."""
    prepared = prelaunch.launch_with_pads(
        _Spec(), "Game.exe", n_pads=2,
        pad_factory=rec.pad_factory, window_ok=rec.window_ok)

    assert rec.events == ["pads_created(2)", "launch", "window_wait->True"]
    assert prepared.pads is rec.pads[0]
    assert prepared.pads.wakeups == 1, \
        "pads were created but never woken — vgamepad has a device and the " \
        "game polls XInput and finds no bound slot"
    assert not prepared.pads.closed, "the caller owns the pads on success"


def test_the_caller_gets_pads_it_can_hand_straight_to_make_env(rec):
    prepared = prelaunch.launch_with_pads(
        _Spec(), "Game.exe", n_pads=1,
        pad_factory=rec.pad_factory, window_ok=rec.window_ok)
    assert prelaunch.env_pad_kwargs(prepared.pads) == {
        "input_driver": prepared.pads}
    # A platform with no pre-creatable driver returns None, and then the bundle
    # builds its own — the pre-existing behaviour.
    assert prelaunch.env_pad_kwargs(None) == {}


def test_a_failed_launch_closes_the_pads_instead_of_leaking_them(monkeypatch):
    """Nothing owns the pads until launch_with_pads returns, so it has to drop
    them itself or every failed launch leaks a ViGEm device for the life of the
    process."""
    rec = _LaunchRecorder(windows_ok=(False,))
    monkeypatch.setattr("coding4game_gui.benchmark.windows.game_launch.launch_game", rec.launch)

    with pytest.raises(RuntimeError, match="failed to launch"):
        prelaunch.launch_with_pads(
            _Spec(), "Game.exe", n_pads=1,
            pad_factory=rec.pad_factory, window_ok=rec.window_ok)

    assert rec.pads[0].closed, "a failed launch leaked the ViGEm device"


def test_retries_reuse_the_same_pads_and_run_the_failure_hook(monkeypatch):
    """The bench path retries 3x. The pads must NOT be rebuilt per attempt —
    they are what the game will enumerate, and rebuilding them mid-sequence is
    the very hot-plug assumption that does not hold."""
    rec = _LaunchRecorder(windows_ok=(False, False, True))
    monkeypatch.setattr("coding4game_gui.benchmark.windows.game_launch.launch_game", rec.launch)
    cleared: list[int] = []

    prepared = prelaunch.launch_with_pads(
        _Spec(), "Game.exe", n_pads=1, attempts=3,
        pad_factory=rec.pad_factory, window_ok=rec.window_ok,
        on_attempt_failed=cleared.append)

    assert rec.attempts == 3
    assert len(rec.pads) == 1, "pads were rebuilt between launch attempts"
    assert prepared.attempts_used == 3
    assert cleared == [1, 2], "the orphan-clearing hook did not run per failure"
    assert not prepared.pads.closed


def test_steam_window_timeout_never_resubmits_an_unresolved_launch(monkeypatch):
    from types import SimpleNamespace
    from coding4game_gui.benchmark.windows import game_launch
    rec = _LaunchRecorder(windows_ok=(False, False, True))
    spec = SimpleNamespace(launch_path='C:/Steam/steam.exe',
                           launch_args=['-applaunch', '714010'], window_wait_seconds=5)
    monkeypatch.setattr(game_launch, 'launch_game', rec.launch)
    monkeypatch.setattr(game_launch, 'wait_for_steam_app_idle', lambda *a, **kw: None,
                        raising=False)
    failures = []
    with pytest.raises(game_launch.SteamLaunchPendingError):
        prelaunch.launch_with_pads(
            spec, 'AimLab_tb.exe', n_pads=1, attempts=3,
            pad_factory=rec.pad_factory, window_ok=rec.window_ok,
            on_attempt_failed=failures.append)
    assert rec.attempts == 1 and failures == []
    assert rec.pads[0].closed


def test_steam_waits_for_tracked_helpers_and_updates_before_pad_setup(monkeypatch):
    from types import SimpleNamespace
    from coding4game_gui.benchmark.windows import game_launch
    monkeypatch.setattr(game_launch, '_steam_client_tracks_app', lambda *_a: False)
    rec = _LaunchRecorder()
    spec = SimpleNamespace(launch_path='C:/Steam/steam.exe',
                           launch_args=['-applaunch', '714010'], window_wait_seconds=5)
    states = [(True, False), (False, True)]
    def activity(appid):
        state = states.pop(0) if states else (False, False)
        rec.events.append(('steam_state', state))
        return state
    monkeypatch.setattr(game_launch, '_steam_app_activity', activity)
    monkeypatch.setattr(game_launch, '_game_process_running', lambda name: False)
    monkeypatch.setattr(game_launch, 'launch_game', rec.launch)
    monkeypatch.setattr(prelaunch.time, 'sleep', lambda seconds: None)
    prepared = prelaunch.launch_with_pads(
        spec, 'AimLab_tb.exe', n_pads=1, attempts=3,
        pad_factory=rec.pad_factory, window_ok=rec.window_ok)
    assert rec.events[:3] == [('steam_state', (True, False)),
                             ('steam_state', (False, True)),
                             ('steam_state', (False, False))]
    assert rec.events.index('pads_created(1)') > 2
    assert rec.attempts == 1 and prepared.attempts_used == 1


@pytest.mark.parametrize('existing_game', [False, True])
def test_steam_busy_or_existing_game_cannot_receive_another_launch(monkeypatch, existing_game):
    from types import SimpleNamespace
    from coding4game_gui.benchmark.windows import game_launch
    monkeypatch.setattr(game_launch, '_steam_client_tracks_app', lambda *_a: False)
    rec = _LaunchRecorder()
    spec = SimpleNamespace(launch_path='C:/Steam/steam.exe',
                           launch_args=['-applaunch', '714010'], window_wait_seconds=.5)
    now = [0.0]
    monkeypatch.setattr(game_launch, '_game_process_running', lambda name: existing_game)
    monkeypatch.setattr(game_launch, '_steam_app_activity', lambda appid: (True, False))
    monkeypatch.setattr(game_launch.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(game_launch.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(game_launch, 'launch_game', rec.launch)
    with pytest.raises(game_launch.SteamLaunchPendingError):
        prelaunch.launch_with_pads(spec, 'AimLab_tb.exe', n_pads=1, attempts=3,
            pad_factory=rec.pad_factory, window_ok=rec.window_ok)
    assert rec.attempts == 0 and rec.pads == []


def test_steam_registry_idle_cannot_override_client_tracking(monkeypatch):
    from types import SimpleNamespace
    from coding4game_gui.benchmark.windows import game_launch
    rec = _LaunchRecorder()
    spec = SimpleNamespace(launch_path='C:/Steam/steam.exe',
                           launch_args=['-applaunch', '714010'], window_wait_seconds=.5)
    now = [0.0]
    monkeypatch.setattr(game_launch, '_game_process_running', lambda _name: False)
    monkeypatch.setattr(game_launch, '_steam_app_activity', lambda _appid: (False, False))
    monkeypatch.setattr(game_launch, '_steam_client_tracks_app', lambda *_a: True)
    monkeypatch.setattr(game_launch.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(game_launch.time, 'sleep', lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(game_launch, 'launch_game', rec.launch)
    with pytest.raises(game_launch.SteamLaunchPendingError, match='still busy'):
        prelaunch.launch_with_pads(spec, 'AimLab_tb.exe', n_pads=1, attempts=3,
            pad_factory=rec.pad_factory, window_ok=rec.window_ok)
    assert rec.attempts == 0 and rec.pads == []


def test_a_missing_launch_path_is_rejected_before_any_pad_is_created(rec):
    class NoPath:
        launch_path = None
        window_wait_seconds = 5.0

    with pytest.raises(RuntimeError, match="no direct auto_launch.launch_path"):
        prelaunch.launch_with_pads(
            NoPath(), "Game.exe", n_pads=1,
            pad_factory=rec.pad_factory, window_ok=rec.window_ok)
    assert rec.events == [], "a config error should not create ViGEm devices"


@pytest.mark.parametrize('requires_pads', [False, True])
def test_pad_creation_failure_respects_callers_startup_contract(rec, requires_pads):
    def boom(_n):
        raise RuntimeError("ViGEmBus not installed")

    options = dict(n_pads=1, pad_factory=boom, window_ok=rec.window_ok,
                   require_prepared_pads=requires_pads)
    if requires_pads:
        with pytest.raises(RuntimeError, match='before launch'):
            prelaunch.launch_with_pads(_Spec(), 'Game.exe', **options)
        assert rec.attempts == 0
    else:
        prepared = prelaunch.launch_with_pads(_Spec(), 'Game.exe', **options)
        assert prepared.pads is None and rec.attempts == 1


def test_bundle_accepts_and_reuses_a_prebuilt_driver():
    """The bundle needs a RUNNING process to find an HWND, so make_env cannot
    be hoisted before launch — only the pads move, and the bundle has to reuse
    them. Creating a second driver would leave the pre-launch pads resident and
    unused while the game polls them."""
    sig = inspect.signature(windows_xinput_bundle)
    assert "input_driver" in sig.parameters
    assert sig.parameters["input_driver"].default is None, \
        "must stay optional so other games keep the old behaviour"


def test_prebuilt_driver_is_rejected_when_it_has_too_few_pads():
    """A 2P menu_skip needs 2 pads; silently running with 1 would look like
    player 2's inputs vanishing."""
    pads = FakePads(n_pads=1)
    try:
        windows_xinput_bundle(
            "nonexistent.exe", env_fps=10, n_players=2, input_driver=pads)
    except ValueError as e:
        assert "pad" in str(e).lower()
        return
    except Exception:
        # Process lookup fails first on a machine with no such game; the
        # pad-count guard is asserted by reading the source below instead.
        pass
    src = inspect.getsource(windows_xinput_bundle)
    assert "n_pads < n_players" in src


# ---------------------------------------------------------------------------
# Delegation. Every launch path must go THROUGH the shared sequence. These are
# source assertions because the alternative is launching real games.
# ---------------------------------------------------------------------------

def _script_src(name: str) -> str:
    return (ROOT / "scripts" / "calibration" / name).read_text(encoding="utf-8")


def test_calibrate_script_delegates_to_the_shared_sequence():
    """It only takes screenshots, but it still creates pads, and a pad the game
    never enumerated changes what the settle it is calibrating looks like."""
    src = _script_src("test_auto_launch_calibrate.py")
    assert "launch_with_pads(" in src
    assert "env_pad_kwargs(" in src


# ---------------------------------------------------------------------------
# The backstop. Everything above can pass while a NEW sixth entry point
# quietly re-introduces the bug, which is exactly what happened twice. This
# test fails on any direct launch_game call outside the allowlist.
# ---------------------------------------------------------------------------

# Files allowed to call launch_game directly, each with the reason. Adding an
# entry is a deliberate act: if the new caller drives a gamepad, it belongs in
# prelaunch.launch_with_pads instead, and adding it here would re-open the
# exact hole this test exists to close.
_LAUNCH_GAME_ALLOWLIST = {
    # The shared sequence itself.
    "coding4game_gui/benchmark/windows/prelaunch.py",
    # Launch-only smoke test: deliberately stops before any pad sequence would
    # run, which is the whole point of the "dry" in its name.
    "scripts/calibration/test_auto_launch_dry.py",
}


def _files_calling_launch_game() -> set[str]:
    hits: set[str] = set()
    for base in ("coding4game_gui", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            try:
                src = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "launch_game" not in src:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                # A CALL to launch_game — importing or defining it is fine.
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "launch_game"):
                    hits.add(path.relative_to(ROOT).as_posix())
    return hits


def test_no_entry_point_calls_launch_game_directly():
    """A new launch path must go through prelaunch.launch_with_pads.

    This is the test that would have caught auto_nav keeping the pre-fix
    ordering, and the one that catches the next entry point someone adds. If it
    fails on a file you just wrote: call `launch_with_pads` instead of
    `launch_game`, or, if your caller genuinely drives no gamepad, add it to
    the allowlist above WITH a reason.
    """
    unexpected = _files_calling_launch_game() - _LAUNCH_GAME_ALLOWLIST
    assert not unexpected, (
        f"these files launch a game without the shared pre-launch pad "
        f"sequence: {sorted(unexpected)}. Use "
        f"coding4game_gui.benchmark.windows.prelaunch.launch_with_pads so the pads exist before "
        f"the game enumerates controllers (LibGDX/Slay the Spire never sees a "
        f"pad created later), or allowlist the file if it drives no pads."
    )


def test_the_allowlist_itself_stays_honest():
    """An allowlist entry that no longer calls launch_game is stale and would
    silently permit a future direct call from that file."""
    callers = _files_calling_launch_game()
    stale = {p for p in _LAUNCH_GAME_ALLOWLIST if p not in callers}
    assert not stale, (
        f"allowlisted files that no longer call launch_game: {sorted(stale)}. "
        f"Remove them so the entry cannot silently cover a future caller.")
