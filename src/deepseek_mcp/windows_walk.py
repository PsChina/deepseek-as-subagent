"""Windows handle checks shared by the bounded workspace walker."""
from __future__ import annotations

import ntpath
import os
from pathlib import Path

from . import file_io
from .file_io import ToolInputError

_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

if os.name == "nt":  # pragma: no cover - exercised by the Windows CI matrix
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = (
            ("creation_time", ctypes.c_longlong),
            ("last_access_time", ctypes.c_longlong),
            ("last_write_time", ctypes.c_longlong),
            ("change_time", ctypes.c_longlong),
            ("attributes", wintypes.DWORD),
        )

_FILE_BASIC_INFO = 0


def safe_part(part: str) -> bool:
    if not part or ":" in part or part.rstrip(" .") != part:
        return False
    return part.split(".", 1)[0].upper() not in _RESERVED_STEMS


def safe_path_text(value: str) -> bool:
    _drive, tail = ntpath.splitdrive(value)
    parts = (part for part in tail.replace("/", "\\").split("\\") if part)
    return all(safe_part(part) for part in parts)


def _expected(path: Path) -> str:
    return ntpath.normcase(ntpath.normpath(os.path.abspath(os.fspath(path))))


def open_guard(path: Path, *, directory: bool) -> int:
    handle = file_io._win_open_path(
        os.fspath(path), file_io._WIN_READ, file_io._WIN_OPEN_EXISTING,
        directory=directory,
    )
    try:
        file_io._win_validate_handle(handle, directory=directory)
        if file_io._win_final_path(handle) != _expected(path):
            raise ToolInputError("workspace path escaped during traversal")
        return handle
    except BaseException:
        file_io._win_close(handle)
        raise


def close_guard(handle: int | None) -> None:
    if handle is not None:
        file_io._win_close(handle)


def handle_matches(handle: int, path: Path, *, directory: bool) -> bool:
    try:
        file_io._win_validate_handle(handle, directory=directory)
        return file_io._win_final_path(handle) == _expected(path)
    except (OSError, ToolInputError):
        return False


def descriptor_matches(descriptor: int, path: Path) -> bool:
    try:
        import msvcrt

        return handle_matches(msvcrt.get_osfhandle(descriptor), path, directory=False)
    except (OSError, ToolInputError):
        return False


def descriptor_change_time(descriptor: int) -> int | None:
    """Return the NTFS change-time token for an open descriptor on Windows."""
    if os.name != "nt":
        return None
    info = _FileBasicInfo()
    handle = msvcrt.get_osfhandle(descriptor)
    ok = file_io._GET_HANDLE_INFO(
        handle, _FILE_BASIC_INFO, ctypes.byref(info), ctypes.sizeof(info)
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(info.change_time)
