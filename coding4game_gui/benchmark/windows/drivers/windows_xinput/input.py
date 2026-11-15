"""XInputDriver: drives N virtual Xbox 360 gamepads via the `vgamepad` library.

Translates the cross-platform pad-state dict (buttons + sticks + triggers)
to vgamepad's XUSB enums. This is the canonical implementation — other
platforms ship a translation layer that produces the same observable
effect via their own input API.
"""

from __future__ import annotations

import platform
import time

from coding4game_gui.benchmark.windows.gamepad import XBOX_BUTTON_MAP

# Allow tools to import the driver module before vgamepad is available.
try:
    import vgamepad as vg
    _VGAMEPAD_AVAILABLE = True
    _VGAMEPAD_IMPORT_ERROR = None
except Exception as _exc:  # noqa: BLE001
    vg = None  # type: ignore
    _VGAMEPAD_AVAILABLE = False
    _VGAMEPAD_IMPORT_ERROR = _exc


class XInputDriver:
    """One vg.VX360Gamepad per pad, kept resident for the run."""

    def __init__(self, n_pads: int = 1):
        assert platform.system().lower() == "windows", "XInputDriver: Windows only."
        if not _VGAMEPAD_AVAILABLE:
            raise RuntimeError(
                f"vgamepad is not importable: {_VGAMEPAD_IMPORT_ERROR!r}. "
                f"Install with `pip install vgamepad` (Windows + ViGEm)."
            )
        if n_pads < 1:
            raise ValueError("n_pads must be >= 1")
        self.n_pads = n_pads
        self.pads = [vg.VX360Gamepad() for _ in range(n_pads)]

    def _apply_to_pad(
        self, gp, *, buttons, left_stick, right_stick,
        left_trigger, right_trigger,
    ):
        gp.reset()
        # LT/RT can show up either as analog values or as named buttons —
        # older configs / raw-mode agents sometimes list them in buttons.
        # Treat that case as a full press (1.0), merged with explicit
        # trigger floats via max.
        for btn in buttons:
            if btn == "LEFT_TRIGGER":
                if left_trigger < 1.0:
                    left_trigger = 1.0
                continue
            if btn == "RIGHT_TRIGGER":
                if right_trigger < 1.0:
                    right_trigger = 1.0
                continue
            mapped = XBOX_BUTTON_MAP.get(btn)
            if mapped:
                gp.press_button(button=getattr(vg.XUSB_BUTTON, mapped))

        # Sticks clamped to [-1, 1] then scaled to int16 range. +Y is up
        # per XInput convention (matches FPS / 3rd-person mappings).
        lx = int(max(-1.0, min(1.0, left_stick[0])) * 32767)
        ly = int(max(-1.0, min(1.0, left_stick[1])) * 32767)
        rx = int(max(-1.0, min(1.0, right_stick[0])) * 32767)
        ry = int(max(-1.0, min(1.0, right_stick[1])) * 32767)
        gp.left_joystick(x_value=lx, y_value=ly)
        gp.right_joystick(x_value=rx, y_value=ry)

        # Analog triggers: [0, 1] → [0, 255].
        lt = int(max(0.0, min(1.0, left_trigger)) * 255)
        rt = int(max(0.0, min(1.0, right_trigger)) * 255)
        gp.left_trigger(value=lt)
        gp.right_trigger(value=rt)

        gp.update()

    def apply_state(
        self, idx: int, *, buttons, left_stick, right_stick,
        left_trigger, right_trigger,
    ) -> None:
        if not (0 <= idx < self.n_pads):
            raise IndexError(f"pad index {idx} out of range (n_pads={self.n_pads})")
        self._apply_to_pad(
            self.pads[idx],
            buttons=buttons, left_stick=left_stick, right_stick=right_stick,
            left_trigger=left_trigger, right_trigger=right_trigger,
        )

    def reset_pad(self, idx: int) -> None:
        if not (0 <= idx < self.n_pads):
            raise IndexError(f"pad index {idx} out of range (n_pads={self.n_pads})")
        self.pads[idx].reset()
        self.pads[idx].update()

    def reset(self) -> None:
        for pad in self.pads:
            pad.reset()
            pad.update()

    def wakeup(self, duration: float = 0.1) -> None:
        """Tap LEFT_THUMB on each pad so Windows registers the virtual controllers."""
        for pad in self.pads:
            pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB)
            pad.update()
            time.sleep(duration)
            pad.reset()
            pad.update()
            time.sleep(duration)

    def close(self) -> None:
        # Drop pad references + force GC so vgamepad's __del__ (vigem_target_
        # remove) runs NOW, instead of leaking the virtual device until
        # interpreter shutdown. Matters because the env singleton that owns this
        # driver used to never be closed — every run leaked a ViGEm pad.
        import gc
        self.pads = []
        gc.collect()
