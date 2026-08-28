"""Run deadlines that advance across sleep without trusting wall time alone."""
from __future__ import annotations

import time
import os
import sys
from dataclasses import dataclass


def _darwin_continuous_time() -> float:
    import ctypes

    class _Timebase(ctypes.Structure):
        _fields_ = (("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32))

    system = ctypes.CDLL(None)
    system.mach_continuous_time.restype = ctypes.c_uint64
    system.mach_timebase_info.argtypes = (ctypes.POINTER(_Timebase),)
    info = _Timebase()
    if system.mach_timebase_info(ctypes.byref(info)) != 0 or not info.denom:
        raise RuntimeError("continuous system clock is unavailable")
    ticks = system.mach_continuous_time()
    return ticks * info.numer / info.denom / 1_000_000_000


def _windows_continuous_time() -> float:
    import ctypes

    ticks = ctypes.WinDLL("kernel32", use_last_error=True).GetTickCount64
    ticks.restype = ctypes.c_ulonglong
    return ticks() / 1_000


def continuous_time() -> float:
    """Suspend-aware monotonic clock on every supported production platform."""
    if sys.platform == "darwin":
        return _darwin_continuous_time()
    if os.name == "nt":
        return _windows_continuous_time()
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    if clock is not None:
        return time.clock_gettime(clock)
    return time.monotonic()


@dataclass(frozen=True)
class HardDeadline:
    monotonic_end: float
    realtime_end: float
    continuous_end: float | None = None

    @classmethod
    def after(cls, seconds: float) -> "HardDeadline":
        return cls(
            time.monotonic() + seconds,
            time.time() + seconds,
            continuous_time() + seconds,
        )

    def remaining(self) -> float:
        values = [
            self.monotonic_end - time.monotonic(),
            self.realtime_end - time.time(),
        ]
        if self.continuous_end is not None:
            values.append(self.continuous_end - continuous_time())
        return min(values)

    def limited(self, seconds: float) -> "HardDeadline":
        return HardDeadline(
            min(self.monotonic_end, time.monotonic() + seconds),
            min(self.realtime_end, time.time() + seconds),
            min(
                self.continuous_end, continuous_time() + seconds
            ) if self.continuous_end is not None else None,
        )


Deadline = HardDeadline | float


def remaining(deadline: Deadline) -> float:
    if isinstance(deadline, HardDeadline):
        return deadline.remaining()
    return deadline - time.monotonic()


def limited(deadline: Deadline | None, seconds: float) -> tuple[Deadline, bool]:
    if deadline is None:
        return HardDeadline.after(seconds), False
    run_remaining = remaining(deadline)
    if run_remaining <= seconds:
        return deadline, True
    if isinstance(deadline, HardDeadline):
        return deadline.limited(seconds), False
    return time.monotonic() + seconds, False
