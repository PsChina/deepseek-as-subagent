"""Cross-process execution lease scoped to one workspace directory."""
from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from . import windows_file_io

if os.name == "nt":
    import msvcrt
else:
    import fcntl

DEFAULT_LOCK_DIRECTORY = Path.home() / ".deepseek-mcp" / "locks"


class WorkspaceLockError(RuntimeError):
    """The workspace execution lease could not be created or operated."""


class WorkspaceLockBusy(WorkspaceLockError):
    """Another process already holds the workspace execution lease."""


@dataclass
class WorkspaceExecutionLease:
    """A native file lock whose open descriptor owns the execution lease."""

    path: Path
    _fd: int | None

    def fileno(self) -> int:
        """Return the active descriptor for a supervised child process."""
        if self._fd is None:
            raise WorkspaceLockError("workspace execution lease is already released")
        return self._fd

    def release(self) -> None:
        """Release the lease once; closing the descriptor is the final safeguard."""
        fd = self._fd
        if fd is None:
            return
        self._fd = None

        unlock_error: OSError | None = None
        if os.name == "nt":
            try:
                _unlock_fd(fd)
            except OSError as error:
                unlock_error = error
        try:
            os.close(fd)
        except OSError as error:
            if unlock_error is None:
                unlock_error = error

        if unlock_error is not None:
            raise WorkspaceLockError(
                f"failed to release workspace execution lease: {unlock_error}"
            ) from unlock_error


def acquire_workspace_lease(
    workspace: Path,
    lock_directory: Path | None = None,
    *,
    expected_identity: bytes | None = None,
) -> WorkspaceExecutionLease:
    """Acquire a non-blocking native lease for one canonical workspace."""
    directory = lock_directory or DEFAULT_LOCK_DIRECTORY
    identity = workspace_identity(workspace)
    if expected_identity is not None and identity != expected_identity:
        raise WorkspaceLockError("workspace identity changed before lease acquisition")
    path = _lock_path(identity, directory)
    fd = _open_lock_file(path)

    try:
        _lock_fd(fd)
    except OSError as error:
        os.close(fd)
        if _is_lock_conflict(error):
            raise WorkspaceLockBusy(
                f"workspace is already owned by another DeepSeek execution: "
                f"{workspace.resolve()}"
            ) from error
        raise WorkspaceLockError(
            f"failed to acquire workspace execution lease: {error}"
        ) from error

    lease = WorkspaceExecutionLease(path=path, _fd=fd)
    try:
        if workspace_identity(workspace) != identity:
            raise WorkspaceLockError(
                "workspace identity changed during lease acquisition"
            )
    except BaseException:
        lease.release()
        raise
    return lease


def _lock_path(identity: bytes, lock_directory: Path) -> Path:
    digest = hashlib.sha256(identity).hexdigest()
    directory = Path(os.path.abspath(lock_directory))
    try:
        directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _secure_directory(directory.parent)
        directory.mkdir(mode=0o700, exist_ok=True)
        _secure_directory(directory)
    except OSError as error:
        raise WorkspaceLockError(
            f"failed to prepare workspace lock directory: {error}"
        ) from error
    return directory / f"{digest}.lock"


def _secure_directory(path: Path) -> None:
    if os.name == "nt":
        try:
            windows_file_io.validate_private_path(path, directory=True)
        except windows_file_io.WindowsPathError as error:
            raise OSError("workspace lock directory is not private") from error
        return
    flags = os.O_RDONLY | os.O_DIRECTORY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("workspace lock directory has unsafe ownership")
        os.fchmod(descriptor, 0o700)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
            raise OSError("workspace lock directory mode is not 0700")
    finally:
        os.close(descriptor)


def filesystem_identity(metadata) -> bytes | None:
    inode = int(getattr(metadata, "st_ino", 0) or 0)
    if not inode:
        return None
    device = int(getattr(metadata, "st_dev", 0) or 0)
    return f"filesystem:{device}:{inode}".encode("ascii")


