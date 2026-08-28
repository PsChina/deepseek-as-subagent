#!/usr/bin/env python3
"""Transactional Codex MCP configuration for the DeepSeek adapter."""

from __future__ import annotations

from contextlib import suppress
import hashlib
import json
import os
import stat
import sys
import uuid
from pathlib import Path
from collections.abc import Callable
from typing import NamedTuple

import tomlkit

if __package__:
    from . import atomic_commit, windows_file_io
    from .configure_policy import (
        ConfigTransactionError, EXPOSED_TOOLS, FORWARDED_ENV_VARS,
        MANAGED_MARKER, OwnershipError, configure_install, configure_uninstall,
        validate_registration_absent, validate_registration_payload,
    )
else:  # Executed directly by the installer.
    import atomic_commit
    import windows_file_io
    from configure_policy import (
        ConfigTransactionError, EXPOSED_TOOLS, FORWARDED_ENV_VARS,
        MANAGED_MARKER, OwnershipError, configure_install, configure_uninstall,
        validate_registration_absent, validate_registration_payload,
    )


class TransactionConflict(ConfigTransactionError):
    """The config changed after this transaction and cannot be safely restored."""


class _FileSnapshot(NamedTuple):
    exists: bool
    data: bytes = b""
    mode: int = 0o600
    device: int = 0
    inode: int = 0
    size: int = 0
    mtime_ns: int = 0


_ExpectedSnapshot = _FileSnapshot | None
MAX_TRANSACTION_BYTES = 1024 * 1024
_MANIFEST_FIELDS = (
    "version", "config_path", "backup_path", "original_exists", "original_mode",
    "original_sha256", "installed_sha256",
)
_MANIFEST_TYPES = (int, str, str, bool, int, str, str)


class _ConfigLease:
    """Advisory cross-process lease for cooperating config writers."""

    def __init__(self, config_path: Path):
        self.path = config_path.parent / f".{config_path.name}.deepseek.lock"
        self.descriptor = -1

    def __enter__(self):
        if os.name == "nt":
            try:
                self.descriptor = windows_file_io.open_lock(self.path)
            except (OSError, windows_file_io.WindowsPathError) as exc:
                raise ConfigTransactionError("Codex config lock is unsafe") from exc
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_RDWR | os.O_CREAT
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            self.descriptor = os.open(self.path, flags, 0o600)
        info = os.fstat(self.descriptor)
        unsafe_links = os.name == "nt" and info.st_nlink != 1
        if not stat.S_ISREG(info.st_mode) or unsafe_links:
            os.close(self.descriptor)
            self.descriptor = -1
            raise ConfigTransactionError("Codex config lock is not a private regular file")
        if os.name == "nt":
            import msvcrt

            if info.st_size == 0:
                os.write(self.descriptor, b"\0")
            os.lseek(self.descriptor, 0, os.SEEK_SET)
            msvcrt.locking(self.descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            os.fchmod(self.descriptor, 0o600)
            fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self.descriptor, 0, os.SEEK_SET)
                msvcrt.locking(self.descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)
            self.descriptor = -1
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
def _read_snapshot(path: Path) -> _FileSnapshot:
    if os.name == "nt":
        return _read_windows_snapshot(path)
    directory = _open_posix_directory(path.parent)
    try:
        return _read_posix_snapshot(directory, path.name)
    finally:
        os.close(directory)
def _open_posix_directory(path: Path, *, create: bool = False) -> int:
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ConfigTransactionError("Codex config parent is not a directory")
    return descriptor
