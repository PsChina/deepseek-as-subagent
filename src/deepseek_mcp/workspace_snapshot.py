"""Create disposable regular-file snapshots for untrusted container commands."""
from __future__ import annotations

import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from .git_snapshot import (
    GitSnapshotError,
    materialize_git_snapshot,
    validate_git_marker_info,
)
from .safety import is_agent_control_name, is_protected_host_path, is_vcs_control_name
from .workspace_guard import require_workspace_identity, require_workspace_stat
from .hard_deadline import Deadline, HardDeadline, remaining

MAX_SNAPSHOT_ENTRIES = 50_000
MAX_SNAPSHOT_DIRECTORIES = 10_000
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_DEPTH = 64
MAX_SNAPSHOT_SECONDS = 30.0
MAX_STALE_SNAPSHOTS = 16
_CHUNK_BYTES = 1024 * 1024
_LABEL_RE = re.compile(r"[0-9a-f]{64}")
_SNAPSHOT_RE = re.compile(r"deepseek-mcp-[0-9a-f]{16}-[0-9a-f]{32}")


class WorkspaceSnapshotError(RuntimeError):
    """A safe disposable workspace snapshot could not be prepared or removed."""


@dataclass
class _CopyBudget:
    deadline: Deadline
    allocation_unit: int
    snapshot_root: Path
    entries: int = 0
    directories: int = 1
    bytes_copied: int = 0
    files: int = 0
    path_bytes: int = 0
    tree_name_bytes: int = 0
    git_repository: bool = False

    def check_time(self) -> None:
        if remaining(self.deadline) <= 0:
            raise WorkspaceSnapshotError("workspace snapshot exceeded its time limit")

    def add_entry(self, depth: int, name: str) -> None:
        self.check_time()
        self.entries += 1
        self.tree_name_bytes += len(os.fsencode(name))
        if self.entries > MAX_SNAPSHOT_ENTRIES:
            raise WorkspaceSnapshotError("workspace snapshot has too many entries")
        if depth > MAX_SNAPSHOT_DEPTH:
            raise WorkspaceSnapshotError("workspace snapshot is nested too deeply")

    def add_directory(self) -> None:
        self.directories += 1
        if self.directories > MAX_SNAPSHOT_DIRECTORIES:
            raise WorkspaceSnapshotError("workspace snapshot has too many directories")

    def add_bytes(self, amount: int) -> None:
        self.bytes_copied += amount
        if self.bytes_copied > MAX_SNAPSHOT_BYTES:
            raise WorkspaceSnapshotError("workspace snapshot is too large")

    def add_file(self, path: Path, root: Path) -> None:
        self.files += 1
        self.path_bytes += len(os.fsencode(path.relative_to(root)))


def _staging_root() -> Path:
    base = Path.home() / ".deepseek-mcp"
    _ensure_private_directory(base)
    root = base / "snapshots"
    _ensure_private_directory(root)
    return root


def _ensure_private_directory(path: Path) -> None:
    if os.name != "posix":
        raise WorkspaceSnapshotError("container snapshots require a POSIX host")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDONLY | os.O_DIRECTORY
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            os.fchmod(descriptor, 0o700)
            info = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise WorkspaceSnapshotError(f"cannot secure snapshot directory: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise WorkspaceSnapshotError("snapshot directory is not privately owned")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise WorkspaceSnapshotError("snapshot directory mode must be 0700")


def _validate_label(workspace_label: str) -> str:
    if not isinstance(workspace_label, str) or not _LABEL_RE.fullmatch(workspace_label):
        raise WorkspaceSnapshotError("workspace snapshot label is invalid")
    return workspace_label[:16]


def _new_snapshot_directory(workspace_label: str, root: Path) -> Path:
    prefix = _validate_label(workspace_label)
    transaction = os.environ.get("DEEPSEEK_TOOL_TRANSACTION_ID", "")
    if re.fullmatch(r"[0-9a-f]{32}", transaction):
        path = root / f"deepseek-mcp-{prefix}-{transaction}"
        try:
            path.mkdir(mode=0o700)
            return path
        except OSError as exc:
            raise WorkspaceSnapshotError(
                f"cannot create workspace snapshot: {exc}"
            ) from exc
    for _ in range(4):
        path = root / f"deepseek-mcp-{prefix}-{uuid.uuid4().hex}"
        try:
            path.mkdir(mode=0o700)
            return path
        except FileExistsError:
            continue
        except OSError as exc:
            raise WorkspaceSnapshotError(f"cannot create workspace snapshot: {exc}") from exc
    raise WorkspaceSnapshotError("could not allocate a unique workspace snapshot")


def _open_source_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkspaceSnapshotError(f"cannot open workspace directory: {exc}") from exc
    info = os.fstat(descriptor)
    require_workspace_stat(info)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise WorkspaceSnapshotError("workspace root is not a real directory")
    return descriptor


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino, first.st_mode) == (
        second.st_dev,
        second.st_ino,
        second.st_mode,
    )


