"""Durable, private intent journal for kill-safe workspace mutations."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from contextlib import contextmanager, suppress
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from . import posix_atomic_commit, windows_atomic_commit, windows_file_io
from .file_io import read_workspace_text
from .file_identity import FileIdentity, MissingFile, WorkspaceFileNotFound
from .safety import SandboxViolation, resolve_safe_path
from .policy_enforcement import enforce_current_file_policy
from .transaction_records import (
    MAX_WARNING_BYTES,
    _DIGEST,
    _IDENTITY,
    _ID,
    _TOOLS,
    _StoredRecord,
    TransactionJournalError,
    _decode_record,
    _encode_record,
    _validate_warning,
)
from .workspace_guard import bind_workspace_identity, require_workspace_identity

JOURNAL_DIRECTORY = Path.home() / ".deepseek-mcp" / "transactions"
MAX_RECORDS, MAX_RECORD_BYTES = 128, 64 * 1024
_RECORD_NAME = re.compile(r"([0-9a-f]{32})\.json")
_TEMP_NAME = re.compile(r"\.([0-9a-f]{32})\.json\.tmp")


class JournalUpdatePublishedWarning(TransactionJournalError):
    pass


class JournalAcknowledgementOutcomeUncertain(TransactionJournalError):
    pass


def _encode(record: _StoredRecord) -> bytes:
    return _encode_record(record, MAX_RECORD_BYTES)


def _decode(data: bytes) -> _StoredRecord:
    return _decode_record(data, MAX_RECORD_BYTES)


def _transaction_id(value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise TransactionJournalError("transaction id must be 32 lowercase hex characters")
    return value
def _workspace_identity(config) -> str:
    token = getattr(config, "expected_workspace_identity", None)
    workspace = getattr(config, "workspace", None)
    if not isinstance(token, str) or _IDENTITY.fullmatch(token) is None:
        raise TransactionJournalError("configured workspace identity is invalid")
    if not isinstance(workspace, Path):
        raise TransactionJournalError("configured workspace path is invalid")
    try:
        require_workspace_identity(workspace, token)
    except (OSError, RuntimeError, ValueError) as error:
        raise TransactionJournalError("configured workspace identity changed") from error
    return token
def _relative_target(config, arguments: object) -> str:
    if not isinstance(arguments, dict) or not isinstance(arguments.get("path"), str):
        raise TransactionJournalError("mutation arguments require a path")
    try:
        root = config.workspace.resolve()
        target = resolve_safe_path(arguments["path"], root)
        relative = target.relative_to(root)
        enforce_current_file_policy(relative.as_posix())
    except (OSError, RuntimeError, ValueError, SandboxViolation) as error:
        raise TransactionJournalError("mutation path is outside the workspace") from error
    if not relative.parts:
        raise TransactionJournalError("mutation path must identify a file")
    return relative.as_posix()
def _digest(value: object) -> str:
    if isinstance(value, bytes) and len(value) == hashlib.sha256().digest_size:
        return value.hex()
    if isinstance(value, str) and _DIGEST.fullmatch(value) is not None:
        return value
    raise TransactionJournalError("mutation digest must be a SHA-256 value")
def _name(transaction_id: str) -> str: return f"{transaction_id}.json"
def _scope(identity: str) -> str: return hashlib.sha256(bytes.fromhex(identity)).hexdigest()
def _validate_posix_directory(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise TransactionJournalError("journal directory has unsafe ownership or type")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise TransactionJournalError("journal directory mode must be 0700")
def _validate_posix_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise TransactionJournalError("journal file has unsafe ownership or type")
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise TransactionJournalError("journal file must be private and uniquely linked")
def _directory_flags() -> int:
    return (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
def _open_private_directory(path: Path, *, create: bool) -> int:
    if create:
        with suppress(FileExistsError): os.mkdir(path, 0o700)
    descriptor = os.open(path, _directory_flags())
    try:
        _validate_posix_directory(descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
def _open_child_directory(parent: int, name: str) -> int:
    with suppress(FileExistsError): os.mkdir(name, 0o700, dir_fd=parent)
    child = os.open(name, _directory_flags(), dir_fd=parent)
    try:
        _validate_posix_directory(child)
        os.fsync(parent)
        return child
    except BaseException:
        os.close(child)
        raise
def _open_posix_journal(identity: str) -> int:
    directory = Path(os.path.abspath(JOURNAL_DIRECTORY))
    parent = _open_private_directory(directory.parent, create=True)
    try: journal = _open_child_directory(parent, directory.name)
    finally: os.close(parent)
    try: return _open_child_directory(journal, _scope(identity))
    finally: os.close(journal)
def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    return all(getattr(first, field) == getattr(second, field) for field in fields)
def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_RECORD_BYTES:
        chunk = os.read(descriptor, min(65536, MAX_RECORD_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > MAX_RECORD_BYTES:
        raise TransactionJournalError("journal record exceeds 64 KiB")
    return b"".join(chunks)
def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise TransactionJournalError("journal write made no progress")
        view = view[written:]
class _PosixStore:
    def __init__(self, identity: str) -> None:
        self.directory = _open_posix_journal(identity)
        self.lock = -1
    def __enter__(self) -> "_PosixStore":
        import fcntl
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            self.lock = os.open(".lock", flags, 0o600, dir_fd=self.directory)
            _validate_posix_file(os.fstat(self.lock))
            fcntl.flock(self.lock, fcntl.LOCK_EX)
            return self
        except BaseException:
            if self.lock >= 0: os.close(self.lock)
            os.close(self.directory)
            raise
    def __exit__(self, *_unused) -> None:
        if self.lock >= 0: os.close(self.lock)
        os.close(self.directory)
    def names(self) -> list[str]:
        entries = os.listdir(self.directory)
        for name in sorted(item for item in entries if _TEMP_NAME.fullmatch(item)):
            stale = self.read(name)
            if stale is not None: self._remove_owned(name, stale[1])
        names = sorted(name for name in entries if _RECORD_NAME.fullmatch(name))
        if len(names) > MAX_RECORDS:
            raise TransactionJournalError("journal record capacity exceeded")
        return names
    def read(self, name: str) -> tuple[bytes, os.stat_result] | None:
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=self.directory)
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(descriptor)
            _validate_posix_file(before)
            data = _read_all(descriptor)
            after = os.fstat(descriptor)
            if len(data) != before.st_size or not _same_file(before, after):
                raise TransactionJournalError("journal record changed while reading")
            return data, after
        finally:
            os.close(descriptor)
    def write(self, name: str, data: bytes, baseline: os.stat_result | None) -> None:
        temporary = f".{name}.tmp"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=self.directory)
        try:
            _validate_posix_file(os.fstat(descriptor))
            os.ftruncate(descriptor, 0)
            _write_all(descriptor, data)
            os.fsync(descriptor)
            temp_info = os.fstat(descriptor)
            self._publish(temporary, name, temp_info, baseline)
        finally:
            os.close(descriptor)
            self._remove_owned(temporary, locals().get("temp_info"))
    def _publish(
        self, temporary: str, name: str, temp_info: os.stat_result,
        baseline: os.stat_result | None,
    ) -> None:
        current = os.stat(name, dir_fd=self.directory, follow_symlinks=False) if baseline else None
        if baseline is not None and not _same_file(current, baseline):
            raise TransactionJournalError("journal record changed before replacement")
        if baseline is None:
            posix_atomic_commit._move_no_replace(self.directory, temporary, name)
        else:
            os.replace(
                temporary, name, src_dir_fd=self.directory, dst_dir_fd=self.directory,
            )
        _validate_posix_file(os.stat(name, dir_fd=self.directory, follow_symlinks=False))
        try: os.fsync(self.directory)
        except OSError as error: raise JournalUpdatePublishedWarning(
            "journal update published but directory durability is not confirmed"
        ) from error
    def _remove_owned(self, name: str, expected: os.stat_result | None) -> None:
        if expected is None:
            return
        try:
            current = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
            if _same_file(current, expected):
                os.unlink(name, dir_fd=self.directory)
                os.fsync(self.directory)
        except FileNotFoundError:
            return
    def delete(self, name: str, baseline: os.stat_result) -> None:
        current = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
        if not _same_file(current, baseline):
            raise TransactionJournalError("journal record changed before acknowledgement")
        os.unlink(name, dir_fd=self.directory)
        os.fsync(self.directory)
def _windows_info_is_safe(info: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not attributes & reparse
def _open_windows_mutable(path: Path, *, create: bool) -> int:
    if os.name != "nt":
        raise TransactionJournalError("Windows journal backend is unavailable")
    import ctypes, msvcrt
    parent = windows_file_io._open_directory(path.parent)
    handle = descriptor = -1
    try:
        expected = windows_file_io._normalized(os.path.join(parent.expected, path.name))
        creation = windows_file_io._OPEN_ALWAYS if create else windows_file_io._OPEN_EXISTING
        opened = windows_file_io._CREATE_FILE(
            expected, windows_file_io._READ | windows_file_io._WRITE | 0x10000,
            0, None, creation, windows_file_io._OPEN_REPARSE, None,
        )
        if opened in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        handle = int(opened)
        windows_file_io._validate_handle(handle, expected, directory=False)
        windows_file_io._validate_acl(handle)
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | getattr(os, "O_BINARY", 0))
        handle = -1
        os.set_inheritable(descriptor, False)
        return descriptor
    finally:
        if handle >= 0:
            windows_file_io._close(handle)
        windows_file_io._close(parent.handle)
def _prepare_windows_directory(path: Path) -> None: path.mkdir(mode=0o700, exist_ok=True); windows_file_io.validate_private_path(path, directory=True)
class _WindowsStore:
    def __init__(self, identity: str) -> None:
        root = Path(os.path.abspath(JOURNAL_DIRECTORY))
        _prepare_windows_directory(root.parent)
        _prepare_windows_directory(root)
        self.path = root / _scope(identity)
        _prepare_windows_directory(self.path)
        self.lock = -1
    def __enter__(self) -> "_WindowsStore":
        try:
            self.lock = windows_file_io.open_exclusive_regular(self.path / ".lock")
            if not _windows_info_is_safe(os.fstat(self.lock)):
                raise TransactionJournalError("Windows journal lock is not private")
            return self
        except BaseException:
            if self.lock >= 0: os.close(self.lock)
            raise
    def __exit__(self, *_unused) -> None:
        if self.lock >= 0: os.close(self.lock)
    def names(self) -> list[str]:
        windows_file_io.validate_private_path(self.path, directory=True)
        entries = [entry.name for entry in os.scandir(self.path)]
        for name in sorted(item for item in entries if _TEMP_NAME.fullmatch(item)):
            stale = self.read(name)
            if stale is not None: self.delete(name, stale[1])
        names = sorted(name for name in entries if _RECORD_NAME.fullmatch(name))
        windows_file_io.validate_private_path(self.path, directory=True)
        if len(names) > MAX_RECORDS:
            raise TransactionJournalError("journal record capacity exceeded")
        return names
    def read(self, name: str) -> tuple[bytes, os.stat_result] | None:
        try:
            data, info = windows_file_io.read_regular(
                self.path / name, max_bytes=MAX_RECORD_BYTES,
            )
        except FileNotFoundError:
            return None
        if not _windows_info_is_safe(info):
            raise TransactionJournalError("Windows journal file is not private")
        return data, info
    def write(self, name: str, data: bytes, baseline: os.stat_result | None) -> None:
        temporary = self.path / f".{name}.tmp"
        descriptor = _open_windows_mutable(temporary, create=True)
        published = [False]
        try:
            if not _windows_info_is_safe(os.fstat(descriptor)):
                raise TransactionJournalError("Windows journal temporary is not private")
            os.ftruncate(descriptor, 0)
            _write_all(descriptor, data)
            os.fsync(descriptor)
            self._publish(descriptor, name, baseline, published)
        finally:
            if not published[0]:
                with suppress(OSError):
                    windows_atomic_commit.mark_delete(descriptor)
            os.close(descriptor)
    def _publish(self, descriptor: int, name: str, baseline: os.stat_result | None, published: list[bool]) -> None:
        current = self.read(name)
        if baseline is None and current is not None:
            raise TransactionJournalError("journal transaction already exists")
        if baseline is not None and (current is None or not _same_file(current[1], baseline)):
            raise TransactionJournalError("journal record changed before replacement")
        parent = windows_file_io._open_directory(self.path)
        try:
            windows_atomic_commit.rename(
                descriptor, parent.handle, name, replace=baseline is not None,
            )
            published[0] = True
        finally:
            try: windows_file_io._close(parent.handle)
            except OSError as error:
                if published[0]: raise JournalUpdatePublishedWarning("Windows journal update published but parent audit failed") from error
                raise
    def delete(self, name: str, baseline: os.stat_result) -> None:
        descriptor = _open_windows_mutable(self.path / name, create=False)
        try:
            if not _same_file(os.fstat(descriptor), baseline):
                raise TransactionJournalError("journal record changed before acknowledgement")
            windows_atomic_commit.mark_delete(descriptor)
        finally:
            os.close(descriptor)
@contextmanager
def _store(identity: str) -> Iterator[_PosixStore | _WindowsStore]:
    try:
        backend = _WindowsStore(identity) if os.name == "nt" else _PosixStore(identity)
        with backend:
            yield backend
    except TransactionJournalError:
        raise
    except (OSError, windows_file_io.WindowsPathError) as error:
        raise TransactionJournalError(f"journal storage operation failed: {error}") from error
def _baseline_fields(baseline: object) -> tuple[str, str | None]:
    if isinstance(baseline, MissingFile):
        return "missing", None
    if isinstance(baseline, FileIdentity) and baseline.digest is not None:
        if len(baseline.digest) == hashlib.sha256().digest_size:
            return "present", baseline.digest.hex()
    return "unknown", None

def record_intent(
    config, transaction_id: str, tool: str, arguments: dict, digest: bytes | str,
    baseline: object = None,
) -> dict[str, object]:
    identity = _workspace_identity(config)
    identifier = _transaction_id(transaction_id)
    if tool not in _TOOLS:
        raise TransactionJournalError("journal tool is not a mutation tool")
    baseline_state, baseline_sha256 = _baseline_fields(baseline)
    record = _StoredRecord(
        identifier, identity, tool, _relative_target(config, arguments), _digest(digest),
        (), 2, baseline_state, baseline_sha256,
    )
    encoded = _encode(record)
    with _store(identity) as store:
        names = store.names()
        existing = store.read(_name(identifier))
        if existing is not None:
            if _decode(existing[0]) != record:
                raise TransactionJournalError("transaction id already has a different intent")
        else:
            if len(names) >= MAX_RECORDS:
                raise TransactionJournalError("journal record capacity exceeded")
            store.write(_name(identifier), encoded, None)
    return record.public_payload("pending")
def append_warning(config, transaction_id: str, warning: str) -> dict[str, object]:
    identity = _workspace_identity(config)
    identifier = _transaction_id(transaction_id)
    checked_warning = _validate_warning(warning)
    with _store(identity) as store:
        existing = store.read(_name(identifier))
        if existing is None:
            raise TransactionJournalError("journal transaction does not exist")
        record = _decode(existing[0])
        if record.workspace_identity != identity:
            raise TransactionJournalError("journal transaction belongs to another workspace")
        updated = replace(record, warnings=(*record.warnings, checked_warning))
        store.write(_name(identifier), _encode(updated), existing[1])
    return updated.public_payload("pending")
def _classify(config, record: _StoredRecord) -> dict[str, object]:
    try:
        with bind_workspace_identity(record.workspace_identity):
            _text, current = read_workspace_text(config.workspace, record.path)
    except WorkspaceFileNotFound:
        status = "not_committed" if record.baseline_state == "missing" else "uncertain"
    except (OSError, RuntimeError, ValueError, SandboxViolation):
        status = "uncertain"
    else:
        current_digest = current.digest.hex() if current.digest is not None else None
        if current_digest == record.sha256:
            status = "committed"
        elif record.baseline_state == "present" and current_digest == record.baseline_sha256:
            status = "not_committed"
        else:
            status = "uncertain"
    return record.public_payload(status)
def pending_records(config) -> list[dict[str, object]]:
    identity = _workspace_identity(config)
    records: list[_StoredRecord] = []
    with _store(identity) as store:
        for name in store.names():
            existing = store.read(name)
            if existing is None:
                raise TransactionJournalError("journal record vanished while enumerating")
            record = _decode(existing[0])
            if record.workspace_identity == identity:
                records.append(record)
    return [_classify(config, record) for record in records]
def _ack_ids(ids) -> list[str]:
    if isinstance(ids, (str, bytes)):
        raise TransactionJournalError("acknowledgement ids must be an iterable")
    try: iterator = iter(ids)
    except TypeError as error: raise TransactionJournalError("acknowledgement ids must be an iterable") from error
    values: set[str] = set()
    for index, value in enumerate(iterator):
        if index >= MAX_RECORDS:
            raise TransactionJournalError("too many acknowledgement ids")
        values.add(_transaction_id(value))
    return sorted(values)
def acknowledge(config, ids) -> list[str]:
    identity = _workspace_identity(config)
    identifiers = _ack_ids(ids)
    entries = []
    deleting = False
    try:
        with _store(identity) as store:
            for identifier in identifiers:
                existing = store.read(_name(identifier))
                if existing is None:
                    continue
                record = _decode(existing[0])
                if record.workspace_identity != identity:
                    continue
                if _classify(config, record)["status"] not in {"committed", "not_committed"}:
                    raise TransactionJournalError("cannot acknowledge a mutation whose current file matches neither its committed nor original baseline state")
                entries.append((identifier, existing[1]))
            deleting = bool(entries)
            for identifier, version in entries:
                store.delete(_name(identifier), version)
    except JournalAcknowledgementOutcomeUncertain:
        raise
    except (TransactionJournalError, OSError) as error:
        if deleting:
            raise JournalAcknowledgementOutcomeUncertain(
                "journal acknowledgement may be partially published; re-query recovery before retrying"
            ) from error
        raise
    return [identifier for identifier, _version in entries]

