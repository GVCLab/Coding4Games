"""Write operator progress for stepped Direct episodes to stderr.

Set C4G_PROGRESS=0 to silence progress. Set NO_COLOR to disable ANSI colors.
"""
from __future__ import annotations

import os
import sys
import time

_T0 = time.monotonic()
_STREAM = None  # tests may inject a stream via set_stream(); else stderr at call time

# ANSI palette (only emitted when color is on).
_CODES = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m", "italic": "\033[3m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    "white": "\033[37m", "grey": "\033[90m",
}


def set_stream(stream) -> None:
    """Redirect narration (used by tests). Pass None to restore stderr."""
    global _STREAM
    _STREAM = stream


def _out():
    return _STREAM if _STREAM is not None else sys.stderr


def _enabled() -> bool:
    v = os.environ.get("C4G_PROGRESS")
    if v is not None:
        return v.strip().lower() not in ("0", "", "false", "no", "off")
    return True


def _color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("C4G_PROGRESS_COLOR") == "1":
        return True  # force color even when not a tty (tests / piping on purpose)
    try:
        return bool(_out().isatty())
    except Exception:  # noqa: BLE001
        return False


def _paint(text: str, *codes: str) -> str:
    if not codes or not _color():
        return text
    pre = "".join(_CODES.get(c, "") for c in codes)
    return f"{pre}{text}{_CODES['reset']}"


def _clock() -> str:
    s = int(time.monotonic() - _T0)
    return f"{s // 60:02d}:{s % 60:02d}"


def _emit(line: str) -> None:
    """Write one already-formatted line (+newline) atomically. Never raises."""
    if not _enabled():
        return
    try:
        f = _out()
        f.write(line + "\n")
        f.flush()
    except Exception:  # noqa: BLE001 — narration must never break a run
        pass


def _row(icon: str, body: str, *, time_color: str = "grey") -> None:
    """A standard feed row: dim clock + icon + body."""
    _emit(f"{_paint(_clock(), time_color)} {icon} {body}")


# -- public API ------------------------------------------------------------

def banner(title: str, subtitle: str = "") -> None:
    """Write a visible phase header."""
    bar = "─" * 3
    head = _paint(f"{bar} {title} {bar}", "bold", "cyan")
    line = head if not subtitle else f"{head} {_paint(subtitle, 'dim')}"
    _emit("")
    _emit(f"{_paint(_clock(), 'grey')} {line}")


def note(text: str) -> None:
    """A low-key status aside."""
    _row(_paint("·", "grey"), _paint(text, "dim"))


def play_start(seed: int, ep: int, total: int, label: str = "seed") -> None:
    """Report the start of a Direct episode."""
    _row(_paint("🎮", "cyan"),
         _paint(f"player rolling out episode {ep + 1}/{total} ({label} {seed})", "cyan"))


def play_done(seed: int, score, tics: int, calls: int, error=None,
              label: str = "seed") -> None:
    """A player episode finished — score + cost line."""
    try:
        sc = f"{float(score):+.1f}"
    except (TypeError, ValueError):
        sc = str(score)
    col = "red" if error else ("green" if _is_pos(score) else "yellow")
    tail = _paint(f"ERROR {error}", "red") if error else ""
    _emit(f"{' ' * 6}{_paint('└', 'grey')} {label} {seed}: "
          f"{_paint('score ' + sc, col, 'bold')} "
          f"{_paint(f'({calls} calls, {tics} tics)', 'dim')} {tail}".rstrip())


def _is_pos(score) -> bool:
    try:
        return float(score) > 0
    except (TypeError, ValueError):
        return False
