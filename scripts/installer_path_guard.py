#!/usr/bin/env python3
"""Fail-closed local path checks used before installer reads, writes, or executes."""
from __future__ import annotations

import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from deepseek_mcp import windows_file_io
import installer_asset_guard

MAX_VENV_ENTRIES = 100_000
MAX_GENERATIONS = 10_000
MAX_CONFIG_BYTES = 1024 * 1024
GENERATION_NAME = re.compile(r"generation\.[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


class GuardError(RuntimeError):
    pass


def _is_windows() -> bool:
    return os.name == "nt"


def _validate_windows_path(path: Path, *, directory: bool) -> None:
    if not _is_windows():
        return
    try:
        windows_file_io.validate_private_path(path, directory=directory)
    except (OSError, windows_file_io.WindowsPathError) as exc:
        raise GuardError("Windows path failed handle and ACL validation") from exc


def _validate_windows_descriptor(descriptor: int, path: Path) -> None:
    if not _is_windows():
        return
    try:
        windows_file_io.validate_private_descriptor(descriptor, path)
    except (OSError, windows_file_io.WindowsPathError) as exc:
        raise GuardError("Windows file handle failed ACL validation") from exc


def _is_link_or_reparse(path: Path, info: os.stat_result) -> bool:
    junction = getattr(os.path, "isjunction", lambda _path: False)
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    return path.is_symlink() or junction(path) or bool(attributes & 0x400)


def _check_owner(info: os.stat_result, *, trusted_root: bool = False) -> None:
    if os.name != "posix":
        return
    allowed = {os.getuid()}
    if trusted_root:
        allowed.add(0)
    if info.st_uid not in allowed:
        raise GuardError("path ownership is not trusted")


def secure_directory(path: Path, *, create: bool = True) -> None:
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = path.lstat()
    except OSError as exc:
        raise GuardError("required directory is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or _is_link_or_reparse(path, info):
        raise GuardError("required path is not a real directory")
    _check_owner(info)
    _validate_windows_path(path, directory=True)
    if os.name == "posix":
        flags = os.O_RDONLY | os.O_DIRECTORY
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            _check_owner(os.fstat(descriptor))
            os.fchmod(descriptor, 0o700)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
                raise GuardError("directory mode could not be secured")
        finally:
            os.close(descriptor)


def secure_file(path: Path, *, harden_mode: bool = True) -> None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise GuardError("configuration path is unavailable") from exc
    if not stat.S_ISREG(before.st_mode) or _is_link_or_reparse(path, before):
        raise GuardError("configuration path is not a real regular file")
    _check_owner(before)
    _validate_windows_path(path, directory=False)
    if os.name == "posix":
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            actual = os.fstat(descriptor)
            _check_owner(actual)
            if (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino):
                raise GuardError("configuration path changed during validation")
            if harden_mode:
                os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise GuardError("configuration write made no progress")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_if_identity(path: Path, expected: os.stat_result) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if _same_identity(current, expected):
        path.unlink()


def _publish_no_clobber(temp_path: Path, path: Path, expected: os.stat_result) -> None:
    try:
        current = temp_path.lstat()
    except OSError as exc:
        raise GuardError("configuration temporary file is unavailable") from exc
    if not _same_identity(current, expected):
        raise GuardError("configuration temporary file changed before publication")
    try:
        os.link(temp_path, path, follow_symlinks=False)
    except FileExistsError as exc:
        raise GuardError("configuration path appeared during publication") from exc
    published = path.lstat()
    if not _same_identity(published, expected):
        raise GuardError("configuration path changed during publication")
    try:
        _fsync_directory(path.parent)
    except OSError:
        message = "configuration is published; directory durability is unconfirmed"
        print(f"path safety warning: {message}", file=sys.stderr)

def write_exclusive(path: Path) -> None:
    secure_directory(path.parent, create=False)
    payload = sys.stdin.buffer.read(MAX_CONFIG_BYTES + 1)
    if len(payload) > MAX_CONFIG_BYTES:
        raise GuardError("configuration template is too large")
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.deepseek-mcp.", dir=path.parent
    )
    temp_path = Path(raw_temp)
    expected = os.fstat(descriptor)
    try:
        os.fchmod(descriptor, 0o600)
        _validate_windows_descriptor(descriptor, temp_path)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        expected = os.fstat(descriptor)
        _publish_no_clobber(temp_path, path, expected)
    finally:
        os.close(descriptor)
        _unlink_if_identity(temp_path, expected)

def _validate_private_root(root: Path) -> os.stat_result:
    try:
        info = root.lstat()
    except OSError as exc:
        raise GuardError("private directory root is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or _is_link_or_reparse(root, info):
        raise GuardError("private directory root is not a real directory")
    _check_owner(info)
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o022:
        raise GuardError("private directory root is writable by another user")
    _validate_windows_path(root, directory=True)
    return info

def _relative_parts(value: Path) -> tuple[str, ...]:
    if value.is_absolute() or not value.parts:
        raise GuardError("private directory path must be relative")
    parts = tuple(value.parts)
    if any(part in {"", ".", ".."} for part in parts):
        raise GuardError("private directory path contains an unsafe component")
    return parts

def _secure_posix_descendant(root: Path, parts: tuple[str, ...]) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, flags)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=descriptor)
            info = os.fstat(child)
            try:
                if not stat.S_ISDIR(info.st_mode):
                    raise GuardError("private path component is not a directory")
                _check_owner(info)
                if stat.S_IMODE(info.st_mode) & 0o022:
                    raise GuardError("private path component is writable by another user")
                os.fchmod(child, 0o700)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)

