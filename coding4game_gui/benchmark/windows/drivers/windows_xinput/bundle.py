"""WindowsXInputBundle factory: assembles the 4 drivers for Windows + XInput."""

from __future__ import annotations

import platform
from typing import Optional

from coding4game_gui.benchmark.windows.drivers.base import Bundle
from coding4game_gui.benchmark.windows.drivers.windows_xinput.input import XInputDriver
from coding4game_gui.benchmark.windows.drivers.windows_xinput.lifecycle import WindowsProcessLifecycle
from coding4game_gui.benchmark.windows.drivers.windows_xinput.lifecycle import set_dpi_awareness
from coding4game_gui.benchmark.windows.drivers.windows_xinput.renderer import make_windows_renderer
from coding4game_gui.benchmark.windows.drivers.windows_xinput.timectl import make_windows_timectl


def windows_xinput_bundle(
    process_name: str,
    *,
    env_fps: int,
    n_players: int,
    screenshot_backend: Optional[str] = None,
    use_speedhack: bool = True,
    input_driver: Optional[XInputDriver] = None,
) -> Bundle:
    assert platform.system().lower() == "windows", "windows-xinput bundle: Windows only."

    # Declare DPI awareness BEFORE the first window query so the bbox below is
    # in physical pixels (matches PrintWindow + dxcam). Without this, a scaled
    # display crops every capture to the top-left ~1/scale of the window.
    set_dpi_awareness()

    lifecycle = WindowsProcessLifecycle(process_name)
    info = lifecycle.find()
    if info is None:
        raise RuntimeError(
            f"windows-xinput bundle: process {process_name!r} not found. "
            f"Launch the game first, or pass --auto-launch."
        )
    print(
        f"Game process: {process_name} (PID={info.pid}, arch={info.arch}, "
        f"window={info.window_title!r})"
    )

    hwnd = info.extras.get("hwnd")
    bbox = info.extras.get("bbox") or (0, 0, 0, 0)
    if not hwnd:
        raise RuntimeError(
            f"windows-xinput bundle: no HWND found for {process_name!r}. "
            f"The game window may not be ready yet."
        )

    # Activate the window once at construction so dxcam captures the right
    # pixels and the virtual pad input lands in the right place. The 2P
    # script later re-forces foreground via lifecycle.foreground() during
    # menu_skip replay.
    lifecycle.foreground()

    renderer = make_windows_renderer(
        hwnd=hwnd, bbox=bbox, pid=info.pid,
        env_fps=env_fps, backend=screenshot_backend,
    )
    # An already-created driver is REUSED rather than replaced. Games that
    # enumerate controllers once at startup (LibGDX — Slay the Spire) can only
    # see a pad that existed BEFORE the process launched, but this factory
    # requires the process to already be running, so the caller creates the
    # pads first and hands them in. Creating a second driver here would leave
    # the pre-launch pads resident and unused while the game keeps polling
    # them, so the reuse is required for correctness, not just tidiness.
    if input_driver is not None:
        if input_driver.n_pads < n_players:
            raise ValueError(
                f"windows-xinput bundle: input_driver has "
                f"{input_driver.n_pads} pad(s), need {n_players}")
        input_ = input_driver
    else:
        input_ = XInputDriver(n_pads=n_players)
    timectl = make_windows_timectl(info.pid, info.arch, use_speedhack)

    return input_, renderer, lifecycle, timectl
