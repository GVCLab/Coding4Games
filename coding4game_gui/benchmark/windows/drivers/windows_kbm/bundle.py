"""Shared Windows bundle with policy-facing physical keyboard/mouse control.

The wrapped XInput driver remains available to trusted startup infrastructure;
the benchmark resolver keeps it outside the keyboard/mouse policy surface.
"""
from __future__ import annotations

from typing import Optional

from coding4game_gui.benchmark.windows.drivers.base import Bundle
from coding4game_gui.benchmark.windows.drivers.windows_kbm.input import KeyboardMouseInputDriver
from coding4game_gui.benchmark.windows.drivers.windows_kbm.input import ScanCodeKeyboard
from coding4game_gui.benchmark.windows.drivers.windows_xinput.input import XInputDriver


def windows_kbm_bundle(
    process_name: str,
    *,
    env_fps: int,
    n_players: int,
    screenshot_backend: Optional[str] = None,
    use_speedhack: bool = True,
    input_driver=None,
    keyboard=None,
) -> Bundle:
    """Reuse the proven Windows bundle and deepen only its input adapter."""
    from coding4game_gui.benchmark.windows.drivers.windows_xinput.bundle import windows_xinput_bundle

    gamepad = input_driver or XInputDriver(n_pads=n_players)
    wrapped = None
    try:
        keyboard_driver = keyboard or ScanCodeKeyboard()
        wrapped = KeyboardMouseInputDriver(gamepad, keyboard_driver)
        return windows_xinput_bundle(
            process_name,
            env_fps=env_fps,
            n_players=n_players,
            screenshot_backend=screenshot_backend,
            use_speedhack=use_speedhack,
            input_driver=wrapped,
        )
    except BaseException:
        # Ownership transfers to this bundle factory even when attachment fails.
        try:
            if wrapped is not None:
                wrapped.close()
            else:
                gamepad.close()
        except Exception:
            pass
        raise


__all__ = ["windows_kbm_bundle"]