def _secure_windows_descendant(root: Path, parts: tuple[str, ...]) -> None:
    current = root
    for part in parts:
        current /= part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        secure_directory(current, create=False)

def prepare_private_directories(root: Path, descendants: list[Path]) -> None:
    _validate_private_root(root)
    for descendant in descendants:
        parts = _relative_parts(descendant)
        if os.name == "posix":
            _secure_posix_descendant(root, parts)
        else:
            _secure_windows_descendant(root, parts)


def _validate_posix_descendant(root: Path, parts: tuple[str, ...]) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, flags)
    try:
        for part in parts:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                return
            info = os.fstat(child)
            try:
                if not stat.S_ISDIR(info.st_mode):
                    raise GuardError("private path component is not a directory")
                _check_owner(info)
                if stat.S_IMODE(info.st_mode) & 0o022:
                    raise GuardError("private path component is writable by another user")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)

def _validate_windows_descendant(root: Path, parts: tuple[str, ...]) -> None:
    current = root
    for part in parts:
        current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            return
        secure_directory(current, create=False)

def validate_private_directories(root: Path, descendants: list[Path]) -> None:
    _validate_private_root(root)
    for descendant in descendants:
        parts = _relative_parts(descendant)
        if os.name == "posix":
            _validate_posix_descendant(root, parts)
        else:
            _validate_windows_descendant(root, parts)


def _validate_venv_tree(generation: Path) -> None:
    count = 0
    for current, directories, files in os.walk(
        generation, followlinks=False, onerror=_raise_walk_error
    ):
        for name in [*directories, *files]:
            count += 1
            if count > MAX_VENV_ENTRIES:
                raise GuardError("Python environment has too many entries")
            entry = Path(current) / name
            info = entry.lstat()
            _check_owner(info)
            if _is_windows() and _is_link_or_reparse(entry, info):
                raise GuardError("Python environment contains a reparse point")
            _validate_windows_path(entry, directory=stat.S_ISDIR(info.st_mode))
            if (
                not _is_windows()
                and not entry.is_symlink()
                and stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise GuardError("Python environment contains a writable entry")


def _raise_walk_error(error: OSError) -> None:
    raise GuardError("Python environment could not be inspected") from error


def _normalized_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _validate_generation_location(root: Path, generation: Path) -> tuple[Path, Path]:
    normalized_root = _normalized_path(root)
    normalized_generation = _normalized_path(generation)
    if normalized_generation.parent != normalized_root:
        raise GuardError("generation is not a direct child of its root")
    if GENERATION_NAME.fullmatch(normalized_generation.name) is None:
        raise GuardError("generation name is invalid")
    return normalized_root, normalized_generation


def _validate_generation_entry(path: Path, info: os.stat_result) -> None:
    if not stat.S_ISDIR(info.st_mode) or _is_link_or_reparse(path, info):
        raise GuardError("generation path is not a real directory")
    _check_owner(info)
    _validate_windows_path(path, directory=True)


def _generation_entries(root: Path) -> list[tuple[Path, os.stat_result]]:
    entries: list[tuple[Path, os.stat_result]] = []
    try:
        iterator = os.scandir(root)
    except OSError as exc:
        raise GuardError("generation root is unavailable") from exc
    with iterator:
        for entry in iterator:
            if not entry.name.startswith("generation."):
                continue
            if GENERATION_NAME.fullmatch(entry.name) is None:
                raise GuardError("generation name is invalid")
            if len(entries) >= MAX_GENERATIONS:
                raise GuardError("generation root has too many entries")
            path = root / entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise GuardError("generation entry is unavailable") from exc
            _validate_generation_entry(path, info)
            entries.append((path, info))
    return entries


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode)) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _revalidate_generation(path: Path, expected: os.stat_result) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise GuardError("generation changed during validation") from exc
    _validate_generation_entry(path, current)
    if not _same_identity(expected, current):
        raise GuardError("generation changed during validation")
    _validate_venv_tree(path)