def _read_posix_snapshot(directory: int, name: str) -> _FileSnapshot:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory)
    except FileNotFoundError:
        return _FileSnapshot(False)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ConfigTransactionError("Codex config is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(MAX_TRANSACTION_BYTES + 1)
        if len(data) > MAX_TRANSACTION_BYTES:
            raise ConfigTransactionError("Codex config transaction file is too large")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, key) != getattr(after, key) for key in identity):
        raise TransactionConflict("Codex config changed while it was being read")
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise TransactionConflict("Codex config was replaced while it was read") from exc
    if any(getattr(after, key) != getattr(current, key) for key in identity):
        raise TransactionConflict("Codex config was replaced while it was read")
    return _FileSnapshot(
        True,
        data,
        stat.S_IMODE(after.st_mode),
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
def _read_windows_snapshot(path: Path) -> _FileSnapshot:
    try:
        data, info = windows_file_io.read_regular(path)
    except FileNotFoundError:
        return _FileSnapshot(False)
    except (OSError, windows_file_io.WindowsPathError) as exc:
        raise ConfigTransactionError("Codex config path is unsafe") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ConfigTransactionError("Codex config is not a regular file")
    return _FileSnapshot(
        True, data, stat.S_IMODE(info.st_mode), info.st_dev, info.st_ino,
        info.st_size, info.st_mtime_ns,
    )
def _snapshot_matches(path: Path, expected: _FileSnapshot) -> bool:
    return _same_snapshot(_read_snapshot(path), expected)
def _same_snapshot(current: _FileSnapshot, expected: _FileSnapshot) -> bool:
    return current == expected and _sha256(current.data) == _sha256(expected.data)
def _needs_install(snapshot: _FileSnapshot, installed: bytes) -> bool:
    return installed != snapshot.data or (snapshot.exists and snapshot.mode != 0o600)
def _load_document(raw: bytes):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigTransactionError("Codex config is not valid UTF-8") from exc
    try:
        return tomlkit.parse(text) if text else tomlkit.document()
    except Exception as exc:
        raise ConfigTransactionError(
            "Codex config is invalid TOML; correct it before retrying"
        ) from exc
def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
def _fsync_commit(directory: int, action: str) -> None:
    try:
        os.fsync(directory)
    except OSError as exc:
        raise ConfigTransactionError(
            f"Codex config {action} committed but directory durability sync failed"
        ) from exc
def _snapshot_identity(snapshot: _FileSnapshot) -> tuple | None:
    if not snapshot.exists:
        return None
    return snapshot.device, snapshot.inode, snapshot.size, snapshot.mtime_ns, _sha256(snapshot.data)
def _atomic_write(path: Path, data: bytes, mode: int, expected: _ExpectedSnapshot = None) -> None:
    if os.name == "nt":
        current = expected if expected is not None else _read_snapshot(path)
        try:
            windows_file_io.atomic_write(path, data, _snapshot_identity(current))
        except (OSError, windows_file_io.WindowsPathError) as exc:
            raise ConfigTransactionError("Codex config replacement failed safely") from exc
        return
    _atomic_write_posix(path, data, mode, expected)
def _atomic_write_posix(
    path: Path, data: bytes, mode: int, expected: _FileSnapshot | None
) -> None:
    if len(data) > MAX_TRANSACTION_BYTES:
        raise ConfigTransactionError("Codex config transaction file is too large")
    directory = _open_posix_directory(path.parent, create=True)
    temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    created = False
    try:
        current = _read_posix_snapshot(directory, path.name)
        baseline = current if expected is None else expected
        if not _same_snapshot(current, baseline):
            raise TransactionConflict("Codex config changed before replacement")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, mode, dir_fd=directory)
        created = True
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = -1
        transaction = _read_posix_snapshot(directory, temporary)
        created = False
        if baseline.exists:
            _commit_existing(directory, path.name, temporary, baseline, transaction)
        else:
            _commit_new(directory, path.name, temporary)
        _fsync_commit(directory, "replacement")
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if created:
            atomic_commit.discard_best_effort(directory, temporary)
        os.close(directory)
