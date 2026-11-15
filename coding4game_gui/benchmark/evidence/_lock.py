"""Cross-platform advisory file lock — POSIX flock / Windows msvcrt.locking.

Evidence tools run on Linux and Windows. `msvcrt.locking` locks a byte
range at the current position on Windows, so we always
lock byte 0 (ranges may extend past EOF on Windows) irrespective of where
the caller's file position is, and restore the position afterwards.
Advisory on both platforms: every writer must go through this helper."""
from __future__ import annotations

from contextlib import contextmanager

try:
    import fcntl
except ImportError:          # Windows
    fcntl = None
    import msvcrt


@contextmanager
def locked(f):
    """Exclusive lock on an OPEN file object for the duration of the block."""
    if fcntl is not None:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield f
        finally:
            try:
                f.flush()          # bytes reach the fd BEFORE the lock drops
            except (OSError, ValueError):
                pass               # read-only/closed handles have nothing to flush
            fcntl.flock(f, fcntl.LOCK_UN)
        return
    # Windows: LK_LOCK retries ~10s then raises — loop for indefinite blocking.
    while True:
        pos = f.tell()
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            f.seek(pos)
            break
        except OSError:
            f.seek(pos)
    try:
        yield f
    finally:
        f.flush()            # bytes must reach the fd BEFORE the lock drops
        pos = f.tell()
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            f.seek(pos)
