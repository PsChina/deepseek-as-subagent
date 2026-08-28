"""Validate host executables used by the optional container boundary."""
from __future__ import annotations

import os
import stat
from pathlib import Path


class TrustedExecutableError(RuntimeError):
    pass


def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _trusted_owner(info: os.stat_result) -> bool:
    return info.st_uid in {0, os.getuid()}


def _validate_directory_chain(directory: Path) -> None:
    for candidate in (directory, *directory.parents):
        try:
            info = candidate.lstat()
        except OSError as error:
            raise TrustedExecutableError("executable parent cannot be inspected") from error
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or not _trusted_owner(info)
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise TrustedExecutableError("executable parent chain is not trusted")


def _validate_open_file(resolved: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(resolved, flags)
        info = os.fstat(descriptor)
        current = resolved.lstat()
    except OSError as error:
        raise TrustedExecutableError("host executable cannot be opened safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    unsafe = (
        not stat.S_ISREG(info.st_mode)
        or not _trusted_owner(info)
        or bool(stat.S_IMODE(info.st_mode) & 0o022)
        or not bool(stat.S_IMODE(info.st_mode) & 0o111)
        or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
    )
    if unsafe:
        raise TrustedExecutableError("host executable file is not trusted")


def validate_trusted_executable(candidate: str | Path, workspace: Path) -> Path:
    """Return a canonical executable protected from other local principals."""
    if os.name != "posix":
        raise TrustedExecutableError("trusted host executable checks require POSIX")
    raw = Path(candidate)
    if not raw.is_absolute():
        raise TrustedExecutableError("host executable path must be absolute")
    raw = Path(os.path.abspath(raw))
    root = workspace.resolve(strict=True)
    if _inside(raw, root):
        raise TrustedExecutableError("host executable is inside the workspace")
    try:
        link_info = raw.lstat()
        resolved = raw.resolve(strict=True)
    except OSError as error:
        raise TrustedExecutableError("host executable does not exist") from error
    if not _trusted_owner(link_info):
        raise TrustedExecutableError("host executable link has unsafe ownership")
    if _inside(resolved, root):
        raise TrustedExecutableError("host executable resolves inside the workspace")
    _validate_directory_chain(raw.parent)
    _validate_directory_chain(resolved.parent)
    _validate_open_file(resolved)
    return resolved
