"""Resource-bounded glob walking without following workspace symlinks."""
from __future__ import annotations

import fnmatch
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Iterator

from .file_io import ToolInputError
from .safety import is_protected_host_path
from . import windows_walk
from . import walk_support
from .workspace_guard import require_workspace_identity, require_workspace_stat

MAX_WALK_ENTRIES = 10_000
MAX_WALK_DIRECTORIES = 1_000
MAX_WALK_DEPTH = 64
MAX_WALK_SECONDS = 2.0
MAX_GLOB_PATTERN_CHARS = 1_024
MAX_GLOB_PATTERN_SEGMENTS = 64
_HAS_SECURE_DIR_FDS = (
    os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.scandir in os.supports_fd
    and hasattr(os, "O_NOFOLLOW")
)

@dataclass(frozen=True)
class _GlobPattern:
    parts: tuple[str, ...]

    @classmethod
    def parse(cls, raw: str) -> _GlobPattern:
        if "\x00" in raw or len(raw) > MAX_GLOB_PATTERN_CHARS:
            raise ToolInputError("glob pattern exceeds the configured safety limit")
        normalized = raw.replace("\\", "/")
        if normalized.startswith("/") or _has_windows_drive(normalized):
            raise ToolInputError("glob pattern must stay within the workspace")
        parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
        if not parts or ".." in parts:
            raise ToolInputError("glob pattern must stay within the workspace")
        if os.name == "nt" and not all(windows_walk.safe_part(part) for part in parts):
            raise ToolInputError("glob pattern contains an unsafe Windows component")
        if len(parts) > MAX_GLOB_PATTERN_SEGMENTS:
            raise ToolInputError("glob pattern exceeds the configured safety limit")
        return cls(parts)

    def initial_states(self) -> frozenset[int]:
        return self._closure(frozenset({0}))

    def advance(self, states: frozenset[int], name: str) -> frozenset[int]:
        advanced: set[int] = set()
        for index in states:
            if index >= len(self.parts):
                continue
            part = self.parts[index]
            if part == "**" and not name.startswith("."):
                advanced.add(index)
            elif part != "**" and _segment_matches(name, part):
                advanced.add(index + 1)
        return self._closure(frozenset(advanced))

    def is_match(self, states: frozenset[int]) -> bool:
        return len(self.parts) in states

    def can_descend(self, states: frozenset[int]) -> bool:
        return any(index < len(self.parts) for index in states)

    def _closure(self, states: frozenset[int]) -> frozenset[int]:
        expanded = set(states)
        for index in range(len(self.parts)):
            if index in expanded and self.parts[index] == "**":
                expanded.add(index + 1)
        return frozenset(expanded)

@dataclass(frozen=True)
class WalkEntry:
    path: Path
    relative_to_workspace: Path
    is_file: bool
    is_dir: bool
    _descriptor: int | None = None

    def read_bytes(self, max_bytes: int) -> bytes:
        if not self.is_file or self._descriptor is None:
            raise ToolInputError("workspace entry is not an open regular file")
        return walk_support.read_open_entry(self._descriptor, max_bytes)

@dataclass
class _Frame:
    descriptor: int | None
    iterator: Iterator[os.DirEntry[str]]
    display_path: Path
    identity: os.stat_result | None
    guard_handle: int | None
    relative_parts: tuple[str, ...]
    states: frozenset[int]
    depth: int

