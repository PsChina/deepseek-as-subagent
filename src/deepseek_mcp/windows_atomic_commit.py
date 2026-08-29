"""Windows atomic commit helpers with displaced-file verification."""
from __future__ import annotations

import os
import uuid
from collections.abc import Callable

from .file_identity import FileIdentity, MissingFile, ToolInputError

_ALREADY_EXISTS = frozenset({80, 183})
_PARTIAL_REPLACE = 1177


class RollbackCompletedWarning(OSError):
    """Rollback ran, but its post-operation parent audit was inconclusive."""


def same_version(actual: FileIdentity, expected: FileIdentity) -> bool:
    """Compare stable metadata plus the bounded content digest."""
    inode_matches = (
        not actual.inode
        or not expected.inode
        or (actual.device, actual.inode) == (expected.device, expected.inode)
    )
    return inode_matches and (
        actual.mode,
        actual.size,
        actual.modified_ns,
        actual.digest,
    ) == (
        expected.mode,
        expected.size,
        expected.modified_ns,
        expected.digest,
    )


def _recovery_name(temporary: str) -> str:
    token = temporary.removeprefix(".deepseek-mcp-").removesuffix(".tmp")
    return f".deepseek-mcp-recovery-{token}-{uuid.uuid4().hex}.file"


def _exists_error(error: OSError) -> bool:
    return isinstance(error, FileExistsError) or getattr(
        error, "winerror", None
    ) in _ALREADY_EXISTS


def _restore(
    target: str,
    displaced: str,
    replacement: FileIdentity,
    rollback: Callable[[str, str, str], None],
    discard: Callable[[str, FileIdentity], bool],
    *,
    conflict: bool,
) -> None:
    recovery = _recovery_name(displaced)
    try:
        rollback(target, displaced, recovery)
    except RollbackCompletedWarning as error:
        raise OSError(
            "commit verification failed; rollback audit is uncertain; "
            f"replacement retained as {recovery}"
        ) from error
    except OSError as error:
        raise OSError(
            f"commit verification failed; original retained as {displaced}"
        ) from error
    try:
        removed = discard(recovery, replacement)
    except OSError:
        removed = False
    label = "write target changed during edit" if conflict else (
        "commit audit failed; original restored"
    )
    if not removed:
        label += f"; concurrent data retained as {recovery}"
    error_type = ToolInputError if conflict else OSError
    raise error_type(label)


def commit(
    temporary: str,
    target: str,
    baseline: FileIdentity | MissingFile,
    replacement: FileIdentity,
    publish: Callable[[str, str], None],
    replace: Callable[[str, str, str], None],
    discard: Callable[[str, FileIdentity], bool],
    rollback: Callable[[str, str, str], None] | None = None,
) -> None:
    """Publish a temporary file and restore any non-baseline displacement."""
    if isinstance(baseline, MissingFile):
        try:
            publish(temporary, target)
        except OSError as error:
            if _exists_error(error):
                raise ToolInputError(
                    "write target appeared during edit"
                ) from None
            raise
        return
    rollback = rollback or replace
    displaced = _recovery_name(temporary)
    replace(target, temporary, displaced)
    try:
        matched = discard(displaced, baseline)
    except OSError:
        _restore(
            target, displaced, replacement, rollback, discard, conflict=False
        )
    if matched:
        return
    _restore(target, displaced, replacement, rollback, discard, conflict=True)


def replace_paths(target: str, replacement: str, backup: str) -> None:
    """Call ReplaceFileW without flags; callers recover documented partial errors."""
    if os.name != "nt":
        raise OSError("ReplaceFileW is unavailable")
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = kernel.ReplaceFileW
    operation.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    operation.restype = wintypes.BOOL
    if not operation(target, replacement, backup, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def is_partial_replace(error: OSError) -> bool:
    return getattr(error, "winerror", None) == _PARTIAL_REPLACE


def rename(
    descriptor: int, parent: int, name: str, *, replace: bool,
) -> None:
    """Rename one open file relative to an anchored directory handle.

    SetFileInformationByHandle(FileRenameInfo) rejects a non-null
    RootDirectory on some supported Windows builds. NtSetInformationFile uses
    the native FILE_RENAME_INFORMATION contract and reliably supports the
    handle-relative form required here to keep the destination anchored.
    """
    if os.name != "nt":
        raise OSError("Windows handle rename is unavailable")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    if not name or "\x00" in name:
        raise OSError("Windows rename target is invalid")

    class _RenameMode(ctypes.Union):
        _fields_ = (
            ("replace", wintypes.BOOLEAN),
            ("flags", wintypes.ULONG),
        )

    class _RenameInfo(ctypes.Structure):
        _anonymous_ = ("mode",)
        _fields_ = (
            ("mode", _RenameMode),
            ("root", wintypes.HANDLE),
            ("name_length", wintypes.ULONG),
            ("name", wintypes.WCHAR * 1),
        )

    class _IoStatusValue(ctypes.Union):
        _fields_ = (
            ("status", wintypes.LONG),
            ("pointer", wintypes.LPVOID),
        )

    class _IoStatusBlock(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = (
            ("value", _IoStatusValue),
            ("information", ctypes.c_size_t),
        )

    encoded = name.encode("utf-16-le")
    name_offset = _RenameInfo.name.offset
    buffer_size = max(ctypes.sizeof(_RenameInfo), name_offset + len(encoded))
    buffer = ctypes.create_string_buffer(buffer_size)
    info = ctypes.cast(buffer, ctypes.POINTER(_RenameInfo)).contents
    info.replace = 1 if replace else 0
    info.root = parent
    info.name_length = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded, len(encoded))

    ntdll = ctypes.WinDLL("ntdll")
    operation = ntdll.NtSetInformationFile
    operation.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    )
    operation.restype = wintypes.LONG
    to_dos_error = ntdll.RtlNtStatusToDosError
    to_dos_error.argtypes = (wintypes.LONG,)
    to_dos_error.restype = wintypes.ULONG

    io_status = _IoStatusBlock()
    status = int(operation(
        msvcrt.get_osfhandle(descriptor),
        ctypes.byref(io_status),
        ctypes.byref(buffer),
        buffer_size,
        10,  # FileRenameInformation
    ))
    if status < 0:
        raise ctypes.WinError(int(to_dos_error(status)))


def mark_delete(descriptor: int) -> None:
    """Mark the exact open file object for deletion when its handles close."""
    if os.name != "nt":
        raise OSError("Windows handle deletion is unavailable")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _DispositionInfo(ctypes.Structure):
        _fields_ = (("delete_file", wintypes.BOOLEAN),)

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = kernel.SetFileInformationByHandle
    info = _DispositionInfo(True)
    if not operation(
        msvcrt.get_osfhandle(descriptor), 4,
        ctypes.byref(info), ctypes.sizeof(info),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
