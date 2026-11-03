"""Platform lock primitives for the advisory Codex config lease."""

from __future__ import annotations

import os

LOCK_WAIT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.05
LOCK_BUSY = "Codex config lock is busy; another install may be running"


def try_acquire(descriptor: int) -> OSError | None:
    """Attempt a non-blocking lock; return the busy error instead of raising.

    Both platforms use a non-blocking primitive so the caller owns the wait
    bound and can convert contention into one stable, catchable error instead
    of a traceback (or, on POSIX, an unbounded block).
    """
    return _lock_windows(descriptor) if os.name == "nt" else _lock_posix(descriptor)


def release(descriptor: int) -> None:
    """Release a lock this process holds; callers must only call when holding."""
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _lock_windows(descriptor: int) -> OSError | None:
    import msvcrt

    try:
        if os.fstat(descriptor).st_size == 0:
            # A zero-byte region lock is not observable to another process.
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        # CPython spells this constant LK_NBLCK; there is no LK_NBLOCK.
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return None
    except OSError as error:
        return error


def _lock_posix(descriptor: int) -> OSError | None:
    import fcntl

    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return None
    except OSError as error:
        return error
