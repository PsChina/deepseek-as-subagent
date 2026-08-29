"""Handle-anchored Windows file operations for Codex config transactions."""
from __future__ import annotations

from contextlib import suppress
import hashlib
import ntpath
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from . import atomic_commit, windows_acl
else:
    import atomic_commit
    import windows_acl
from deepseek_mcp import windows_file_io as _shared_windows_file_io

MAX_CONFIG_BYTES = 1024 * 1024
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_DIRECTORY = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)


class WindowsPathError(RuntimeError):
    pass


@dataclass
class _Directory:
    handle: int
    expected: str
    ancestors: tuple[int, ...] = ()


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
    _FINAL_PATH.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    _FINAL_PATH.restype = wintypes.DWORD
    _GET_HANDLE_INFO = _KERNEL32.GetFileInformationByHandleEx
    _GET_HANDLE_INFO.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _GET_HANDLE_INFO.restype = wintypes.BOOL
    _SET_HANDLE_INFO = _KERNEL32.SetFileInformationByHandle
    _SET_HANDLE_INFO.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _SET_HANDLE_INFO.restype = wintypes.BOOL
    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = (("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD))

    class _DispositionInfo(ctypes.Structure):
        _fields_ = (("delete_file", wintypes.BOOLEAN),)


_READ, _WRITE, _DELETE = 0x80000000, 0x40000000, 0x00010000
_SHARE_RW, _SHARE_ALL = 3, 7
_CREATE_NEW, _OPEN_EXISTING, _OPEN_ALWAYS = 1, 3, 4
_OPEN_REPARSE, _BACKUP_SEMANTICS = 0x00200000, 0x02000000
_ATTRIBUTE_TAG_INFO, _RENAME_INFO, _DISPOSITION_INFO = 9, 3, 4


def _require_windows() -> None:
    if os.name != "nt":
        raise WindowsPathError("Windows handle backend is unavailable")


def _normalized(path: str) -> str:
    value = path
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value))


def _absolute_local(path: Path) -> str:
    raw = os.path.abspath(os.fspath(path))
    drive, tail = ntpath.splitdrive(raw)
    if not drive or drive.startswith("\\") or not tail.startswith("\\"):
        raise WindowsPathError("Codex config path must be on a local drive")
    for part in (piece for piece in tail.split("\\") if piece):
        if ":" in part or part.rstrip(" .") != part:
            raise WindowsPathError("Codex config path contains an unsafe component")
    try:
        _shared_windows_file_io._require_local_drive(drive)
    except _shared_windows_file_io.WindowsPathError as exc:
        raise WindowsPathError("Codex config path must be on a local drive") from exc
    return _normalized(raw)


