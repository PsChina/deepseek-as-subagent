"""Fail-closed process limits for components that hold provider credentials."""
from __future__ import annotations

import os
import sys

PROVIDER_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024
_WER_FAULT_REPORTING_FLAG_NOHEAP = 0x00000001


def _disable_windows_heap_reporting() -> None:
    try:
        import ctypes
        from ctypes import wintypes

        set_flags = ctypes.WinDLL("kernel32", use_last_error=True).WerSetFlags
        set_flags.argtypes = (wintypes.DWORD,)
        set_flags.restype = ctypes.c_long
        result = set_flags(_WER_FAULT_REPORTING_FLAG_NOHEAP)
    except (AttributeError, OSError) as error:
        raise RuntimeError("Windows crash heap reporting could not be disabled") from error
    if result != 0:
        raise RuntimeError("Windows crash heap reporting could not be disabled")


def disable_core_dumps() -> None:
    if os.name == "nt":
        _disable_windows_heap_reporting()
        return
    if os.name != "posix":
        return
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, OSError, ValueError) as error:
        raise RuntimeError("core dumps could not be disabled") from error


def harden_provider_process() -> None:
    disable_core_dumps()
    # Darwin's documented setrlimit resources do not include RLIMIT_AS, and
    # modern macOS processes reserve very large sparse address ranges. Applying
    # a Linux-sized address-space limit there prevents ordinary HTTP clients
    # from allocating. The decoded response cap remains platform independent.
    if not sys.platform.startswith("linux"):
        return
    try:
        import resource

        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        target = PROVIDER_MEMORY_LIMIT_BYTES
        if hard != resource.RLIM_INFINITY:
            target = min(target, hard)
        if target <= 0:
            raise ValueError("invalid address-space limit")
        resource.setrlimit(resource.RLIMIT_AS, (target, target))
    except (ImportError, OSError, ValueError) as error:
        raise RuntimeError("provider memory limit could not be applied") from error
