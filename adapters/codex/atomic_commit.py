"""Atomic pathname primitives for Codex configuration commits."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
import uuid
from collections.abc import Callable


class UnsupportedAtomicCommit(OSError):
    """The platform or filesystem cannot provide the required atomic commit."""


_RENAME_NOREPLACE = 0x00000001
_RENAME_EXCHANGE = 0x00000002
_RENAME_SWAP = 0x00000002
_RENAME_EXCL = 0x00000004
_MNT_EXT_FSKIT = 0x00000002
_UNSUPPORTED = {
    errno.EINVAL,
    errno.ENOSYS,
    getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    errno.EOPNOTSUPP,
}


class _DarwinStatFs(ctypes.Structure):
    _fields_ = (
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    )


def _validate_name(name: str) -> bytes:
    if not name or os.sep in name or (os.altsep and os.altsep in name):
        raise ValueError("atomic commit names must be single path components")
    return os.fsencode(name)


def _libc_call(symbol: str, directory: int, source: str, target: str, flag: int) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, symbol, None)
    if function is None:
        raise UnsupportedAtomicCommit(f"{symbol} is unavailable")
    function.argtypes = (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    result = function(
        directory, _validate_name(source), directory, _validate_name(target), flag
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in _UNSUPPORTED:
        raise UnsupportedAtomicCommit(os.strerror(error))
    raise OSError(error, os.strerror(error), target)


def _require_safe_darwin_swap(directory: int) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    function = library.fstatfs
    function.argtypes = (ctypes.c_int, ctypes.POINTER(_DarwinStatFs))
    function.restype = ctypes.c_int
    info = _DarwinStatFs()
    if function(directory, ctypes.byref(info)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    filesystem = bytes(info.f_fstypename).split(b"\0", 1)[0]
    if filesystem != b"apfs" or info.f_flags_ext & _MNT_EXT_FSKIT:
        raise UnsupportedAtomicCommit("safe atomic exchange requires local APFS")


def exchange(directory: int, source: str, target: str) -> None:
    """Atomically exchange two existing names anchored to one directory."""
    if sys.platform == "darwin":
        _require_safe_darwin_swap(directory)
        _libc_call("renameatx_np", directory, source, target, _RENAME_SWAP)
        return
    if sys.platform.startswith("linux"):
        _libc_call("renameat2", directory, source, target, _RENAME_EXCHANGE)
        return
    raise UnsupportedAtomicCommit("atomic exchange is unavailable on this platform")


def move_no_clobber(directory: int, source: str, target: str) -> None:
    """Atomically rename source to an absent target without replacing it."""
    if sys.platform == "darwin":
        _libc_call("renameatx_np", directory, source, target, _RENAME_EXCL)
        return
    if sys.platform.startswith("linux"):
        _libc_call("renameat2", directory, source, target, _RENAME_NOREPLACE)
        return
    raise UnsupportedAtomicCommit("exclusive rename is unavailable on this platform")


def discard(directory: int, name: str) -> None:
    """Remove a private transaction pathname, tolerating only absence."""
    try:
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass


def discard_best_effort(directory: int, name: str) -> None:
    """Clean up a private pathname without masking an earlier failure."""
    try:
        discard(directory, name)
    except OSError:
        pass


def windows_replace(replaced: str, replacement: str, backup: str) -> None:
    """Invoke ReplaceFileW without unsupported durability flags."""
    if os.name != "nt":
        raise UnsupportedAtomicCommit("ReplaceFileW is unavailable")
    from ctypes import wintypes

    function = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    function.argtypes = (
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        wintypes.LPVOID, wintypes.LPVOID,
    )
    function.restype = wintypes.BOOL
    if not function(replaced, replacement, backup, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def windows_replace_cas(
    target: str, temporary: str, expected: tuple, transaction: tuple,
    replace: Callable[[str, str, str], None],
    snapshot: Callable[[str], tuple | None],
    discard_name: Callable[[str], None],
    preserve: Callable[[str, str], str],
    recover_failed: Callable[[str, str], None],
) -> tuple[bool, str | None]:
    """Replace target while retaining and validating the displaced version."""
    backup = f".{target}.{uuid.uuid4().hex}.displaced"
    try:
        replace(target, temporary, backup)
    except OSError:
        recover_failed(target, backup)
        raise
    displaced = snapshot(backup)
    if displaced == expected:
        discard_name(backup)
        return True, None
    if snapshot(target) != transaction:
        return False, preserve(backup, target)
    recovery = f".{target}.{uuid.uuid4().hex}.recovery"
    try:
        replace(target, backup, recovery)
    except OSError:
        recover_failed(target, recovery)
        return False, backup
    if snapshot(recovery) == transaction:
        discard_name(recovery)
        return False, None
    return False, recovery