def _open(path: str, access: int, creation: int, *, directory: bool, share: int) -> int:
    _require_windows()
    flags = _OPEN_REPARSE | (_BACKUP_SEMANTICS if directory else 0)
    handle = _CREATE_FILE(path, access, share, None, creation, flags, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


def _close(handle: int) -> None:
    if not _CLOSE_HANDLE(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _close_parent(parent: _Directory) -> None:
    try:
        _close(parent.handle)
    finally:
        for handle in reversed(parent.ancestors):
            with suppress(OSError):
                _close(handle)


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
    ok = _GET_HANDLE_INFO(handle, _ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(info.attributes)


def _validate(handle: int, expected: str, *, directory: bool) -> str:
    attributes = _attributes(handle)
    actual_directory = bool(attributes & _DIRECTORY)
    if attributes & _REPARSE or actual_directory != directory:
        raise WindowsPathError("Codex config path is a reparse point or wrong type")
    canonical = _shared_windows_file_io._canonical_existing_path(expected)
    if _final_path(handle) != canonical:
        raise WindowsPathError("Codex config path escaped its expected location")
    return canonical


def _validate_acl(handle: int) -> None:
    try:
        windows_acl.validate_private_handle(handle)
    except windows_acl.WindowsAclError as exc:
        raise WindowsPathError("Codex config path has an unsafe ACL") from exc


def _open_parent(path: Path) -> _Directory:
    absolute = _absolute_local(path)
    parent = ntpath.dirname(absolute)
    drive, tail = ntpath.splitdrive(parent)
    current = _normalized(drive + "\\")
    handle = _open(current, _READ, _OPEN_EXISTING, directory=True, share=_SHARE_RW)
    ancestors: list[int] = []
    try:
        current = _validate(handle, current, directory=True)
        for part in (piece for piece in tail.split("\\") if piece):
            candidate = _normalized(ntpath.join(current, part))
            child = _open(candidate, _READ, _OPEN_EXISTING, directory=True, share=_SHARE_RW)
            try:
                canonical = _validate(child, candidate, directory=True)
            except BaseException:
                with suppress(OSError):
                    _close(child)
                raise
            ancestors.append(handle)
            handle, current = child, canonical
        _validate_acl(handle)
        return _Directory(handle, current, tuple(ancestors))
    except BaseException:
        with suppress(OSError):
            _close_parent(_Directory(handle, current, tuple(ancestors)))
        raise


def _open_child(parent: _Directory, name: str, access: int, creation: int, *, share: int) -> int:
    if _final_path(parent.handle) != parent.expected:
        raise WindowsPathError("Codex config directory changed during access")
    expected = _normalized(ntpath.join(parent.expected, name))
    handle = _open(expected, access, creation, directory=False, share=share)
    try:
        _validate(handle, expected, directory=False)
        _validate_acl(handle)
        if _final_path(parent.handle) != parent.expected:
            raise WindowsPathError("Codex config directory changed during access")
        return handle
    except BaseException:
        _close(handle)
        raise


def _descriptor(handle: int, flags: int) -> int:
    descriptor = -1
    try:
        descriptor = msvcrt.open_osfhandle(handle, flags | getattr(os, "O_BINARY", 0))
        os.set_inheritable(descriptor, False)
        return descriptor
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        else:
            _close(handle)
        raise


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_CONFIG_BYTES:
        chunk = os.read(descriptor, min(65536, MAX_CONFIG_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > MAX_CONFIG_BYTES:
        raise WindowsPathError("Codex config transaction file is too large")
    return b"".join(chunks)


def _read_open_descriptor(descriptor: int) -> tuple[bytes, os.stat_result]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise WindowsPathError("Codex config is not a regular file")
    data = _read_descriptor(descriptor)
    after = os.fstat(descriptor)
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, name) != getattr(after, name) for name in fields):
        raise WindowsPathError("Codex config changed while it was read")
    if len(data) != before.st_size:
        raise WindowsPathError("Codex config read ended before the validated size")
    return data, after


def _read_child(parent: _Directory, name: str) -> tuple[bytes, os.stat_result]:
    descriptor = -1
    try:
        handle = _open_child(parent, name, _READ, _OPEN_EXISTING, share=_SHARE_ALL)
        descriptor = _descriptor(handle, os.O_RDONLY)
        return _read_open_descriptor(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_regular(path: Path) -> tuple[bytes, os.stat_result]:
    parent = _open_parent(path)
    try:
        return _read_child(parent, path.name)
    finally:
        _close_parent(parent)


def open_lock(path: Path) -> int:
    parent = _open_parent(path)
    try:
        handle = _open_child(parent, path.name, _READ | _WRITE, _OPEN_ALWAYS, share=_SHARE_RW)
        return _descriptor(handle, os.O_RDWR)
    finally:
        _close_parent(parent)


def _same_stat(actual: os.stat_result | None, expected: tuple | None) -> bool:
    if expected is None:
        return actual is None
    if actual is None:
        return False
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    return tuple(getattr(actual, field) for field in fields) == expected[:4]


def _identity(data: bytes, info: os.stat_result) -> tuple:
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    values = tuple(getattr(info, field) for field in fields)
    return (*values, hashlib.sha256(data).hexdigest())


def _snapshot_name(parent: _Directory, name: str) -> tuple | None:
    try:
        data, info = _read_child(parent, name)
    except FileNotFoundError:
        return None
    return _identity(data, info)


def _current_stat(parent: _Directory, name: str) -> os.stat_result | None:
    try:
        handle = _open_child(parent, name, _READ, _OPEN_EXISTING, share=_SHARE_ALL)
    except FileNotFoundError:
        return None
    descriptor = _descriptor(handle, os.O_RDONLY)
    try:
        return os.fstat(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise WindowsPathError("Codex config write made no progress")
        view = view[written:]


def _rename(descriptor: int, parent: _Directory, name: str, replace: bool = False) -> None:
    """Rename relative to the already validated parent directory handle."""
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
    info.root = parent.handle
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


def _mark_delete(descriptor: int) -> None:
    info = _DispositionInfo(True)
    handle = msvcrt.get_osfhandle(descriptor)
    if not _SET_HANDLE_INFO(
        handle, _DISPOSITION_INFO, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _replace_with_backup(parent: _Directory, target: str, replacement: str, backup: str) -> None:
    _require_windows()
    if _final_path(parent.handle) != parent.expected:
        raise WindowsPathError("Codex config directory changed during replacement")
    child = lambda name: ntpath.join(parent.expected, name)
    atomic_commit.windows_replace(child(target), child(replacement), child(backup))


def _move_name(parent: _Directory, source: str, target: str) -> None:
    handle = _open_child(
        parent, source, _READ | _DELETE, _OPEN_EXISTING, share=_SHARE_ALL
    )
    descriptor = _descriptor(handle, os.O_RDONLY)
    try:
        _rename(descriptor, parent, target)
    finally:
        os.close(descriptor)


def _discard_name(parent: _Directory, name: str) -> None:
    try:
        handle = _open_child(
            parent, name, _READ | _DELETE, _OPEN_EXISTING, share=_SHARE_ALL
        )
        descriptor = _descriptor(handle, os.O_RDONLY)
        try:
            _mark_delete(descriptor)
        finally:
            os.close(descriptor)
    except (OSError, WindowsPathError):
        pass


def _recover_failed_replace(parent: _Directory, target: str, backup: str) -> None:
    if _snapshot_name(parent, target) is not None:
        return
    if _snapshot_name(parent, backup) is None:
        return
    with suppress(OSError, WindowsPathError):
        _move_name(parent, backup, target)


def _preserve_name(parent: _Directory, source: str, target: str) -> str:
    recovery = f".{target}.{uuid.uuid4().hex}.recovery"
    try:
        _move_name(parent, source, recovery)
    except (OSError, WindowsPathError):
        return source
    return recovery


def _commit_replacement(
    parent: _Directory, target: str, temporary: str, expected: tuple, transaction: tuple,
) -> None:
    try:
        committed, recovery = atomic_commit.windows_replace_cas(
            target, temporary, expected, transaction,
            lambda *names: _replace_with_backup(parent, *names),
            lambda name: _snapshot_name(parent, name),
            lambda name: _discard_name(parent, name),
            lambda source, name: _preserve_name(parent, source, name),
            lambda name, backup: _recover_failed_replace(parent, name, backup),
        )
    except OSError as exc:
        raise WindowsPathError("Codex config replacement failed safely") from exc
    if committed:
        return
    detail = f"; newer content preserved at {recovery}" if recovery else ""
    raise WindowsPathError(f"Codex config changed at commit{detail}")


def atomic_write(path: Path, data: bytes, expected: tuple | None) -> None:
    if len(data) > MAX_CONFIG_BYTES:
        raise WindowsPathError("Codex config transaction file is too large")
    parent = _open_parent(path)
    temporary = f".deepseek-{uuid.uuid4().hex}.tmp"
    descriptor = -1
    created = False
    try:
        if not _same_stat(_current_stat(parent, path.name), expected):
            raise WindowsPathError("Codex config changed before replacement")
        handle = _open_child(
            parent, temporary, _READ | _WRITE | _DELETE,
            _CREATE_NEW, share=_SHARE_ALL,
        )
        descriptor = _descriptor(handle, os.O_RDWR)
        created = True
        _write_all(descriptor, data)
        os.fsync(descriptor)
        transaction = _identity(data, os.fstat(descriptor))
        if expected is None:
            _rename(descriptor, parent, path.name)
            created = False
        else:
            os.close(descriptor)
            descriptor = -1
            created = False
            _commit_replacement(parent, path.name, temporary, expected, transaction)
    finally:
        with suppress(OSError):
            if descriptor >= 0 and created:
                _mark_delete(descriptor)
        with suppress(OSError):
            if descriptor >= 0:
                os.close(descriptor)
        with suppress(OSError):
            _close_parent(parent)


def delete_regular(path: Path, expected: tuple) -> None:
    parent = _open_parent(path)
    descriptor = -1
    quarantine = f".{path.name}.{uuid.uuid4().hex}.delete"
    try:
        handle = _open_child(
            parent, path.name, _READ | _DELETE, _OPEN_EXISTING, share=_SHARE_ALL
        )
        descriptor = _descriptor(handle, os.O_RDONLY)
        if not _same_stat(os.fstat(descriptor), expected):
            raise WindowsPathError("Codex config changed before deletion")
        _rename(descriptor, parent, quarantine)
        try:
            data, info = _read_open_descriptor(descriptor)
            if _identity(data, info) != expected:
                raise WindowsPathError("Codex config changed at deletion")
            if _current_stat(parent, path.name) is not None:
                raise WindowsPathError("Codex config was recreated during deletion")
        except BaseException as exc:
            location = _restore_delete(descriptor, parent, path.name, quarantine)
            message = str(exc) if isinstance(exc, WindowsPathError) else (
                "Codex config deletion audit failed"
            )
            raise WindowsPathError(
                f"{message}; preserved at {location}"
            ) from exc
        _mark_delete(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _close_parent(parent)


def _restore_delete(descriptor: int, parent: _Directory, target: str, quarantine: str) -> str:
    try:
        _rename(descriptor, parent, target)
        return target
    except OSError:
        recovery = f".{target}.{uuid.uuid4().hex}.recovery"
        try:
            _rename(descriptor, parent, recovery)
            return recovery
        except OSError:
            return quarantine
