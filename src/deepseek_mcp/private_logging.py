"""Small fail-closed primitives for private bounded runtime logs."""
from __future__ import annotations

import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MAX_LOG_BYTES = 10 * 1024 * 1024


def _validate_private_file(
    info: os.stat_result, label: str, *, require_private: bool = True
) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"DeepSeek {label} path is not a regular file")
    if info.st_uid != os.getuid():
        raise OSError(f"DeepSeek {label} path is not owned by the current user")
    if require_private and stat.S_IMODE(info.st_mode) & 0o077:
        raise OSError(f"DeepSeek {label} path is not private")
    if info.st_nlink != 1:
        raise OSError(f"DeepSeek {label} path must not be hard-linked")


def _open_private_file(directory_fd: int, name: str, label: str) -> int:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        _validate_private_file(
            os.fstat(descriptor), label, require_private=False
        )
        os.fchmod(descriptor, 0o600)
        _validate_private_file(os.fstat(descriptor), label)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class PrivateBoundedLogStream:
    """Serialize rotation and bounded appends across POSIX MCP processes."""

    def __init__(self, path: Path, *, rotate_on_full: bool = False) -> None:
        if os.name == "nt":
            raise OSError("secure persistent logging is unavailable on Windows")
        import fcntl

        self._fcntl = fcntl
        self._name = path.name
        self._rotated_name = f"{path.name}.1"
        self._rotate_on_full = rotate_on_full
        self._thread_lock = threading.Lock()
        self._closed = False
        self._directory_fd = self._open_directory(path.parent)
        self._lock_fd = -1
        self._log_fd = -1
        try:
            self._lock_fd = _open_private_file(
                self._directory_fd, f".{path.name}.lock", "log lock"
            )
            with self._transaction():
                self._refresh_log_locked()
        except BaseException:
            self._close_log()
            if self._lock_fd >= 0:
                os.close(self._lock_fd)
            os.close(self._directory_fd)
            raise

    @staticmethod
    def _open_directory(path: Path) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode):
                raise OSError("DeepSeek log path is not a directory")
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise OSError("DeepSeek log directory is not private")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._fcntl.flock(self._lock_fd, self._fcntl.LOCK_EX)
        try:
            yield
        finally:
            self._fcntl.flock(self._lock_fd, self._fcntl.LOCK_UN)

    def _refresh_log_locked(self) -> None:
        entry = self._entry_info()
        if self._log_fd >= 0 and entry is not None:
            current = os.fstat(self._log_fd)
            if (entry.st_dev, entry.st_ino) == (current.st_dev, current.st_ino):
                os.fchmod(self._log_fd, 0o600)
                _validate_private_file(os.fstat(self._log_fd), "server log")
                if current.st_size <= MAX_LOG_BYTES:
                    return
        self._close_log()
        descriptor = _open_private_file(self._directory_fd, self._name, "server log")
        if os.fstat(descriptor).st_size > MAX_LOG_BYTES:
            os.close(descriptor)
            self._log_fd = -1
            self._rotate_locked()
            return
        self._log_fd = descriptor

    def _rotate_locked(self) -> None:
        self._close_log()
        os.replace(
            self._name,
            self._rotated_name,
            src_dir_fd=self._directory_fd,
            dst_dir_fd=self._directory_fd,
        )
        self._log_fd = _open_private_file(
            self._directory_fd, self._name, "server log"
        )

    def _entry_info(self) -> os.stat_result | None:
        try:
            info = os.stat(
                self._name, dir_fd=self._directory_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            return None
        _validate_private_file(info, "server log", require_private=False)
        return info

    def _close_log(self) -> None:
        if self._log_fd >= 0:
            os.close(self._log_fd)
            self._log_fd = -1

    def write(self, value: str) -> int:
        payload = value.encode("utf-8")
        written = False
        with self._thread_lock:
            if self._closed:
                raise ValueError("I/O operation on closed log stream")
            with self._transaction():
                self._refresh_log_locked()
                remaining = MAX_LOG_BYTES - os.fstat(self._log_fd).st_size
                if len(payload) > remaining and self._rotate_on_full:
                    self._rotate_locked()
                    remaining = MAX_LOG_BYTES
                if len(payload) <= remaining:
                    self._write_all(payload)
                    written = True
        return len(value) if written else 0

    def _write_all(self, payload: bytes) -> None:
        offset = 0
        while offset < len(payload):
            written = os.write(self._log_fd, payload[offset:])
            if written <= 0:
                raise OSError("failed to append DeepSeek server log")
            offset += written

    def flush(self) -> None:
        return None

    def close(self) -> None:
        with self._thread_lock:
            if self._closed:
                return
            self._closed = True
            self._close_log()
            os.close(self._lock_fd)
            os.close(self._directory_fd)
