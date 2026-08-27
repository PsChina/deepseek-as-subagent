"""Shared errors and identity baselines for workspace file operations."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass


class ToolInputError(ValueError):
    """A tool input or bounded workspace file is invalid."""


class WorkspaceFileNotFound(ToolInputError):
    pass


class MutationCommittedWarning(RuntimeError):
    """The mutation committed, but post-commit durability or cleanup is uncertain."""


def bounded_integer(
    value: object,
    label: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolInputError(f"{label} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        limit = f" between {minimum} and {maximum}" if maximum else (
            f" at least {minimum}"
        )
        raise ToolInputError(f"{label} must be{limit}")
    return value


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int
    digest: bytes | None = None

    @classmethod
    def from_stat(
        cls, info: os.stat_result, digest: bytes | None = None
    ) -> "FileIdentity":
        return cls(
            info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, digest,
        )

    def matches_stat(self, info: os.stat_result) -> bool:
        current = FileIdentity.from_stat(info)
        return (
            current.device, current.inode, current.mode, current.size,
            current.modified_ns, current.changed_ns,
        ) == (
            self.device, self.inode, self.mode, self.size,
            self.modified_ns, self.changed_ns,
        )


class MissingFile:
    pass


MISSING_FILE = MissingFile()
ExpectedIdentity = FileIdentity | MissingFile | None


def validate_target(info: os.stat_result | None, expected: ExpectedIdentity) -> int:
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise ToolInputError("write target is not a regular file")
    if expected is MISSING_FILE and info is not None:
        raise ToolInputError("write target appeared during edit")
    if isinstance(expected, FileIdentity):
        if info is None or not expected.matches_stat(info):
            raise ToolInputError("write target changed during edit")
    return stat.S_IMODE(info.st_mode) if info is not None else 0o600


def write_baseline(
    info: os.stat_result | None, expected: ExpectedIdentity
) -> tuple[int, FileIdentity | MissingFile]:
    mode = validate_target(info, expected)
    baseline = expected if expected is not None else (
        FileIdentity.from_stat(info) if info is not None else MISSING_FILE
    )
    return mode, baseline