class WorkspaceWalk:
    """Context-managed iterator so early result limits still close descriptors."""

    def __init__(
        self, base: str, workspace: Path, raw_pattern: str, *, open_files: bool = False
    ) -> None:
        self.root, self.base, relative = _resolve_base(base, workspace)
        self.pattern = _GlobPattern.parse(raw_pattern)
        self.truncated = False
        self._deadline = time.monotonic() + MAX_WALK_SECONDS
        self._entries = 0
        self._directories = 0
        self._open_files = open_files
        self._iterator = self._walk(relative)

    def __enter__(self) -> WorkspaceWalk:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __iter__(self) -> WorkspaceWalk:
        return self

    def __next__(self) -> WalkEntry:
        return next(self._iterator)

    def close(self) -> None:
        self._iterator.close()

    def _walk(self, base_relative: Path) -> Iterator[WalkEntry]:
        stack: list[_Frame] = []
        try:
            base_descriptor, base_identity = _open_base(self.root, base_relative)
            try:
                root_frame = self._new_frame(
                    base_descriptor, self.base, (), 0, self.pattern.initial_states(), base_identity,
                )
            except OSError as exc:
                raise ToolInputError("path identity changed before traversal") from exc
            stack.append(root_frame)
            while stack and not self._budget_exhausted():
                entry = _next_entry(stack)
                if entry is None:
                    continue
                self._entries += 1
                parent = stack[-1]
                yielded, child = self._inspect_entry(parent, entry)
                if child is not None:
                    stack.append(child)
                if yielded is not None:
                    yield from walk_support.yield_entry(yielded)
        finally:
            walk_support.close_frames(stack)

    def _inspect_entry(
        self, parent: _Frame, entry: os.DirEntry[str]
    ) -> tuple[WalkEntry | None, _Frame | None]:
        if os.name == "nt" and not windows_walk.safe_part(entry.name):
            return None, None
        info = _entry_stat(parent, entry.name)
        if info is None or stat.S_ISLNK(info.st_mode):
            return None, None
        states = self.pattern.advance(parent.states, entry.name)
        if not states:
            return None, None
        parts = (*parent.relative_parts, entry.name)
        path = parent.display_path / entry.name
        if is_protected_host_path(path):
            return None, None
        is_file, is_dir = stat.S_ISREG(info.st_mode), stat.S_ISDIR(info.st_mode)
        matched = self._matched_entry(path, parts, states, is_file, is_dir, info, parent)
        child = self._child_frame(path, parts, states, info, parent) if is_dir else None
        return matched, child

    def _matched_entry(
        self,
        path: Path,
        parts: tuple[str, ...],
        states: frozenset[int],
        is_file: bool,
        is_dir: bool,
        info: os.stat_result,
        parent: _Frame,
    ) -> WalkEntry | None:
        if not self.pattern.is_match(states):
            return None
        needs_descriptor = is_file and self._open_files
        descriptor = _open_regular(parent, path, parts[-1], info) if needs_descriptor else None
        if needs_descriptor and descriptor is None:
            return None
        relative = path.relative_to(self.root)
        return WalkEntry(path, relative, is_file, is_dir, descriptor)

    def _child_frame(
        self,
        path: Path,
        parts: tuple[str, ...],
        states: frozenset[int],
        info: os.stat_result,
        parent: _Frame,
    ) -> _Frame | None:
        if not self.pattern.can_descend(states):
            return None
        if parent.depth >= MAX_WALK_DEPTH or self._directories >= MAX_WALK_DIRECTORIES:
            self.truncated = True
            return None
        descriptor = _open_child(parent, path, parts[-1], info)
        if parent.descriptor is not None and descriptor is None:
            return None
        try:
            return self._new_frame(
                descriptor, path, parts, parent.depth + 1, states, info, parent
            )
        except OSError as error:
            return walk_support.skip_race_or_raise(error)

    def _new_frame(
        self,
        descriptor: int | None,
        path: Path,
        parts: tuple[str, ...],
        depth: int,
        states: frozenset[int],
        expected: os.stat_result | None = None,
        parent: _Frame | None = None,
    ) -> _Frame:
        try:
            if descriptor is None:
                iterator, identity, guard = _open_fallback_iterator(
                    path, expected, parent
                )
            else:
                iterator, identity, guard = os.scandir(descriptor), None, None
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            raise
        self._directories += 1
        return _Frame(
            descriptor, iterator, path, identity, guard, parts, states, depth
        )

    def _budget_exhausted(self) -> bool:
        if self._entries >= MAX_WALK_ENTRIES or time.monotonic() >= self._deadline:
            self.truncated = True
            return True
        return False

