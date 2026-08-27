"""Explicit Windows child inheritance for the workspace lease anchor."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


@dataclass
class ChildLeaseAnchor:
    startupinfo: object | None = None
    _parent_handle: int = -1

    @classmethod
    def create(cls, descriptor: int | None) -> "ChildLeaseAnchor":
        if os.name != "nt" or descriptor is None:
            return cls()
        try:
            os.fstat(descriptor)
        except OSError:
            raise RuntimeError("workspace execution lease is unavailable") from None
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_process = kernel.GetCurrentProcess
        get_current_process.argtypes = ()
        get_current_process.restype = wintypes.HANDLE
        current_process = get_current_process()
        duplicate = kernel.DuplicateHandle
        duplicate.argtypes = (
            wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL,
            wintypes.DWORD,
        )
        duplicate.restype = wintypes.BOOL
        inherited = wintypes.HANDLE()
        if not duplicate(
            current_process, msvcrt.get_osfhandle(descriptor), current_process,
            ctypes.byref(inherited), 0, True, 0x2,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": [int(inherited.value)]}
        return cls(startup, int(inherited.value))

    def close_parent_copy(self) -> None:
        handle = self._parent_handle
        if handle < 0:
            return
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        if not close_handle(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        self._parent_handle = -1
