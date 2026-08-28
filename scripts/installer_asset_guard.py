"""Bounded, no-follow comparisons for optional Claude helper assets."""
from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from deepseek_mcp import windows_file_io

MAX_HELPER_BYTES = 1024 * 1024
PUBLISHED_DIGESTS = {
    "skill": "5fee1ad4ee0607694d2955772215de641f13366bb17b5c6348ed0e87eaecee65",
    "command": "de0a8464a5fc1a7ac666606b7ae46fe4334387195034667ca5e45246eabf5562",
}


class AssetGuardError(RuntimeError):
    pass


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def _require_owned(info: os.stat_result) -> None:
    if os.name == "posix" and info.st_uid != os.getuid():
        raise AssetGuardError("helper asset owner is not trusted")


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_HELPER_BYTES:
        chunk = os.read(descriptor, min(65536, MAX_HELPER_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > MAX_HELPER_BYTES:
        raise AssetGuardError("helper asset exceeded its size limit")
    return b"".join(chunks)


def _read_posix_regular(path: Path) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_HELPER_BYTES:
        raise AssetGuardError("helper asset is not a bounded regular file")
    _require_owned(before)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file(before, opened):
            raise AssetGuardError("helper asset changed before it was read")
        data = _read_descriptor(descriptor)
        after = os.fstat(descriptor)
        if not _same_file(opened, after):
            raise AssetGuardError("helper asset changed while it was read")
        if len(data) != opened.st_size:
            raise AssetGuardError("helper asset ended before its validated size")
        return data
    finally:
        os.close(descriptor)


def _read_regular(path: Path) -> bytes:
    try:
        if os.name == "nt":
            return windows_file_io.read_regular(path, max_bytes=MAX_HELPER_BYTES)[0]
        return _read_posix_regular(path)
    except (OSError, windows_file_io.WindowsPathError) as exc:
        raise AssetGuardError("helper asset could not be read safely") from exc


def _bounded_entry_names(iterator: Iterator[os.DirEntry[str]]) -> list[str]:
    names: list[str] = []
    for entry in iterator:
        names.append(entry.name)
        if len(names) == 2:
            break
    return names


def _list_posix_directory(path: Path, before: os.stat_result) -> list[str]:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or not _same_file(before, opened):
            raise AssetGuardError("helper skill directory changed before listing")
        with os.scandir(descriptor) as iterator:
            entries = _bounded_entry_names(iterator)
        if not _same_file(opened, os.fstat(descriptor)):
            raise AssetGuardError("helper skill directory changed while listing")
        return entries
    finally:
        os.close(descriptor)


def _skill_file(path: Path) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AssetGuardError("helper skill directory is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise AssetGuardError("helper skill is not a real directory")
    _require_owned(info)
    try:
        if os.name == "nt":
            windows_file_io.validate_private_path(path, directory=True)
            with os.scandir(path) as iterator:
                entries = _bounded_entry_names(iterator)
        else:
            entries = _list_posix_directory(path, info)
    except (OSError, windows_file_io.WindowsPathError) as exc:
        raise AssetGuardError("helper skill directory could not be listed") from exc
    if entries != ["SKILL.md"]:
        raise AssetGuardError("helper skill directory has unexpected entries")
    return path / "SKILL.md"


def _payload(label: str, path: Path) -> bytes:
    if label == "skill":
        path = _skill_file(path)
    elif label != "command":
        raise AssetGuardError("helper label is invalid")
    return _read_regular(path)


def compare_current(label: str, source: Path, destination: Path) -> None:
    if _payload(label, source) != _payload(label, destination):
        raise AssetGuardError("helper asset content does not match")


def verify_published(label: str, destination: Path) -> None:
    expected = PUBLISHED_DIGESTS.get(label)
    if expected is None:
        raise AssetGuardError("helper label is invalid")
    actual = hashlib.sha256(_payload(label, destination)).hexdigest()
    if actual != expected:
        raise AssetGuardError("helper asset digest is not installer-owned")
