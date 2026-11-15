"""Windows TimeController: xspeedhack wrapper + a Noop fallback."""

from __future__ import annotations


class XSpeedhackTimeController:
    """Attach xspeedhack to the game process, drive pause/unpause/speed via set_speed."""

    available = True

    def __init__(self, pid: int, arch: str):
        import xspeedhack as xsh
        self._client = xsh.Client(process_id=pid, arch=arch)
        self.speed: float = 1.0

    def pause(self) -> None:
        self._client.set_speed(0.0)
        self.speed = 0.0

    def unpause(self) -> None:
        self._client.set_speed(1.0)
        self.speed = 1.0

    def set_speed(self, speed: float) -> None:
        self._client.set_speed(float(speed))
        self.speed = float(speed)


class NoopTimeController:
    """Used when xspeedhack can't attach or the platform has no speed control."""

    available = False

    def __init__(self):
        self.speed: float = 1.0

    def pause(self) -> None:
        pass

    def unpause(self) -> None:
        pass

    def set_speed(self, speed: float) -> None:
        # Silently ignore — phase-default behavior the runner relies on.
        pass


def make_windows_timectl(pid: int, arch: str, use_speedhack: bool):
    """Build the timectl, falling back to Noop if attach fails."""
    if not use_speedhack:
        return NoopTimeController()
    try:
        tc = XSpeedhackTimeController(pid, arch)
        print("xspeedhack attached")
        return tc
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: xspeedhack failed to attach ({exc!r}); pause/unpause disabled")
        return NoopTimeController()