def _workspace_identity(workspace: Path) -> bytes:
    """Return an alias-independent identity for an existing workspace.

    POSIX directory device/inode pairs identify the filesystem object rather
    than the spelling used to reach it.  This is important on default macOS
    filesystems, where ``/Users`` and ``/users`` can resolve to the same inode
    even though ``normcase`` preserves their different spellings.

    Modern Python exposes the underlying file ID as ``st_ino`` on Windows too.
    Some filesystems may still report zero; only there do we fall back to a
    normalized final path.
    """
    try:
        metadata = workspace.stat()
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceLockError(
            f"failed to identify workspace for execution lease: {error}"
        ) from error
    identity = filesystem_identity(metadata)
    if identity is not None:
        return identity

    canonical = os.path.normcase(os.path.realpath(os.fspath(workspace)))
    return b"windows-path:" + os.fsencode(canonical)


def workspace_identity(workspace: Path) -> bytes:
    """Public shared identity for workspace-scoped execution ownership."""
    return _workspace_identity(workspace)


def _open_lock_file(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    directory_fd: int | None = None
    try:
        fd, directory_fd, before = _open_platform_lock(path, flags)
        os.set_inheritable(fd, False)
        info = os.fstat(fd)
        _validate_open_lock(path, fd, info, before)
        if info.st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except (OSError, windows_file_io.WindowsPathError) as error:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if (
            os.name == "nt"
            and isinstance(error, OSError)
            and _is_lock_conflict(error)
        ):
            raise WorkspaceLockBusy(
                "workspace execution lock is already held"
            ) from error
        raise WorkspaceLockError(
            f"failed to open workspace execution lock: {error}"
        ) from error
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _open_platform_lock(
    path: Path, flags: int
) -> tuple[int, int | None, os.stat_result | None]:
    if os.name == "nt":
        before = _optional_real_path_info(path)
        return windows_file_io.open_exclusive_regular(path), None, before
    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(path.parent, directory_flags)
    try:
        descriptor = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
    except BaseException:
        os.close(directory_fd)
        raise
    return descriptor, directory_fd, None


def _validate_open_lock(
    path: Path,
    descriptor: int,
    info: os.stat_result,
    before: os.stat_result | None,
) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise OSError("workspace execution lock is not a regular file")
    if os.name == "nt":
        _validate_windows_lock(path, descriptor, info, before)
        return
    if info.st_uid != os.getuid():
        raise OSError("workspace execution lock has unsafe ownership")
    os.fchmod(descriptor, 0o600)
    if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
        raise OSError("workspace execution lock mode is not 0600")


def _validate_windows_lock(
    path: Path,
    descriptor: int,
    info: os.stat_result,
    before: os.stat_result | None,
) -> None:
    try:
        windows_file_io.validate_private_descriptor(descriptor, path)
    except windows_file_io.WindowsPathError as error:
        raise OSError("workspace execution lock is not private") from error
    if _stat_is_reparse(info):
        raise OSError("workspace execution lock is a reparse point")
    if before is not None and not _same_identity(before, info):
        raise OSError("workspace execution lock changed while opening")


def _stat_is_reparse(info: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    return bool(attributes & reparse_flag)


def _is_reparse(path: Path, info: os.stat_result) -> bool:
    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    return stat.S_ISLNK(info.st_mode) or is_junction(path) or _stat_is_reparse(info)


def _optional_real_path_info(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if _is_reparse(path, info):
        raise OSError("workspace execution lock is a reparse point")
    return info


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    if first.st_ino and second.st_ino:
        return (first.st_dev, first.st_ino, first.st_mode) == (
            second.st_dev,
            second.st_ino,
            second.st_mode,
        )
    fields = ("st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    return all(getattr(first, field) == getattr(second, field) for field in fields)


def _lock_fd(fd: int) -> None:
    if os.name == "nt":
        return
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        return
    fcntl.flock(fd, fcntl.LOCK_UN)


def _is_lock_conflict(error: OSError) -> bool:
    if os.name == "nt":
        return error.errno == errno.EACCES or getattr(error, "winerror", None) in {
            5, 32, 33,
        }
    return error.errno in {errno.EACCES, errno.EAGAIN}
