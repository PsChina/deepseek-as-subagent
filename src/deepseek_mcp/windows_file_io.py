"""Handle-anchored validation and reads for private Windows paths."""
from __future__ import annotations

import ntpath
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from . import windows_acl

MAX_PRIVATE_FILE_BYTES = 1024 * 1024
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_DIRECTORY = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)


class WindowsPathError(RuntimeError):
    pass


@dataclass
class _Directory:
    handle: int
    expected: str


if os.name == "nt":  # pragma: no cover - exercised by the Windows CI matrix
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CREATE_FILE = _KERNEL32.CreateFileW
    _CREATE_FILE.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    _CREATE_FILE.restype = wintypes.HANDLE
    _CLOSE_HANDLE = _KERNEL32.CloseHandle
    _CLOSE_HANDLE.argtypes = (wintypes.HANDLE,)
    _CLOSE_HANDLE.restype = wintypes.BOOL
    _FINAL_PATH = _KERNEL32.GetFinalPathNameByHandleW
    _FINAL_PATH.argtypes = (
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
    )
    _FINAL_PATH.restype = wintypes.DWORD
    _GET_HANDLE_INFO = _KERNEL32.GetFileInformationByHandleEx
    _GET_HANDLE_INFO.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    )
    _GET_HANDLE_INFO.restype = wintypes.BOOL
    _GET_DRIVE_TYPE = _KERNEL32.GetDriveTypeW
    _GET_DRIVE_TYPE.argtypes = (wintypes.LPCWSTR,)
    _GET_DRIVE_TYPE.restype = wintypes.UINT

    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = (("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD))


_READ, _WRITE = 0x80000000, 0x40000000
_SHARE_ALL = 7
_OPEN_EXISTING, _OPEN_ALWAYS = 3, 4
_OPEN_REPARSE, _BACKUP_SEMANTICS = 0x00200000, 0x02000000
_ATTRIBUTE_TAG_INFO = 9
_LOCAL_DRIVE_TYPES = frozenset({2, 3, 6})


def _require_windows() -> None:
    if os.name != "nt":
        raise WindowsPathError("Windows handle validation is unavailable")


def _normalized(path: str) -> str:
    value = path
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value))


def _require_local_drive(drive: str) -> None:
    root = drive + "\\"
    if _GET_DRIVE_TYPE(root) not in _LOCAL_DRIVE_TYPES:
        raise WindowsPathError("Windows path must be on a local drive")


def _absolute_local(path: Path) -> str:
    _require_windows()
    raw = os.path.abspath(os.fspath(path))
    drive, tail = ntpath.splitdrive(raw)
    if not drive or drive.startswith("\\") or not tail.startswith("\\"):
        raise WindowsPathError("Windows path must be on a local drive")
    for part in (piece for piece in tail.split("\\") if piece):
        if part in {".", ".."} or ":" in part or part.rstrip(" .") != part:
            raise WindowsPathError("Windows path contains an unsafe component")
    _require_local_drive(drive)
    return _normalized(raw)


