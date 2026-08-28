"""Atomic compare-and-swap commit primitives for POSIX workspace files."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
import uuid

from .file_identity import (
    FileIdentity,
    MissingFile,
    MutationCommittedWarning,
    ToolInputError,
)

_ATOMIC_EXCHANGE = 0x2
_DARWIN_EXCLUSIVE = 0x4
_LINUX_NOREPLACE = 0x1
_DARWIN_SAFE_FILESYSTEMS = frozenset({"apfs"})


class _DarwinFsid(ctypes.Structure):
    _fields_ = (("value", ctypes.c_int32 * 2),)


class _DarwinStatfs(ctypes.Structure):
    _fields_ = (
        ("block_size", ctypes.c_uint32), ("io_size", ctypes.c_int32),
        ("blocks", ctypes.c_uint64), ("free_blocks", ctypes.c_uint64),
        ("available_blocks", ctypes.c_uint64), ("files", ctypes.c_uint64),
        ("free_files", ctypes.c_uint64), ("fsid", _DarwinFsid),
        ("owner", ctypes.c_uint32), ("kind", ctypes.c_uint32),
        ("flags", ctypes.c_uint32), ("subtype", ctypes.c_uint32),
        ("filesystem", ctypes.c_char * 16),
        ("mount_point", ctypes.c_char * 1024),
        ("mounted_from", ctypes.c_char * 1024),
        ("extended_flags", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 7),
    )


def _darwin_filesystem_name(parent: int) -> str:
    info = _DarwinStatfs()
    operation = ctypes.CDLL(None, use_errno=True).fstatfs
    operation.argtypes = (ctypes.c_int, ctypes.POINTER(_DarwinStatfs))
    operation.restype = ctypes.c_int
    if operation(parent, ctypes.byref(info)):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return bytes(info.filesystem).split(b"\0", 1)[0].decode("ascii", "strict")


def _require_safe_exchange_volume(parent: int) -> None:
    if sys.platform != "darwin":
        return
    if _darwin_filesystem_name(parent) not in _DARWIN_SAFE_FILESYSTEMS:
        raise OSError(errno.ENOTSUP, "atomic exchange is unsafe on this filesystem")


def _rename(parent: int, source: str, target: str, flags: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    name = "renameatx_np" if sys.platform == "darwin" else "renameat2"
    operation = getattr(libc, name, None)
    if operation is None:
        raise OSError(errno.ENOTSUP, "atomic file commit is unavailable")
    operation.argtypes = (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ctypes.c_uint,
    )
    operation.restype = ctypes.c_int
    if operation(parent, os.fsencode(source), parent, os.fsencode(target), flags):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), target)


def _exchange(parent: int, source: str, target: str) -> None:
    _require_safe_exchange_volume(parent)
    _rename(parent, source, target, _ATOMIC_EXCHANGE)


def _move_no_replace(parent: int, source: str, target: str) -> None:
    flag = _DARWIN_EXCLUSIVE if sys.platform == "darwin" else _LINUX_NOREPLACE
    _rename(parent, source, target, flag)


def _info(parent: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _same_metadata(info: os.stat_result | None, expected: FileIdentity) -> bool:
    if info is None or not stat.S_ISREG(info.st_mode):
        return False
    actual = FileIdentity.from_stat(info)
    return (
        actual.device, actual.inode, actual.mode, actual.size, actual.modified_ns
    ) == (
        expected.device, expected.inode, expected.mode, expected.size,
        expected.modified_ns,
    )


def _digest(parent: int, name: str, expected: FileIdentity) -> bytes | None:
    flags = os.O_RDONLY | os.O_NONBLOCK
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, dir_fd=parent)
    try:
        before = os.fstat(descriptor)
        if not _same_metadata(before, expected):
            return None
        digest = hashlib.sha256()
        remaining = expected.size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                return None
            digest.update(chunk)
            remaining -= len(chunk)
        if (
            os.read(descriptor, 1)
            or not FileIdentity.from_stat(before).matches_stat(os.fstat(descriptor))
        ):
            return None
        return digest.digest()
    finally:
        os.close(descriptor)


def _same_version(parent: int, name: str, expected: FileIdentity) -> bool:
    info = _info(parent, name)
    return (
        expected.digest is not None
        and _same_metadata(info, expected)
        and _digest(parent, name, expected) == expected.digest
    )


def discard(parent: int, name: str, expected: FileIdentity) -> bool:
    """Unlink a temporary name only when it is still the exact replacement."""
    if not _same_version(parent, name, expected):
        return False
    os.unlink(name, dir_fd=parent)
    return True


def _recovery_name(temporary: str) -> str:
    token = temporary.removeprefix(".deepseek-mcp-").removesuffix(".tmp")
    return f".deepseek-mcp-recovery-{token}-{uuid.uuid4().hex}.file"


def _park_displaced(parent: int, temporary: str) -> str:
    for _ in range(3):
        recovery = _recovery_name(temporary)
        try:
            _move_no_replace(parent, temporary, recovery)
            return recovery
        except FileExistsError:
            continue
    raise OSError(errno.EEXIST, "could not reserve a recovery file")


def _publish_missing(parent: int, temporary: str, target: str) -> None:
    try:
        _move_no_replace(parent, temporary, target)
    except FileExistsError:
        raise ToolInputError("write target appeared during edit") from None


def _restore_conflict(
    parent: int, recovery: str, target: str, replacement: FileIdentity,
) -> None:
    try:
        _exchange(parent, recovery, target)
    except OSError as error:
        raise MutationCommittedWarning(
            f"replacement committed; concurrent data retained as {recovery}"
        ) from error
    removed = _remove_owned_recovery(parent, recovery, replacement)
    os.fsync(parent)
    if removed:
        raise ToolInputError("write target changed during edit")
    raise ToolInputError(
        f"write target changed during edit; concurrent data retained as {recovery}"
    )


def _remove_owned_recovery(
    parent: int, recovery: str, replacement: FileIdentity,
) -> bool:
    try:
        if not _same_version(parent, recovery, replacement):
            return False
        os.unlink(recovery, dir_fd=parent)
        return True
    except OSError:
        return False


def _restore_after_audit_failure(
    parent: int, recovery: str, target: str, replacement: FileIdentity,
) -> None:
    try:
        _exchange(parent, recovery, target)
    except OSError as error:
        raise MutationCommittedWarning(
            f"replacement committed; displaced data retained as {recovery}"
        ) from error
    removed = _remove_owned_recovery(parent, recovery, replacement)
    os.fsync(parent)
    message = "commit audit failed; original restored"
    if not removed:
        message += f"; replacement retained as {recovery}"
    raise OSError(errno.EIO, message)


def commit(
    parent: int,
    temporary: str,
    target: str,
    baseline: FileIdentity | MissingFile,
    replacement: FileIdentity,
) -> None:
    """Publish ``temporary`` iff ``target`` still has ``baseline``."""
    if isinstance(baseline, MissingFile):
        _publish_missing(parent, temporary, target)
        return
    _exchange(parent, temporary, target)
    try:
        recovery = _park_displaced(parent, temporary)
    except BaseException:
        try:
            _exchange(parent, temporary, target)
        except OSError as error:
            raise MutationCommittedWarning(
                f"replacement committed; displaced data retained as {temporary}"
            ) from error
        raise
    try:
        baseline_matches = _same_version(parent, recovery, baseline)
    except OSError:
        _restore_after_audit_failure(
            parent, recovery, target, replacement
        )
    if baseline_matches:
        try:
            os.unlink(recovery, dir_fd=parent)
        except FileNotFoundError:
            return
        except OSError as error:
            raise MutationCommittedWarning(
                f"recovery cleanup not confirmed: {error}"
            ) from error
        return
    _restore_conflict(parent, recovery, target, replacement)
