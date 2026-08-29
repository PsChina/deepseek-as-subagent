"""Handle-relative Windows rename using the native file-information contract."""
from __future__ import annotations

import os


if os.name == "nt":  # pragma: no cover - exercised by the Windows CI matrix
    import ctypes
    import msvcrt
    from ctypes import wintypes

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

    _NTDLL = ctypes.WinDLL("ntdll")
    _NT_SET_INFORMATION = _NTDLL.NtSetInformationFile
    _NT_SET_INFORMATION.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    )
    _NT_SET_INFORMATION.restype = wintypes.LONG
    _NT_TO_DOS_ERROR = _NTDLL.RtlNtStatusToDosError
    _NT_TO_DOS_ERROR.argtypes = (wintypes.LONG,)
    _NT_TO_DOS_ERROR.restype = wintypes.ULONG


_FILE_RENAME_INFORMATION = 10


def _rename_buffer(parent: int, name: str, replace: bool):
    encoded = name.encode("utf-16-le")
    offset = _RenameInfo.name.offset
    size = max(ctypes.sizeof(_RenameInfo), offset + len(encoded))
    buffer = ctypes.create_string_buffer(size)
    info = ctypes.cast(buffer, ctypes.POINTER(_RenameInfo)).contents
    info.replace = 1 if replace else 0
    info.root = parent
    info.name_length = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
    return buffer, size


def rename(descriptor: int, parent: int, name: str, *, replace: bool) -> None:
    """Rename an open file relative to an already validated directory handle."""
    if os.name != "nt":
        raise OSError("Windows handle rename is unavailable")
    if not name or "\x00" in name:
        raise OSError("Windows rename target is invalid")
    buffer, size = _rename_buffer(parent, name, replace)
    io_status = _IoStatusBlock()
    status = int(_NT_SET_INFORMATION(
        msvcrt.get_osfhandle(descriptor),
        ctypes.byref(io_status),
        ctypes.cast(buffer, wintypes.LPVOID),
        size,
        _FILE_RENAME_INFORMATION,
    ))
    if status < 0:
        raise ctypes.WinError(int(_NT_TO_DOS_ERROR(status)))
