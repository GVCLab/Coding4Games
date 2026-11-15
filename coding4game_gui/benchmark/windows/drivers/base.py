"""Driver protocols + shared dataclasses.

These define the contract every platform bundle must satisfy. The pad-state
dict (`buttons + left_stick + right_stick + left_trigger + right_trigger`)
is the canonical cross-platform action representation — non-XInput drivers
translate it to their native input ops inside `apply_state`.

`runtime_checkable` is intentionally left off the Protocols: structural
typing is enough, and avoiding the marker keeps `isinstance` from doing
slow attribute walks on the driver instances during the game loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Optional
from typing import Protocol

from PIL import Image


# ---- shared types ----

@dataclass(frozen=True)
class ProcessInfo:
    """Cross-platform handle to the target process.

    Each platform fills in what's meaningful and leaves the rest None.
    Windows uses pid + window_title + arch + extras["hwnd"]; Android-ADB
    would use pid=None + extras={"package": ..., "serial": ...}.
    """
    pid: Optional[int] = None
    window_title: Optional[str] = None
    arch: Optional[str] = None
    extras: dict = field(default_factory=dict)


# pad_state shape, repeated as a docstring everywhere so future maintainers
# know what a driver receives without grepping for the canonical example:
#   {
#     "buttons":       list[str],            # XInput names: SOUTH, EAST, ...
#     "left_stick":    tuple[float, float],  # each in [-1, 1]
#     "right_stick":   tuple[float, float],
#     "left_trigger":  float,                # in [0, 1]
#     "right_trigger": float,
#   }


# ---- protocols ----

class InputDriver(Protocol):
    """Apply pad-state to the target. One driver instance may hold N pads."""

    n_pads: int

    def apply_state(
        self,
        idx: int,
        *,
        buttons: list[str],
        left_stick: tuple[float, float],
        right_stick: tuple[float, float],
        left_trigger: float,
        right_trigger: float,
    ) -> None: ...

    def reset_pad(self, idx: int) -> None: ...

    def reset(self) -> None: ...

    def wakeup(self, duration: float = 0.1) -> None: ...

    def close(self) -> None: ...


class Renderer(Protocol):
    """Return a current frame. No internal lock — Env owns it."""

    bbox: tuple[int, int, int, int]   # (left, top, width, height) in screen coords
    name: str                          # for logging / META.json

    def screenshot(self) -> Image.Image: ...

    def close(self) -> None: ...


class ProcessLifecycle(Protocol):
    """Find, stop and focus the target process."""

    process_name: str

    def find(self) -> Optional[ProcessInfo]: ...

    def kill(self) -> int: ...

    def foreground(self) -> bool: ...

class TimeController(Protocol):
    """Pause / unpause / speed control. `available=False` makes set_speed a noop."""

    available: bool

    def pause(self) -> None: ...

    def unpause(self) -> None: ...

    def set_speed(self, speed: float) -> None: ...


# ---- bundle factory type ----

Bundle = tuple[InputDriver, Renderer, ProcessLifecycle, TimeController]
"""Return value of every platform bundle factory."""