def _remove_generations(entries: list[tuple[Path, os.stat_result]]) -> None:
    if os.name == "posix" and not shutil.rmtree.avoids_symlink_attacks:
        raise GuardError("safe recursive generation removal is unavailable")
    for path, expected in entries:
        _revalidate_generation(path, expected)
    for path, expected in entries:
        _revalidate_generation(path, expected)
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise GuardError("generation could not be removed") from exc


def delete_generation(root: Path, generation: Path) -> None:
    normalized_root, normalized_generation = _validate_generation_location(
        root, generation
    )
    secure_directory(normalized_root, create=False)
    matches = [
        entry for entry in _generation_entries(normalized_root)
        if entry[0] == normalized_generation
    ]
    if len(matches) != 1:
        raise GuardError("generation is unavailable")
    _remove_generations(matches)


def prune_generations(root: Path, current: Path) -> None:
    normalized_root, normalized_current = _validate_generation_location(root, current)
    secure_directory(normalized_root, create=False)
    entries = _generation_entries(normalized_root)
    if not any(path == normalized_current for path, _info in entries):
        raise GuardError("current generation is unavailable")
    previous = sorted(
        (entry for entry in entries if entry[0] != normalized_current),
        key=lambda entry: (entry[1].st_mtime_ns, entry[0].name),
        reverse=True,
    )
    keep = {normalized_current, *(path for path, _info in previous[:1])}
    removals = [entry for entry in entries if entry[0] not in keep]
    _remove_generations(removals)


def validate_venv_python(generation: Path, candidate: Path) -> None:
    secure_directory(generation, create=False)
    secure_directory(candidate.parent, create=False)
    try:
        candidate.relative_to(generation)
    except ValueError:
        raise GuardError("Python candidate is outside its generation") from None
    _validate_venv_tree(generation)
    try:
        target = candidate.resolve(strict=True)
        info = target.stat()
    except (OSError, RuntimeError):
        raise GuardError("Python candidate cannot be safely resolved") from None
    if not stat.S_ISREG(info.st_mode):
        raise GuardError("Python candidate target is not a regular file")
    if _is_windows():
        try:
            target.relative_to(generation.resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            raise GuardError("Python candidate target leaves its generation") from None
    _check_owner(info, trusted_root=True)
    _validate_windows_path(target, directory=False)
    writable_by_others = not _is_windows() and stat.S_IMODE(info.st_mode) & 0o022
    if writable_by_others or not os.access(target, os.X_OK):
        raise GuardError("Python candidate target is writable or not executable")


def _apply_all(function: Callable[[Path], None], values: list[Path]) -> None:
    for value in values:
        function(value)


_ACTIONS = {
    "prepare-dirs": (1, None, lambda v: _apply_all(secure_directory, v)),
    "secure-files": (1, None, lambda v: _apply_all(secure_file, v)),
    "validate-files": (1, None, lambda v: [secure_file(x, harden_mode=False) for x in v]),
    "write-exclusive": (1, 1, lambda v: write_exclusive(v[0])),
    "prepare-private-dirs": (2, None, lambda v: prepare_private_directories(v[0], v[1:])),
    "validate-private-dirs": (2, None, lambda v: validate_private_directories(v[0], v[1:])),
    "helper-current": (3, 3, lambda v: installer_asset_guard.compare_current(str(v[0]), v[1], v[2])),
    "helper-published": (2, 2, lambda v: installer_asset_guard.verify_published(str(v[0]), v[1])),
    "validate-venv": (2, 2, lambda v: validate_venv_python(v[0], v[1])),
    "delete-generation": (2, 2, lambda v: delete_generation(v[0], v[1])),
    "prune-generations": (2, 2, lambda v: prune_generations(v[0], v[1])),
}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in _ACTIONS:
        raise GuardError("missing or invalid guard action")
    minimum, maximum, handler = _ACTIONS[argv[1]]
    values = [Path(value) for value in argv[2:]]
    if len(values) < minimum or maximum is not None and len(values) != maximum:
        raise GuardError("invalid guard action arguments")
    handler(values)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except (GuardError, OSError, installer_asset_guard.AssetGuardError) as error:
        print(f"path safety check failed: {error}", file=sys.stderr)
        raise SystemExit(1)