def _open(path: str, *, directory: bool) -> int:
    flags = _OPEN_REPARSE | (_BACKUP_SEMANTICS if directory else 0)
    handle = _CREATE_FILE(
        path, _READ, _SHARE_ALL, None, _OPEN_EXISTING, flags, None
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


def _close(handle: int) -> None:
    if not _CLOSE_HANDLE(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _final_path(handle: int) -> str:
    size = _FINAL_PATH(handle, None, 0, 0)
    if not size:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(size + 1)
    written = _FINAL_PATH(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return _normalized(buffer.value)


def _attributes(handle: int) -> int:
    info = _AttributeTagInfo()
    ok = _GET_HANDLE_INFO(
        handle, _ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info)
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(info.attributes)


def _validate_handle(handle: int, expected: str, *, directory: bool) -> None:
    attributes = _attributes(handle)
    actual_directory = bool(attributes & _DIRECTORY)
    if attributes & _REPARSE or actual_directory != directory:
        raise WindowsPathError("Windows path is a reparse point or wrong type")
    if _final_path(handle) != expected:
        raise WindowsPathError("Windows path escaped its expected location")


def _validate_acl(handle: int) -> None:
    try:
        windows_acl.validate_private_handle(handle)
    except windows_acl.WindowsAclError as exc:
        raise WindowsPathError("Windows path has an unsafe owner or DACL") from exc


def _open_directory(path: Path) -> _Directory:
    absolute = _absolute_local(path)
    drive, tail = ntpath.splitdrive(absolute)
    current = _normalized(drive + "\\")
    handle = _open(current, directory=True)
    try:
        _validate_handle(handle, current, directory=True)
        for part in (piece for piece in tail.split("\\") if piece):
            candidate = _normalized(ntpath.join(current, part))
            child = _open(candidate, directory=True)
            try:
                _validate_handle(child, candidate, directory=True)
            except BaseException:
                _close(child)
                raise
            _close(handle)
            handle, current = child, candidate
        _validate_acl(handle)
        return _Directory(handle, current)
    except BaseException:
        _close(handle)
        raise


def _open_child(parent: _Directory, name: str) -> int:
    if ntpath.basename(name) != name or name in {"", ".", ".."}:
        raise WindowsPathError("Windows child path name is invalid")
    if _final_path(parent.handle) != parent.expected:
        raise WindowsPathError("Windows parent directory changed during access")
    expected = _normalized(ntpath.join(parent.expected, name))
    handle = _open(expected, directory=False)
    try:
        _validate_handle(handle, expected, directory=False)
        _validate_acl(handle)
        if _final_path(parent.handle) != parent.expected:
            raise WindowsPathError("Windows parent directory changed during access")
        return handle
    except BaseException:
        _close(handle)
        raise


def _descriptor(handle: int) -> int:
    descriptor = -1
    try:
        descriptor = msvcrt.open_osfhandle(
            handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
        os.set_inheritable(descriptor, False)
        return descriptor
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        else:
            _close(handle)
        raise


def open_exclusive_regular(path: Path) -> int:
    """Open/create a private regular file with a process-lifetime share deny."""
    parent = _open_directory(path.parent)
    handle = descriptor = -1
    try:
        expected = _normalized(ntpath.join(parent.expected, path.name))
        opened = _CREATE_FILE(
            expected, _READ | _WRITE, 0, None, _OPEN_ALWAYS, _OPEN_REPARSE, None
        )
        if opened in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        handle = int(opened)
        _validate_handle(handle, expected, directory=False)
        _validate_acl(handle)
        if _final_path(parent.handle) != parent.expected:
            raise WindowsPathError("Windows parent directory changed during access")
        descriptor = msvcrt.open_osfhandle(
            handle, os.O_RDWR | getattr(os, "O_BINARY", 0)
        )
        handle = -1
        os.set_inheritable(descriptor, False)
        return descriptor
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        elif handle >= 0:
            _close(handle)
        raise
    finally:
        _close(parent.handle)


def validate_private_path(path: Path, *, directory: bool) -> None:
    """Validate local-drive containment, components, final handle, owner, and DACL."""
    if directory:
        opened = _open_directory(path)
        _close(opened.handle)
        return
    parent = _open_directory(path.parent)
    try:
        handle = _open_child(parent, path.name)
        _close(handle)
    finally:
        _close(parent.handle)


def validate_private_descriptor(
    descriptor: int, path: Path, *, directory: bool = False
) -> None:
    """Validate a newly opened descriptor against its exact expected path."""
    expected = _absolute_local(path)
    handle = msvcrt.get_osfhandle(descriptor)
    _validate_handle(handle, expected, directory=directory)
    _validate_acl(handle)


def _read_descriptor(descriptor: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        chunk = os.read(descriptor, min(65536, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > max_bytes:
        raise WindowsPathError("Windows private file is too large")
    return b"".join(chunks)


def read_regular(
    path: Path, *, max_bytes: int = MAX_PRIVATE_FILE_BYTES
) -> tuple[bytes, os.stat_result]:
    """Read a private regular file from the same validated Windows handle."""
    parent = _open_directory(path.parent)
    descriptor = -1
    try:
        handle = _open_child(parent, path.name)
        descriptor = _descriptor(handle)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise WindowsPathError("Windows private file is not uniquely regular")
        data = _read_descriptor(descriptor, max_bytes)
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if len(data) != before.st_size or any(
            getattr(before, name) != getattr(after, name) for name in fields
        ):
            raise WindowsPathError("Windows private file changed while it was read")
        return data, after
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _close(parent.handle)