def _has_windows_drive(pattern: str) -> bool:
    return len(pattern) >= 2 and pattern[0].isalpha() and pattern[1] == ":"

def _segment_matches(name: str, pattern: str) -> bool:
    if name.startswith(".") and not pattern.startswith("."):
        return False
    return fnmatch.fnmatchcase(name, pattern)

def _resolve_base(base: str, workspace: Path) -> tuple[Path, Path, Path]:
    if not isinstance(base, str):
        raise ToolInputError("'path' must be a string")
    if "\x00" in base:
        raise ToolInputError("null byte in path is not allowed")
    if os.name == "nt" and not windows_walk.safe_path_text(base):
        raise ToolInputError("path contains an unsafe Windows component")
    try:
        require_workspace_identity(workspace)
        root = workspace.resolve()
        requested = Path(base).expanduser() if base else root
        candidate = requested if requested.is_absolute() else root / requested
        candidate = Path(os.path.abspath(candidate))
    except (OSError, RuntimeError, ValueError):
        raise ToolInputError("path cannot be safely resolved") from None
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ToolInputError("path must stay within the workspace") from exc
    if os.name == "nt" and not all(windows_walk.safe_part(part) for part in relative.parts):
        raise ToolInputError("path contains an unsafe Windows component")
    if is_protected_host_path(candidate):
        raise ToolInputError("path targets protected host or VCS control state")
    return root, candidate, relative

def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )

def _open_base(root: Path, relative: Path) -> tuple[int | None, os.stat_result | None]:
    if not _HAS_SECURE_DIR_FDS:
        return None, _validate_base_path(root, relative)
    try:
        descriptor = os.open(root, _directory_flags())
        require_workspace_stat(os.fstat(descriptor))
        for part in relative.parts:
            next_descriptor = os.open(part, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("not a directory")
        return descriptor, None
    except OSError as exc:
        if "descriptor" in locals():
            os.close(descriptor)
        raise ToolInputError("path is not an accessible real directory") from exc

def _entry_stat(parent: _Frame, name: str) -> os.stat_result | None:
    try:
        if parent.descriptor is not None:
            return os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
        before = _frame_path_info(parent)
        if before is None:
            return None
        info = _real_path_info(parent.display_path / name)
        after = _frame_path_info(parent)
        if after is None or not _same_file(before, after):
            return None
        return info
    except OSError as error:
        return walk_support.skip_race_or_raise(error)

def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    if left.st_ino and right.st_ino:
        return left.st_dev == right.st_dev and left.st_ino == right.st_ino
    left_fallback = (left.st_mode, left.st_size, left.st_mtime_ns, left.st_ctime_ns)
    right_fallback = (right.st_mode, right.st_size, right.st_mtime_ns, right.st_ctime_ns)
    return left_fallback == right_fallback

def _open_child(
    parent: _Frame, path: Path, name: str, expected: os.stat_result
) -> int | None:
    if parent.descriptor is None:
        return None
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent.descriptor)
        actual = os.fstat(descriptor)
        if not stat.S_ISDIR(actual.st_mode) or not _same_file(expected, actual):
            os.close(descriptor)
            return None
        return descriptor
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        return walk_support.skip_race_or_raise(error)

def _frame_path_info(frame: _Frame) -> os.stat_result | None:
    if frame.guard_handle is not None and not windows_walk.handle_matches(
        frame.guard_handle, frame.display_path, directory=True
    ):
        return None
    info = _real_path_info(frame.display_path)
    if info is None or frame.identity is None or not stat.S_ISDIR(info.st_mode):
        return None
    return info if _same_file(frame.identity, info) else None

