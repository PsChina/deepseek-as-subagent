"""Windows workspace write orchestration built on atomic commit primitives."""
from __future__ import annotations

from contextlib import suppress
import hashlib
import ntpath
import os
from pathlib import Path

from . import windows_atomic_commit
from .file_identity import (
    ExpectedIdentity,
    FileIdentity,
    MutationCommittedWarning,
)
from .transaction_report import mutation_ready, mutation_warning


def _publish_name(
    parent: int, source: str, target: str, *, committed_result: bool = True,
) -> None:
    from . import file_io

    handle = file_io._win_open_child(
        parent, source, file_io._WIN_READ | file_io._WIN_DELETE,
        file_io._WIN_OPEN_EXISTING, directory=False,
    )
    descriptor = file_io._win_fd(handle, os.O_RDONLY)
    try:
        file_io._win_rename(descriptor, parent, target, replace=False)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise
    try:
        os.close(descriptor)
    except OSError as error:
        if committed_result:
            raise MutationCommittedWarning(
                f"creation committed; handle close not confirmed: {error}"
            ) from error
        raise


def _recover_partial(
    parent: int, target: str, replacement: str, backup: str,
    error: OSError, rollback: bool,
) -> None:
    source = replacement if rollback else backup
    try:
        _publish_name(parent, source, target, committed_result=False)
    except OSError as restore_error:
        message = (
            f"partial rollback status is uncertain; original retained as "
            f"{replacement}; replacement retained as {backup}"
            if rollback else f"partial replacement restoration is uncertain; "
            f"original retained as {backup}"
        )
        warning = windows_atomic_commit.RollbackCompletedWarning if rollback else OSError
        raise warning(message) from restore_error
    if not rollback:
        raise OSError(
            f"partial replacement aborted; original restored from {backup}"
        ) from error


def _audit_replacement(parent: int, base: str, rollback: bool) -> None:
    from . import file_io

    try:
        unchanged = file_io._win_final_path(parent) == base
    except OSError as error:
        warning = (
            windows_atomic_commit.RollbackCompletedWarning
            if rollback else MutationCommittedWarning
        )
        prefix = "rollback completed" if rollback else "replacement committed"
        raise warning(f"{prefix}; parent audit failed: {error}") from error
    if not unchanged:
        warning = (
            windows_atomic_commit.RollbackCompletedWarning
            if rollback else MutationCommittedWarning
        )
        raise warning("workspace parent identity changed after replacement")


def _replace_with_backup(
    parent: int, target: str, replacement: str, backup: str, *,
    rollback: bool = False,
) -> None:
    from . import file_io

    base = file_io._win_final_path(parent)
    path = lambda name: ntpath.join(base, name)
    try:
        windows_atomic_commit.replace_paths(
            path(target), path(replacement), path(backup)
        )
    except OSError as error:
        if windows_atomic_commit.is_partial_replace(error):
            return _recover_partial(
                parent, target, replacement, backup, error, rollback
            )
        if rollback:
            raise MutationCommittedWarning(
                f"rollback failed; replacement remains committed; "
                f"original retained as {replacement}"
            ) from error
        raise
    _audit_replacement(parent, base, rollback)


def _discard_if_version(
    parent: int, name: str, expected: FileIdentity,
) -> bool:
    from . import file_io

    try:
        handle = file_io._win_open_child(
            parent, name, file_io._WIN_READ | file_io._WIN_DELETE,
            file_io._WIN_OPEN_EXISTING, directory=False,
            sharing=file_io._WIN_SHARE_READ | file_io._WIN_SHARE_WRITE,
        )
    except FileNotFoundError:
        return False
    descriptor = file_io._win_fd(handle, os.O_RDONLY)
    try:
        _text, actual = file_io._checked_read(
            descriptor, strict_utf8=False, reject_binary=False
        )
        if not windows_atomic_commit.same_version(actual, expected):
            return False
        file_io._win_mark_delete(descriptor)
        return True
    finally:
        os.close(descriptor)


def _commit(
    parent: int,
    temporary: str,
    target: str,
    baseline,
    replacement: FileIdentity,
) -> None:
    windows_atomic_commit.commit(
        temporary,
        target,
        baseline,
        replacement,
        lambda source, name: _publish_name(parent, source, name),
        lambda name, source, backup: _replace_with_backup(
            parent, name, source, backup
        ),
        lambda name, identity: _discard_if_version(parent, name, identity),
        rollback=lambda name, source, backup: _replace_with_backup(
            parent, name, source, backup, rollback=True
        ),
    )


def write_windows(
    root: Path, relative: Path, data: bytes, expected: ExpectedIdentity
) -> None:
    from . import file_io

    parent = file_io._win_open_parent(root, relative, create=True)
    temporary = file_io._temporary_name()
    descriptor = -1
    created = False
    replacement: FileIdentity | None = None
    try:
        _mode, baseline = file_io._write_baseline(
            file_io._win_current_info(parent, relative.name), expected
        )
        handle = file_io._win_open_child(
            parent, temporary,
            file_io._WIN_READ | file_io._WIN_WRITE | file_io._WIN_DELETE,
            file_io._WIN_CREATE_NEW, directory=False,
        )
        descriptor = file_io._win_fd(handle, os.O_RDWR)
        created = True
        file_io._write_all(descriptor, data)
        os.fsync(descriptor)
        replacement = FileIdentity.from_stat(
            os.fstat(descriptor), hashlib.sha256(data).digest()
        )
        os.close(descriptor)
        descriptor = -1
        assert replacement.digest is not None
        mutation_ready(replacement.digest)
        file_io._validate_target(
            file_io._win_current_info(parent, relative.name), baseline
        )
        try:
            _commit(parent, temporary, relative.name, baseline, replacement)
        except MutationCommittedWarning as warning:
            mutation_warning(str(warning))
            raise
        created = False
    finally:
        with suppress(OSError):
            if created and descriptor < 0 and replacement is not None:
                _discard_if_version(parent, temporary, replacement)
            elif descriptor >= 0 and created:
                file_io._win_mark_delete(descriptor)
        with suppress(OSError):
            if descriptor >= 0:
                os.close(descriptor)
        with suppress(OSError):
            file_io._win_close(parent)
