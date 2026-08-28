"""Race-resistant, bounded workspace text-file operations."""
from __future__ import annotations
import hashlib
import ntpath
import os
import signal
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .file_identity import (
    ExpectedIdentity,
    FileIdentity,
    MISSING_FILE,
    MissingFile,
    MutationCommittedWarning,
    ToolInputError,
    WorkspaceFileNotFound,
    validate_target as _validate_target,
    write_baseline as _write_baseline,
)
from .posix_atomic_commit import (
    commit as _commit_posix_file,
    discard as _discard_posix_file,
)
from .safety import resolve_safe_path
from . import windows_atomic_commit
from .transaction_report import mutation_ready, mutation_warning
from .workspace_guard import require_workspace_identity, require_workspace_stat
MAX_TEXT_FILE_BYTES = 5_000_000
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_DIRECTORY_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)

def _temporary_name() -> str:
    value = os.environ.get("DEEPSEEK_TOOL_TRANSACTION_ID", "")
    try:
        token = uuid.UUID(value).hex if value else uuid.uuid4().hex
    except (ValueError, AttributeError):
        token = uuid.uuid4().hex
    return f".deepseek-mcp-{token}.tmp"
def _location(workspace: Path, label: str) -> tuple[Path, Path]:
    require_workspace_identity(workspace)
    root = workspace.resolve()
    absolute = resolve_safe_path(label, root)
    relative = absolute.relative_to(root)
    if not relative.parts:
        raise ToolInputError("write target is not a regular file")
    if os.name == "nt":
        _validate_windows_parts(relative)
    return root, relative
def _validate_windows_parts(relative: Path) -> None:
    for part in relative.parts:
        if ":" in part or part.rstrip(" .") != part:
            raise ToolInputError("Windows path contains an unsafe component")
