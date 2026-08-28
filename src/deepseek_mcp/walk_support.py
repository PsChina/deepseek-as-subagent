"""Small lifecycle helpers for bounded workspace traversal."""
from __future__ import annotations

import errno
import os
import stat
from typing import Iterator, Any

from .file_io import ToolInputError
from . import windows_walk

_RACE_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


class WorkspaceEntryTooLarge(ToolInputError):
    pass


def skip_race_or_raise(error: OSError):
    if error.errno in _RACE_ERRNOS:
        return None
    raise ToolInputError("workspace traversal failed") from error


def read_descriptor(descriptor: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise WorkspaceEntryTooLarge(f"file exceeds {max_bytes} bytes")
    return data


def read_open_entry(descriptor: int, max_bytes: int) -> bytes:
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ToolInputError("workspace entry is not a regular file")
        if before.st_size > max_bytes:
            raise WorkspaceEntryTooLarge(f"file exceeds {max_bytes} bytes")
        os.lseek(descriptor, 0, os.SEEK_SET)
        data = read_descriptor(descriptor, max_bytes)
        after = os.fstat(descriptor)
        fields = (
            "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns"
        )
        if len(data) != before.st_size or any(
            getattr(before, name) != getattr(after, name) for name in fields
        ):
            raise ToolInputError("workspace entry changed while reading")
        return data
    except WorkspaceEntryTooLarge:
        raise
    except OSError as error:
        raise ToolInputError("failed to read workspace entry") from error


def yield_entry(entry: Any) -> Iterator[Any]:
    try:
        yield entry
    finally:
        if entry._descriptor is not None:
            os.close(entry._descriptor)


def next_entry(stack: list[Any]):
    try:
        return next(stack[-1].iterator)
    except StopIteration:
        close_frame(stack.pop())
        return None
    except OSError as error:
        close_frame(stack.pop())
        raise ToolInputError("workspace traversal failed") from error


def close_frames(stack: list[Any]) -> None:
    while stack:
        close_frame(stack.pop())


def close_frame(frame: Any) -> None:
    try:
        frame.iterator.close()
    finally:
        if frame.descriptor is not None:
            os.close(frame.descriptor)
        windows_walk.close_guard(frame.guard_handle)
