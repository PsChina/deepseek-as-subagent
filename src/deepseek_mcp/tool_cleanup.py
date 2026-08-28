"""Identity-checked cleanup for artifacts owned by one tool subprocess."""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from . import file_io
from .file_identity import ToolInputError, WorkspaceFileNotFound
from .workspace_snapshot import WorkspaceSnapshotError, cleanup_workspace_snapshot
from .workspace_guard import bind_workspace_identity

_MUTATION_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})


def _temp_name(transaction_id: str) -> str:
    return f".deepseek-mcp-{transaction_id}.tmp"


def _cleanup_posix_temp(workspace: Path, label: str, transaction_id: str) -> None:
    root, relative = file_io._location(workspace, label)
    try:
        parent = file_io._open_posix_parent(root, relative, create=False)
    except (FileNotFoundError, WorkspaceFileNotFound):
        return
    try:
        name = _temp_name(transaction_id)
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
        ):
            raise ToolInputError("managed tool temporary file is not safe to preserve")
        # A killed POSIX rename-exchange may leave either the model replacement
        # or displaced user data at this name. Without the child's digest, an
        # identity-safe cleanup cannot distinguish them; preserve it.
    finally:
        os.close(parent)


def _cleanup_windows_temp(workspace: Path, label: str, transaction_id: str) -> None:
    root, relative = file_io._location(workspace, label)
    try:
        parent = file_io._win_open_parent(root, relative, create=False)
    except (FileNotFoundError, WorkspaceFileNotFound):
        return
    descriptor = -1
    try:
        try:
            handle = file_io._win_open_child(
                parent,
                _temp_name(transaction_id),
                file_io._WIN_READ | file_io._WIN_DELETE,
                file_io._WIN_OPEN_EXISTING,
                directory=False,
            )
        except FileNotFoundError:
            return
        descriptor = file_io._win_fd(handle, os.O_RDONLY)
        if os.fstat(descriptor).st_nlink != 1:
            raise ToolInputError("managed tool temporary file is not safe to remove")
        file_io._win_mark_delete(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        file_io._win_close(parent)


def _cleanup_snapshot(identity_token: str, transaction_id: str) -> None:
    label = hashlib.sha256(bytes.fromhex(identity_token)).hexdigest()[:16]
    root = Path.home() / ".deepseek-mcp" / "snapshots"
    snapshot = root / f"deepseek-mcp-{label}-{transaction_id}"
    try:
        cleanup_workspace_snapshot(snapshot, root)
    except WorkspaceSnapshotError:
        if snapshot.exists() or snapshot.is_symlink():
            raise


def cleanup_tool_artifacts(
    config, name: str, arguments: dict, transaction_id: str
) -> None:
    assert config.expected_workspace_identity is not None
    with bind_workspace_identity(config.expected_workspace_identity):
        if name in _MUTATION_TOOLS:
            label = arguments.get("path")
            if not isinstance(label, str) or not label:
                return
            if os.name == "nt":
                _cleanup_windows_temp(config.workspace, label, transaction_id)
            else:
                _cleanup_posix_temp(config.workspace, label, transaction_id)
        if name == "Bash":
            _cleanup_snapshot(config.expected_workspace_identity, transaction_id)
