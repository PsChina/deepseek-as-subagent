"""Event-driven parent-death boundary for isolated helper processes."""
from __future__ import annotations

import math
import os
import re
import select
import stat
import subprocess
from dataclasses import dataclass

from .hard_deadline import HardDeadline, remaining

_LIVENESS_ATTRIBUTE = "_deepseek_parent_liveness"
_POSIX_TOKEN = re.compile(r"fd:([0-9]+)")
_WINDOWS_TOKEN = re.compile(r"pid:([0-9]+)")


def _close_fd(descriptor: int) -> None:
    if descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


@dataclass
class ParentLiveness:
    """Parent-owned endpoint whose loss wakes exactly one child watchdog."""

    argument: str
    inherited_fds: tuple[int, ...] = ()
    _child_fd: int = -1
    _parent_fd: int = -1

    @classmethod
    def create(cls) -> "ParentLiveness":
        if os.name != "posix":
            return cls(f"pid:{os.getpid()}")
        child_fd = parent_fd = -1
        try:
            child_fd, parent_fd = os.pipe()
            os.set_inheritable(child_fd, False)
            os.set_inheritable(parent_fd, False)
            return cls(
                f"fd:{child_fd}",
                (child_fd,),
                _child_fd=child_fd,
                _parent_fd=parent_fd,
            )
        except BaseException:
            _close_fd(child_fd)
            _close_fd(parent_fd)
            raise

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        setattr(process, _LIVENESS_ATTRIBUTE, self)
        _close_fd(self._child_fd)
        self._child_fd = -1

    def close(self) -> None:
        _close_fd(self._child_fd)
        _close_fd(self._parent_fd)
        self._child_fd = -1
        self._parent_fd = -1


def close_parent_liveness(process: subprocess.Popen[bytes]) -> None:
    liveness = getattr(process, _LIVENESS_ATTRIBUTE, None)
    if isinstance(liveness, ParentLiveness):
        liveness.close()


def _posix_wait(argument: str, timeout: float) -> None:
    matched = _POSIX_TOKEN.fullmatch(argument)
    if matched is None:
        return
    descriptor = int(matched.group(1))
    deadline = HardDeadline.after(timeout)
    try:
        info = os.fstat(descriptor)
        if descriptor <= 2 or not stat.S_ISFIFO(info.st_mode):
            return
        while (left := remaining(deadline)) > 0:
            readable, _, _ = select.select(
                [descriptor], [], [], min(1.0, left)
            )
            if readable:
                os.read(descriptor, 1)
                return
    except (OSError, ValueError):
        return


def _windows_wait(argument: str, timeout: float) -> None:
    matched = _WINDOWS_TOKEN.fullmatch(argument)
    if matched is None:
        return
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    wait = kernel.WaitForSingleObject
    wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait.restype = wintypes.DWORD
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    process = open_process(
        synchronize, False, int(matched.group(1))
    )
    if not process:
        return
    try:
        deadline = HardDeadline.after(timeout)
        while (left := remaining(deadline)) > 0:
            milliseconds = min(1000, max(1, math.ceil(left * 1000)))
            result = wait(process, milliseconds)
            if result == 0:
                return
            if result == 0xFFFFFFFF:
                return
            if result not in (0x00000102, 0xFFFFFFFF):
                return
    finally:
        close(process)


def wait_for_parent_loss_or_timeout(argument: str, timeout: float) -> None:
    """Block on parent loss or the absolute child safety timeout."""
    if not math.isfinite(timeout) or timeout <= 0:
        return
    if os.name == "posix":
        _posix_wait(argument, timeout)
    else:  # pragma: no cover - exercised by the Windows CI matrix
        _windows_wait(argument, timeout)