def _same_file_version(first: os.stat_result, second: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    return all(getattr(first, field) == getattr(second, field) for field in fields)


def _same_directory_version(first: os.stat_result, second: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_mode", "st_mtime_ns", "st_ctime_ns")
    return all(getattr(first, field) == getattr(second, field) for field in fields)


def _write_all(target: int, chunk: bytes) -> None:
    view = memoryview(chunk)
    while view:
        written = os.write(target, view)
        if written <= 0:
            raise WorkspaceSnapshotError("workspace snapshot write made no progress")
        view = view[written:]


def _copy_regular_file(
    source_directory: int,
    name: str,
    expected: os.stat_result,
    destination: Path,
    budget: _CopyBudget,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    source = os.open(name, flags, dir_fd=source_directory)
    target: int | None = None
    copied = 0
    try:
        actual = os.fstat(source)
        if not stat.S_ISREG(actual.st_mode) or not _same_object(expected, actual):
            raise WorkspaceSnapshotError("workspace entry changed during snapshot")
        if actual.st_size > MAX_SNAPSHOT_BYTES - budget.bytes_copied:
            raise WorkspaceSnapshotError("workspace snapshot is too large")
        budget.add_file(destination, budget.snapshot_root)
        target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        target_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        target = os.open(destination, target_flags, 0o600)
        while chunk := os.read(source, _CHUNK_BYTES):
            budget.check_time()
            budget.add_bytes(len(chunk))
            copied += len(chunk)
            _write_all(target, chunk)
        final = os.fstat(source)
        if copied != actual.st_size or not _same_file_version(actual, final):
            raise WorkspaceSnapshotError("workspace file changed during snapshot")
        executable = 0o111 if stat.S_IMODE(actual.st_mode) & 0o111 else 0
        snapshot_mode = 0o444 | executable
        os.fchmod(target, snapshot_mode)
    except OSError as exc:
        raise WorkspaceSnapshotError(f"cannot copy regular workspace file: {exc}") from exc
    finally:
        os.close(source)
        if target is not None:
            os.close(target)


def _copy_directory(
    source: int,
    destination: Path,
    depth: int,
    budget: _CopyBudget,
) -> None:
    try:
        before = os.fstat(source)
        iterator = os.scandir(source)
        with iterator:
            for entry in iterator:
                _copy_entry(source, destination, depth, budget, entry)
        if not _same_directory_version(before, os.fstat(source)):
            raise WorkspaceSnapshotError("workspace directory changed during snapshot")
    except OSError as exc:
        raise WorkspaceSnapshotError(f"cannot scan workspace snapshot source: {exc}") from exc


def _copy_entry(
    source: int,
    destination: Path,
    depth: int,
    budget: _CopyBudget,
    entry: os.DirEntry,
) -> None:
    budget.add_entry(depth, entry.name)
    expected = entry.stat(follow_symlinks=False)
    target = destination / entry.name
    if is_vcs_control_name(entry.name):
        if depth == 1 and entry.name.casefold() == ".git":
            try:
                budget.git_repository = validate_git_marker_info(expected)
            except GitSnapshotError as exc:
                raise WorkspaceSnapshotError(str(exc)) from None
        return
    if is_agent_control_name(entry.name):
        return
    if stat.S_ISREG(expected.st_mode):
        _copy_regular_file(source, entry.name, expected, target, budget)
    elif stat.S_ISDIR(expected.st_mode):
        _copy_child_directory(source, entry.name, expected, target, depth, budget)


def _copy_child_directory(
    parent: int,
    name: str,
    expected: os.stat_result,
    destination: Path,
    depth: int,
    budget: _CopyBudget,
) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    child = os.open(name, flags, dir_fd=parent)
    try:
        actual = os.fstat(child)
        if not _same_object(expected, actual):
            raise WorkspaceSnapshotError("workspace directory changed during snapshot")
        budget.add_directory()
        destination.mkdir(mode=0o700)
        _copy_directory(child, destination, depth + 1, budget)
        destination.chmod(0o555)
    except OSError as exc:
        raise WorkspaceSnapshotError(f"cannot copy workspace directory: {exc}") from exc
    finally:
        os.close(child)


def _validate_separate_roots(workspace: Path, staging: Path) -> None:
    try:
        source = workspace.resolve(strict=True)
        target = staging.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceSnapshotError(f"cannot resolve snapshot roots: {exc}") from exc
    if source == target or source in target.parents or target in source.parents:
        raise WorkspaceSnapshotError("workspace and snapshot staging roots overlap")


def create_workspace_snapshot(workspace: Path, workspace_label: str) -> Path:
    """Copy regular files into a private, disposable tree without following links."""
    require_workspace_identity(workspace)
    if is_protected_host_path(workspace):
        raise WorkspaceSnapshotError("workspace is a protected host control path")
    staging = _staging_root()
    _validate_separate_roots(workspace, staging)
    snapshot = _new_snapshot_directory(workspace_label, staging)
    source = -1
    try:
        source = _open_source_directory(workspace)
        allocation_unit = max(512, os.statvfs(snapshot).f_frsize)
        budget = _CopyBudget(
            HardDeadline.after(MAX_SNAPSHOT_SECONDS),
            allocation_unit,
            snapshot_root=snapshot,
        )
        _copy_directory(source, snapshot, 1, budget)
        try:
            materialize_git_snapshot(
                workspace,
                snapshot,
                budget,
                max_bytes=MAX_SNAPSHOT_BYTES,
                deadline=budget.deadline,
            )
        except (GitSnapshotError, OSError) as exc:
            raise WorkspaceSnapshotError(str(exc)) from None
        snapshot.chmod(0o555)
        return snapshot
    except BaseException:
        try:
            cleanup_workspace_snapshot(snapshot)
        except WorkspaceSnapshotError:
            raise WorkspaceSnapshotError(
                "workspace snapshot failed and cleanup was not confirmed"
            ) from None
        raise
    finally:
        if source >= 0:
            os.close(source)


def _managed_snapshot(path: Path, root: Path) -> bool:
    return path.parent == root and bool(_SNAPSHOT_RE.fullmatch(path.name))


def _remove_error(function, path, _error) -> None:
    os.chmod(path, 0o700)
    function(path)


def _make_snapshot_removable(path: Path) -> None:
    try:
        for current, directories, _files in os.walk(path, followlinks=False):
            os.chmod(current, 0o700)
            for name in directories:
                child = Path(current) / name
                if stat.S_ISDIR(child.lstat().st_mode) and not child.is_symlink():
                    child.chmod(0o700)
    except OSError as exc:
        raise WorkspaceSnapshotError(
            f"cannot prepare workspace snapshot for removal: {exc}"
        ) from exc


def _cleanup_root(staging_root: Path | None) -> Path:
    if staging_root is None:
        return _staging_root()
    root = Path(staging_root)
    if not root.is_absolute() or root != Path(os.path.abspath(root)):
        raise WorkspaceSnapshotError("snapshot staging root must be canonical and absolute")
    _ensure_private_directory(root)
    return root


def cleanup_workspace_snapshot(
    path: Path,
    staging_root: Path | None = None,
) -> None:
    """Remove exactly one validated managed snapshot without following root links."""
    root = _cleanup_root(staging_root)
    candidate = Path(path)
    if not candidate.is_absolute() or candidate != Path(os.path.abspath(candidate)):
        raise WorkspaceSnapshotError("managed snapshot path must be canonical and absolute")
    if not _managed_snapshot(candidate, root):
        raise WorkspaceSnapshotError("refusing to remove an unmanaged snapshot path")
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or candidate.is_symlink():
        raise WorkspaceSnapshotError("managed snapshot path is not a real directory")
    try:
        _make_snapshot_removable(candidate)
        shutil.rmtree(candidate, onerror=_remove_error)
    except OSError as exc:
        raise WorkspaceSnapshotError(f"cannot remove workspace snapshot: {exc}") from exc
    if candidate.exists() or candidate.is_symlink():
        raise WorkspaceSnapshotError("workspace snapshot removal was not confirmed")


def cleanup_stale_snapshots(workspace_label: str) -> None:
    """Remove bounded stale snapshots for a workspace while its lease is held."""
    prefix = f"deepseek-mcp-{_validate_label(workspace_label)}-"
    root = _staging_root()
    candidates = [path for path in root.iterdir() if path.name.startswith(prefix)]
    for candidate in candidates[:MAX_STALE_SNAPSHOTS]:
        cleanup_workspace_snapshot(candidate)
    if len(candidates) > MAX_STALE_SNAPSHOTS:
        raise WorkspaceSnapshotError(
            "removed a bounded batch of stale snapshots; retry cleanup"
        )