def _directory_flags() -> int:
    return (
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
def _open_posix_parent(root: Path, relative: Path, *, create: bool) -> int:
    descriptor = os.open(root, _directory_flags())
    try:
        require_workspace_stat(os.fstat(descriptor))
        for part in relative.parent.parts:
            child = _open_posix_directory(descriptor, part, create=create)
            os.close(descriptor)
            descriptor = child
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ToolInputError("workspace parent is not a real directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
def _open_posix_directory(parent: int, name: str, *, create: bool) -> int:
    try:
        return os.open(name, _directory_flags(), dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise WorkspaceFileNotFound("file not found") from None
    try:
        os.mkdir(name, 0o755, dir_fd=parent)
        os.fsync(parent)
    except FileExistsError:
        pass
    return os.open(name, _directory_flags(), dir_fd=parent)
def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_TEXT_FILE_BYTES:
        amount = min(1024 * 1024, MAX_TEXT_FILE_BYTES + 1 - total)
        chunk = os.read(descriptor, amount)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    data = b"".join(chunks)
    if len(data) > MAX_TEXT_FILE_BYTES:
        raise ToolInputError(f"file exceeds {MAX_TEXT_FILE_BYTES} bytes")
    return data
def _decode(data: bytes, *, strict_utf8: bool, reject_binary: bool) -> str:
    if reject_binary and b"\x00" in data[:8192]:
        raise ToolInputError("file appears to be binary")
    try:
        return data.decode("utf-8", errors="strict" if strict_utf8 else "replace")
    except UnicodeDecodeError:
        raise ToolInputError("file is not valid UTF-8") from None

def _checked_read(descriptor: int, *, strict_utf8: bool, reject_binary: bool) -> tuple[str, FileIdentity]:
    before = FileIdentity.from_stat(os.fstat(descriptor))
    if not stat.S_ISREG(before.mode):
        raise ToolInputError("file is not a regular file")
    data = _read_descriptor(descriptor)
    if len(data) != before.size or FileIdentity.from_stat(os.fstat(descriptor)) != before:
        raise ToolInputError("file changed while reading")
    identity = FileIdentity.from_stat(os.fstat(descriptor), hashlib.sha256(data).digest())
    return _decode(data, strict_utf8=strict_utf8, reject_binary=reject_binary), identity

def _read_posix(
    root: Path, relative: Path, *, strict_utf8: bool, reject_binary: bool
) -> tuple[str, FileIdentity]:
    parent = _open_posix_parent(root, relative, create=False)
    descriptor = -1
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(relative.name, flags, dir_fd=parent)
        except FileNotFoundError:
            raise WorkspaceFileNotFound("file not found") from None
        return _checked_read(
            descriptor, strict_utf8=strict_utf8, reject_binary=reject_binary
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)

def read_workspace_text(
    workspace: Path,
    label: str,
    *,
    strict_utf8: bool = False,
    reject_binary: bool = False,
) -> tuple[str, FileIdentity]:
    root, relative = _location(workspace, label)
    if os.name == "nt":
        return _read_windows(
            root, relative, strict_utf8=strict_utf8, reject_binary=reject_binary
        )
    return _read_posix(
        root, relative, strict_utf8=strict_utf8, reject_binary=reject_binary
    )

def _current_posix_info(parent: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None

def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("workspace write made no progress")
        view = view[written:]

@dataclass
class _PosixCommitState:
    committed: bool = False
    warnings: list[str] = field(default_factory=list)


@contextmanager
def _defer_posix_termination():
    mask = getattr(signal, "pthread_sigmask", None)
    if mask is None:
        yield
        return
    previous = mask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        yield
    finally:
        mask(signal.SIG_SETMASK, previous)


def _finish_posix_commit(
    parent: int, temporary: str, target: str, baseline,
    replacement: FileIdentity, state: _PosixCommitState,
) -> None:
    with _defer_posix_termination():
        try:
            _commit_posix_file(
                parent, temporary, target, baseline, replacement
            )
        except MutationCommittedWarning as warning:
            detail = str(warning)
            state.warnings.append(detail)
            mutation_warning(detail)
        state.committed = True
        try:
            os.fsync(parent)
        except OSError as error:
            detail = f"directory durability not confirmed: {error}"
            state.warnings.append(detail)
            mutation_warning(detail)


def _discard_owned_posix_temp(
    parent: int, temporary: str, replacement: FileIdentity | None,
) -> None:
    if replacement is None:
        _unlink_posix_temp(parent, temporary)
        return
    try:
        _discard_posix_file(parent, temporary, replacement)
    except OSError:
        pass


def _write_posix(
    root: Path, relative: Path, data: bytes, expected: ExpectedIdentity
) -> None:
    parent = _open_posix_parent(root, relative, create=True)
    temporary = _temporary_name()
    descriptor = -1
    created = False
    replacement: FileIdentity | None = None
    state = _PosixCommitState()
    try:
        mode, baseline = _write_baseline(
            _current_posix_info(parent, relative.name), expected
        )
        descriptor = _create_posix_temp(parent, temporary)
        created = True
        _write_all(descriptor, data)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        replacement = FileIdentity.from_stat(
            os.fstat(descriptor), hashlib.sha256(data).digest()
        )
        os.close(descriptor)
        descriptor = -1
        assert replacement.digest is not None
        mutation_ready(replacement.digest)
        _validate_target(_current_posix_info(parent, relative.name), baseline)
        _finish_posix_commit(
            parent, temporary, relative.name, baseline, replacement, state
        )
        created = False
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if created and not state.committed:
            _discard_owned_posix_temp(parent, temporary, replacement)
        try:
            os.close(parent)
        except OSError as error:
            if not state.committed:
                raise
            detail = f"directory handle close not confirmed: {error}"
            state.warnings.append(detail)
            mutation_warning(detail)
    if state.warnings:
        raise MutationCommittedWarning("; ".join(state.warnings))

def _create_posix_temp(parent: int, name: str) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(name, flags, 0o600, dir_fd=parent)

def _unlink_posix_temp(parent: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass

def _encode_text(content: str) -> bytes:
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        raise ToolInputError("content is not valid UTF-8") from None
    if len(data) > MAX_TEXT_FILE_BYTES:
        raise ToolInputError(f"content exceeds {MAX_TEXT_FILE_BYTES} bytes")
    return data

def atomic_write_workspace_text(
    workspace: Path, label: str, content: str, *, expected: ExpectedIdentity = None
) -> None:
    root, relative = _location(workspace, label)
    data = _encode_text(content)
    if os.name == "nt":
        _write_windows(root, relative, data, expected)
        return
    _write_posix(root, relative, data, expected)

if os.name == "nt":  # pragma: no cover - exercised by the Windows CI matrix
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CREATE_FILE = _KERNEL32.CreateFileW
    _CREATE_FILE.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    _CREATE_FILE.restype = wintypes.HANDLE
    _CLOSE_HANDLE = _KERNEL32.CloseHandle
    _CLOSE_HANDLE.argtypes = (wintypes.HANDLE,)
    _CLOSE_HANDLE.restype = wintypes.BOOL
    _FINAL_PATH = _KERNEL32.GetFinalPathNameByHandleW
    _FINAL_PATH.argtypes = (wintypes.HANDLE, wintypes.LPWSTR,
                            wintypes.DWORD, wintypes.DWORD)
    _FINAL_PATH.restype = wintypes.DWORD
    _GET_HANDLE_INFO = _KERNEL32.GetFileInformationByHandleEx
    _GET_HANDLE_INFO.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _GET_HANDLE_INFO.restype = wintypes.BOOL
    class _WinAttributeTagInfo(ctypes.Structure):
        _fields_ = (("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD))

_WIN_READ, _WIN_WRITE, _WIN_DELETE = 0x80000000, 0x40000000, 0x00010000
_WIN_SHARE_READ, _WIN_SHARE_WRITE, _WIN_SHARE_ALL = 1, 2, 7
_WIN_OPEN_EXISTING, _WIN_CREATE_NEW = 3, 1
_WIN_OPEN_REPARSE, _WIN_BACKUP_SEMANTICS = 0x00200000, 0x02000000
_WIN_FILE_ATTRIBUTE_TAG_INFO = 9

def _win_open_path(
    path: str, access: int, creation: int, *, directory: bool,
    sharing: int | None = None,
) -> int:
    flags = _WIN_OPEN_REPARSE | (_WIN_BACKUP_SEMANTICS if directory else 0)
    if sharing is None:
        sharing = _WIN_SHARE_READ | _WIN_SHARE_WRITE if directory else _WIN_SHARE_ALL
    handle = _CREATE_FILE(
        path, access, sharing, None, creation, flags, None
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)

def _win_close(handle: int) -> None:
    if not _CLOSE_HANDLE(handle):
        raise ctypes.WinError(ctypes.get_last_error())

def _win_final_path(handle: int) -> str:
    size = _FINAL_PATH(handle, None, 0, 0)
    if not size:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(size + 1)
    written = _FINAL_PATH(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    path = buffer.value
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return ntpath.normcase(ntpath.normpath(path))

def _win_attributes(handle: int) -> int:
    info = _WinAttributeTagInfo()
    ok = _GET_HANDLE_INFO(
        handle, _WIN_FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info)
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(info.attributes)

def _win_validate_handle(handle: int, *, directory: bool) -> None:
    attributes = _win_attributes(handle)
    if attributes & _REPARSE_ATTRIBUTE:
        raise ToolInputError("workspace path is a reparse point")
    is_directory = bool(attributes & _DIRECTORY_ATTRIBUTE)
    if is_directory != directory:
        kind = "directory" if directory else "regular file"
        raise ToolInputError(f"workspace path is not a {kind}")

def _win_open_root(root: Path) -> int:
    handle = _win_open_path(
        str(root), _WIN_READ, _WIN_OPEN_EXISTING, directory=True
    )
    try:
        _win_validate_handle(handle, directory=True)
        require_workspace_identity(Path(_win_final_path(handle)))
        return handle
    except BaseException:
        _win_close(handle)
        raise

def _win_open_child(
    parent: int, name: str, access: int, creation: int, *, directory: bool,
    sharing: int | None = None,
) -> int:
    before = _win_final_path(parent)
    handle = _win_open_path(
        ntpath.join(before, name), access, creation,
        directory=directory, sharing=sharing,
    )
    try:
        _win_validate_handle(handle, directory=directory)
        after = _win_final_path(parent)
        actual = _win_final_path(handle)
        expected = ntpath.normcase(ntpath.normpath(ntpath.join(after, name)))
        if before != after or actual != expected:
            raise ToolInputError("workspace path identity changed while opening")
        return handle
    except BaseException:
        _win_close(handle)
        raise

def _win_open_directory(parent: int, name: str, *, create: bool) -> int:
    try:
        return _win_open_child(
            parent, name, _WIN_READ, _WIN_OPEN_EXISTING, directory=True
        )
    except FileNotFoundError:
        if not create:
            raise WorkspaceFileNotFound("file not found") from None
    try:
        os.mkdir(ntpath.join(_win_final_path(parent), name))
    except FileExistsError:
        pass
    return _win_open_child(
        parent, name, _WIN_READ, _WIN_OPEN_EXISTING, directory=True
    )

def _win_open_parent(root: Path, relative: Path, *, create: bool) -> int:
    handle = _win_open_root(root)
    try:
        for part in relative.parent.parts:
            child = _win_open_directory(handle, part, create=create)
            _win_close(handle)
            handle = child
        return handle
    except BaseException:
        _win_close(handle)
        raise

def _win_fd(handle: int, flags: int) -> int:
    try:
        descriptor = msvcrt.open_osfhandle(handle, flags | getattr(os, "O_BINARY", 0))
    except BaseException:
        _win_close(handle)
        raise
    try:
        os.set_inheritable(descriptor, False)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise

def _read_windows(
    root: Path, relative: Path, *, strict_utf8: bool, reject_binary: bool
) -> tuple[str, FileIdentity]:
    parent = _win_open_parent(root, relative, create=False)
    descriptor = -1
    try:
        try:
            handle = _win_open_child(
                parent, relative.name, _WIN_READ, _WIN_OPEN_EXISTING, directory=False
            )
        except FileNotFoundError:
            raise WorkspaceFileNotFound("file not found") from None
        descriptor = _win_fd(handle, os.O_RDONLY)
        return _checked_read(
            descriptor, strict_utf8=strict_utf8, reject_binary=reject_binary
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _win_close(parent)

def _win_current_info(parent: int, name: str) -> os.stat_result | None:
    try:
        handle = _win_open_child(
            parent, name, _WIN_READ, _WIN_OPEN_EXISTING, directory=False
        )
    except FileNotFoundError:
        return None
    except PermissionError as error:
        try:
            directory = _win_open_child(
                parent, name, _WIN_READ, _WIN_OPEN_EXISTING, directory=True
            )
        except OSError:
            raise error
        _win_close(directory)
        raise ToolInputError("write target is not a regular file") from None
    descriptor = _win_fd(handle, os.O_RDONLY)
    try:
        return os.fstat(descriptor)
    finally:
        os.close(descriptor)

_win_rename = windows_atomic_commit.rename
_win_mark_delete = windows_atomic_commit.mark_delete

from .windows_workspace_write import write_windows as _write_windows
