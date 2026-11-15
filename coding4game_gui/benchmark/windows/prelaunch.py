"""Bind virtual pads before launching a game and waiting for its window.

Slay the Spire enumerates controllers at startup and persists the input mode.
The benchmark and recorder share this ordering. The caller owns the prepared
pads after success. A failed launch closes them here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from typing import Callable
from typing import Optional


@dataclass
class PreparedLaunch:
    """Result of a successful pre-launch sequence.

    `pads` is None when pre-launch creation failed and fallback was allowed.
    """
    pads: Optional[Any]
    attempts_used: int


def make_pads(n_pads: int, *,
              platform_name: str = "auto",
              pad_factory: Optional[Callable[[int], Any]] = None):
    """Create the pads and BIND them to XInput slots. Exposed for tests.

    Prefer `launch_with_pads`. Calling this directly puts the caller back in
    charge of the ordering, which is the mistake this module exists to remove.

    The wakeup tap is what actually binds each pad to an XInput slot: without
    it vgamepad has created the device but the game polls XInput and finds
    nothing. The settle afterwards is because Windows completes that binding
    asynchronously.
    """
    if platform_name not in ("auto", "windows-xinput", "windows-kbm"):
        return None
    try:
        if pad_factory is not None:
            pads = pad_factory(n_pads)
        else:
            from coding4game_gui.benchmark.windows.drivers.windows_xinput.input import XInputDriver
            pads = XInputDriver(n_pads=n_pads)
    except Exception as exc:  # noqa: BLE001 — fall back to bundle-created pads
        print(f"[prelaunch] pad creation unavailable ({exc!r}); the bundle "
              f"will create pads after launch")
        return None
    if pads is None:
        return None
    try:
        pads.wakeup()
        time.sleep(1.0)      # let Windows finish binding the slots
    except Exception as exc:  # noqa: BLE001
        print(f"[prelaunch] WARN: wakeup tap failed: {exc!r}")
    print(f"[prelaunch] {n_pads} virtual pad(s) live before launch")
    return pads


def launch_with_pads(
    auto_launch,
    process_name: str,
    *,
    n_pads: int,
    platform_name: str = "auto",
    require_prepared_pads: bool = False,
    attempts: int = 1,
    pad_factory: Optional[Callable[[int], Any]] = None,
    on_attempt_failed: Optional[Callable[[int], None]] = None,
    window_ok: Optional[Callable[[str, float], bool]] = None,
) -> PreparedLaunch:
    """Create+bind the pads, THEN launch the game, THEN wait for its window.

    This is the whole ordering-sensitive sequence, start to finish. Callers
    pass configuration, not steps.

    Args:
        auto_launch: the game's `AutoLaunchSpec` (needs `launch_path` and
            `window_wait_seconds`).
        process_name: the `process:` name to wait for. When `launch_args`
            routes through a platform client this is the GAME, while the
            launched exe is the client, so the wait must key on this and not
            on the spawned process.
        n_pads: how many virtual pads to create. Derive it from the config
            BEFORE calling (e.g. from `menu_skip`), never after.
        platform_name: the Windows bundle that will reuse the prepared pads.
        require_prepared_pads: stop before launching if pad creation failed.
        attempts: launch attempts. The bench path uses 3 with an
            `on_attempt_failed` that clears orphans; interactive paths use 1 so
            a failure surfaces immediately.
        pad_factory: optional constructor for testing pad and launch ordering.
        on_attempt_failed: called with the 1-based attempt number after a
            failed attempt, before retrying. Use it to kill orphan processes.
        window_ok: window-wait predicate `(process_name, timeout) -> bool`.
            Defaults to `save_state.wait_for_game_window`; injectable for
            tests so they need no real game.

    Returns:
        PreparedLaunch with the pads the caller now owns.

    Raises:
        RuntimeError: if `auto_launch` has no `launch_path`, or the window
            never appeared. The pads are closed before raising in both cases.
    """
    if auto_launch is None or not getattr(auto_launch, "launch_path", None):
        raise RuntimeError(
            f"{process_name}: no direct auto_launch.launch_path to start")
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    if n_pads < 1:
        raise ValueError("n_pads must be >= 1")

    from coding4game_gui.benchmark.windows.game_launch import (
        SteamLaunchPendingError, steam_app_id, wait_for_steam_app_idle,
    )
    appid = steam_app_id(auto_launch)
    timeout = float(getattr(auto_launch, "window_wait_seconds", 120.0))
    steam_deadline = time.monotonic() + timeout if appid is not None else None
    if appid is not None:
        wait_for_steam_app_idle(appid, process_name, timeout=timeout,
                               steam_exe=auto_launch.launch_path)

    # ---- pads FIRST: the entire point of this module ----
    pads = make_pads(n_pads, platform_name=platform_name,
                     pad_factory=pad_factory)
    if pads is None and require_prepared_pads:
        raise RuntimeError("Could not create virtual pads before launch")

    # Nothing owns the pads until this function returns, so every exit from
    # here on closes them.
    try:
        from coding4game_gui.benchmark.windows.game_launch import launch_game
        if window_ok is None:
            from coding4game_gui.benchmark.windows import save_state as ss
            window_ok = ss.wait_for_game_window

        for attempt in range(1, attempts + 1):
            window_timeout = timeout
            if appid is not None:
                # Check again after pad setup in case an earlier delayed request
                # created the game while this caller was preparing its devices.
                wait_for_steam_app_idle(
                    appid, process_name, timeout=max(0.0, steam_deadline - time.monotonic()),
                    steam_exe=auto_launch.launch_path)
                window_timeout = max(0.0, steam_deadline - time.monotonic())
                if window_timeout <= 0:
                    raise SteamLaunchPendingError(
                        f'Steam app {appid} has no window-wait budget left; no launch was sent')
            print(f"[prelaunch] launching {auto_launch.launch_path}"
                  + (f" (attempt {attempt}/{attempts})" if attempts > 1
                     else ""))
            launch_game(auto_launch)
            print(f"[prelaunch] waiting up to {window_timeout:.0f}s for "
                  f"{process_name} window ...")
            if window_ok(process_name, window_timeout):
                return PreparedLaunch(pads=pads, attempts_used=attempt)
            print(f"[prelaunch] attempt {attempt}: {process_name} window "
                  f"never appeared in {window_timeout:.0f}s")
            if appid is not None:
                raise SteamLaunchPendingError(
                    f'Steam launch for app {appid} is unresolved after the window wait; '
                    'no additional request was sent. Finish or cancel the existing launch first.')
            if on_attempt_failed is not None:
                on_attempt_failed(attempt)

        raise RuntimeError(
            f"{process_name} failed to launch after {attempts} attempt(s) "
            f"(game window never appeared)")
    except BaseException:
        _close_quietly(pads)
        raise


def _close_quietly(pads) -> None:
    """Drop the ViGEm devices; a close failure must not mask the real error."""
    if pads is None:
        return
    try:
        pads.close()
    except Exception:  # noqa: BLE001
        pass


def env_pad_kwargs(pads) -> dict:
    """`make_env(**env_pad_kwargs(pads))` — reuse the pre-launch pads.

    A bundle that builds its own driver would leave the pre-launch pads
    resident and unused while the game keeps polling them, so passing these
    through is required for correctness, not tidiness.
    """
    return {"input_driver": pads} if pads is not None else {}


__all__ = [
    "PreparedLaunch",
    "make_pads",
    "launch_with_pads",
    "env_pad_kwargs",
]