def _fallback_identity_changed(
    before: os.stat_result,
    after: os.stat_result | None,
    parent: _Frame | None,
    parent_before: os.stat_result | None,
) -> bool:
    if after is None or not _same_file(before, after):
        return True
    if parent is None:
        return False
    parent_after = _frame_path_info(parent)
    return (
        parent_before is None
        or parent_after is None
        or not _same_file(parent_before, parent_after)
    )

def _open_fallback_iterator(
    path: Path, expected: os.stat_result | None, parent: _Frame | None
) -> tuple[Iterator[os.DirEntry[str]], os.stat_result, int | None]:
    parent_before = _frame_path_info(parent) if parent is not None else None
    if parent is not None and parent_before is None:
        raise OSError("parent directory identity changed")
    before = _real_path_info(path)
    if before is None or not stat.S_ISDIR(before.st_mode):
        raise OSError("directory is not real")
    if expected is not None and not _same_file(expected, before):
        raise OSError("directory identity changed")
    guard = windows_walk.open_guard(path, directory=True) if os.name == "nt" else None
    try:
        iterator = os.scandir(path)
    except BaseException:
        windows_walk.close_guard(guard)
        raise
    if _fallback_identity_changed(
        before, _real_path_info(path), parent, parent_before
    ):
        iterator.close()
        windows_walk.close_guard(guard)
        raise OSError("directory identity changed")
    return iterator, before, guard

def _open_regular(
    parent: _Frame, path: Path, name: str, expected: os.stat_result
) -> int | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    if parent.descriptor is None:
        return _open_fallback_regular(parent, path, expected, flags)
    return _open_regular_at(parent.descriptor, name, expected, flags)

def _open_regular_at(
    parent: int, name: str, expected: os.stat_result, flags: int
) -> int | None:
    descriptor: int | None = None
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
        actual = os.fstat(descriptor)
        if not stat.S_ISREG(actual.st_mode) or not _same_file(expected, actual):
            os.close(descriptor)
            return None
        return descriptor
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        return walk_support.skip_race_or_raise(error)

def _open_fallback_regular(
    parent: _Frame, path: Path, expected: os.stat_result, flags: int
) -> int | None:
    descriptor: int | None = None
    try:
        parent_before = _frame_path_info(parent)
        if parent_before is None:
            return None
        before = _real_path_info(path)
        if before is None or not _same_file(expected, before):
            return None
        descriptor = os.open(path, flags)
        actual, after = os.fstat(descriptor), _real_path_info(path)
        parent_after = _frame_path_info(parent)
        valid = stat.S_ISREG(actual.st_mode) and _same_file(expected, actual)
        valid &= after is not None and _same_file(expected, after)
        valid &= parent_after is not None and _same_file(parent_before, parent_after)
        if os.name == "nt":
            valid &= windows_walk.descriptor_matches(descriptor, path)
        if not valid:
            os.close(descriptor)
            return None
        return descriptor
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        return walk_support.skip_race_or_raise(error)

def _next_entry(stack: list[_Frame]) -> os.DirEntry[str] | None:
    return walk_support.next_entry(stack)

def _real_path_info(path: Path) -> os.stat_result | None:
    try:
        info = os.lstat(path)
    except OSError as error:
        return walk_support.skip_race_or_raise(error)
    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    if (
        stat.S_ISLNK(info.st_mode)
        or is_junction(path)
        or bool(file_attributes & reparse_flag)
    ):
        return None
    return info

def _validate_base_path(root: Path, relative: Path) -> os.stat_result:
    require_workspace_identity(root)
    chain: list[tuple[Path, os.stat_result]] = []
    current = root
    for part in ("", *relative.parts):
        current = current / part if part else current
        info = _real_path_info(current)
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise ToolInputError("path is not an accessible real directory")
        chain.append((current, info))
    for path, expected in chain:
        actual = _real_path_info(path)
        if actual is None or not _same_file(expected, actual):
            raise ToolInputError("path identity changed during validation")
    return chain[-1][1]
