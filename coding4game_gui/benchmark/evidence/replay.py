"""Time selectors for host-recorded Windows replay evidence (no game execution)."""
import re
from coding4game_gui.benchmark.windows.timing import DEFAULT_TICS_PER_SECOND


def to_tic(value, tics_per_second=DEFAULT_TICS_PER_SECOND):
    if isinstance(value, (int, float)):
        return max(0, int(value))
    value = str(value).strip()
    if ":" in value:
        minutes, seconds = value.split(":", 1)
        return max(0, int(round((int(minutes) * 60 + float(seconds)) * tics_per_second)))
    if re.fullmatch(r"\d+", value):
        return int(value)
    return max(0, int(round(float(value) * tics_per_second)))