def _commit_new(directory: int, target: str, temporary: str) -> None:
    try:
        os.link(
            temporary, target, src_dir_fd=directory, dst_dir_fd=directory,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        atomic_commit.discard_best_effort(directory, temporary)
        raise TransactionConflict("Codex config was created before commit") from exc
    try:
        atomic_commit.discard(directory, temporary)
    except OSError as exc:
        raise ConfigTransactionError(
            "Codex config creation committed but private link cleanup failed"
        ) from exc
def _commit_existing(
    directory: int, target: str, temporary: str,
    baseline: _FileSnapshot, transaction: _FileSnapshot,
) -> None:
    try:
        atomic_commit.exchange(directory, temporary, target)
    except atomic_commit.UnsupportedAtomicCommit as exc:
        atomic_commit.discard_best_effort(directory, temporary)
        raise ConfigTransactionError("filesystem lacks safe atomic replacement") from exc
    displaced = _read_posix_snapshot(directory, temporary)
    if _same_snapshot(displaced, baseline):
        os.unlink(temporary, dir_fd=directory)
        return
    recovery = _recover_exchange(directory, target, temporary, transaction)
    detail = f"; newer content preserved at {recovery}" if recovery else ""
    raise TransactionConflict(f"Codex config changed at commit{detail}")
def _recover_exchange(
    directory: int, target: str, temporary: str, transaction: _FileSnapshot
) -> str | None:
    if not _same_snapshot(_read_posix_snapshot(directory, target), transaction):
        return _preserve_recovery(directory, target, temporary)
    atomic_commit.exchange(directory, temporary, target)
    if _same_snapshot(_read_posix_snapshot(directory, temporary), transaction):
        os.unlink(temporary, dir_fd=directory)
        return None
    return _preserve_recovery(directory, target, temporary)
def _preserve_recovery(directory: int, target: str, source: str) -> str:
    recovery = f".{target}.{uuid.uuid4().hex}.recovery"
    try:
        atomic_commit.move_no_clobber(directory, source, recovery)
    except OSError as exc:
        raise ConfigTransactionError(
            f"concurrent config preserved at private temporary {source}"
        ) from exc
    return recovery
def _delete_posix(path: Path, expected: _FileSnapshot) -> None:
    directory = _open_posix_directory(path.parent)
    quarantine = f".{path.name}.{uuid.uuid4().hex}.delete"
    try:
        try:
            atomic_commit.move_no_clobber(directory, path.name, quarantine)
        except atomic_commit.UnsupportedAtomicCommit as exc:
            raise ConfigTransactionError("filesystem lacks safe atomic deletion") from exc
        try:
            displaced = _read_posix_snapshot(directory, quarantine)
            matches = _same_snapshot(displaced, expected)
        except BaseException as exc:
            recovery = _restore_quarantine(directory, path.name, quarantine)
            if recovery:
                raise ConfigTransactionError(
                    f"Codex config deletion audit failed; preserved at {recovery}"
                ) from exc
            raise
        if not matches:
            recovery = _restore_quarantine(directory, path.name, quarantine)
            detail = f"; newer content preserved at {recovery}" if recovery else ""
            raise TransactionConflict(f"Codex config changed at deletion{detail}")
        os.unlink(quarantine, dir_fd=directory)
        _fsync_commit(directory, "deletion")
    finally:
        os.close(directory)
def _restore_quarantine(directory: int, target: str, quarantine: str) -> str | None:
    try:
        atomic_commit.move_no_clobber(directory, quarantine, target)
        return None
    except FileExistsError:
        return _preserve_recovery(directory, target, quarantine)
    except OSError as exc:
        raise ConfigTransactionError(
            f"Codex config preserved at private quarantine {quarantine}"
        ) from exc


def begin_transaction(
    config_path: Path,
    backup_path: Path,
    manifest_path: Path,
    mutate: Callable[[object], list[str] | None],
) -> dict[str, object]:
    with _ConfigLease(config_path):
        return _begin_locked(config_path, backup_path, manifest_path, mutate)
def _begin_locked(
    config_path: Path,
    backup_path: Path,
    manifest_path: Path,
    mutate: Callable[[object], list[str] | None],
) -> dict[str, object]:
    original_snapshot = _read_snapshot(config_path)
    original_exists = original_snapshot.exists
    original = original_snapshot.data
    original_mode = original_snapshot.mode
    document = _load_document(original)
    warnings = mutate(document) or []
    installed = tomlkit.dumps(document).encode("utf-8")
    if not _snapshot_matches(config_path, original_snapshot):
        raise TransactionConflict("Codex config changed while preparing the update")
    manifest = {
        "version": 1,
        "config_path": str(config_path),
        "backup_path": str(backup_path),
        "original_exists": original_exists,
        "original_mode": original_mode,
        "original_sha256": _sha256(original),
        "installed_sha256": _sha256(installed),
    }

    if original_exists:
        _atomic_write(backup_path, original, 0o600)
    _atomic_write(
        manifest_path,
        json.dumps(manifest, sort_keys=True).encode("utf-8"),
        0o600,
    )
    changed = _needs_install(original_snapshot, installed)
    if changed:
        if not _snapshot_matches(config_path, original_snapshot):
            raise TransactionConflict(
                "Codex config changed before the atomic replacement; backup preserved"
            )
        try:
            _atomic_write(config_path, installed, 0o600, original_snapshot)
        except BaseException:
            current = _read_snapshot(config_path)
            if current.exists and _sha256(current.data) == _sha256(installed):
                _restore_original(manifest, backup_path, force=True)
            raise
    return {"changed": changed, "warnings": warnings, **manifest}


def _restore_original(manifest: dict[str, object], backup_path: Path, force: bool) -> None:
    config_path = Path(str(manifest["config_path"]))
    installed_hash = str(manifest["installed_sha256"])
    current = _read_snapshot(config_path)
    if not force and _sha256(current.data) != installed_hash:
        raise TransactionConflict(
            "Codex config changed after installation; refusing to overwrite newer edits. "
            f"The protected backup remains at {backup_path}"
        )
    if not _snapshot_matches(config_path, current):
        raise TransactionConflict("Codex config changed immediately before rollback")
    if bool(manifest["original_exists"]):
        backup = _read_snapshot(backup_path)
        if not backup.exists:
            raise TransactionConflict("Codex config backup is missing")
        original = backup.data
        if _sha256(original) != str(manifest["original_sha256"]):
            raise TransactionConflict("Codex config backup checksum does not match")
        _atomic_write(
            config_path, original, int(manifest["original_mode"]), current
        )
    elif current.exists:
        if os.name == "nt":
            try:
                windows_file_io.delete_regular(
                    config_path, _snapshot_identity(current)
                )
            except (OSError, windows_file_io.WindowsPathError) as exc:
                raise ConfigTransactionError("Codex config deletion failed safely") from exc
        else:
            _delete_posix(config_path, current)
        if os.name == "nt":
            _fsync_directory(config_path.parent)


def _validate_manifest(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(_MANIFEST_FIELDS):
        raise ValueError("manifest fields are missing or unexpected")
    actual = tuple(type(value[name]) for name in _MANIFEST_FIELDS)
    if actual != _MANIFEST_TYPES:
        raise ValueError("manifest fields have invalid types")
    digests = (value["original_sha256"], value["installed_sha256"])
    invalid_digest = any(
        len(item) != 64 or item.strip("0123456789abcdef") for item in digests
    )
    if value["version"] != 1 or not value["config_path"] or not value["backup_path"]:
        raise ValueError("manifest metadata is invalid")
    if not 0 <= value["original_mode"] <= 0o7777 or invalid_digest:
        raise ValueError("manifest integrity metadata is invalid")
    return value


def rollback_transaction(manifest_path: Path, force: bool = False) -> None:
    try:
        snapshot = _read_snapshot(manifest_path)
        if not snapshot.exists:
            raise ConfigTransactionError("transaction manifest is missing")
        manifest = _validate_manifest(json.loads(snapshot.data.decode("utf-8")))
    except Exception as exc:
        raise ConfigTransactionError(f"invalid transaction manifest: {exc}") from exc
    backup_path = Path(str(manifest["backup_path"]))
    config_path = Path(str(manifest["config_path"]))
    with _ConfigLease(config_path):
        _restore_original(manifest, backup_path, force)


if __name__ == "__main__":
    from configure_cli import run

    raise SystemExit(run(sys.modules[__name__]))
