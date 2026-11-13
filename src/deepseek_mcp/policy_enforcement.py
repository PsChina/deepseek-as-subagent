"""Runtime checks for immutable task-contract policies."""
from __future__ import annotations

import os
from functools import lru_cache
from fnmatch import fnmatchcase
from pathlib import Path

from .policy_engine import EffectivePolicy
from .safety import SandboxViolation, resolve_safe_path

_READ_TOOLS = frozenset({"Read", "Glob", "Grep"})
_WRITE_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})


@lru_cache(maxsize=8192)
def path_matches_pattern(relative_path: str, pattern: str, case_sensitive: bool = True) -> bool:
    path_parts = tuple(part for part in relative_path.split("/") if part)
    pattern_parts = tuple(pattern.split("/"))
    if not case_sensitive:
        path_parts = tuple(part.casefold() for part in path_parts)
        pattern_parts = tuple(part.casefold() for part in pattern_parts)
    previous = [False] * (len(path_parts) + 1)
    previous[0] = True
    for segment in pattern_parts:
        current = [False] * (len(path_parts) + 1)
        if segment == "**":
            current[0] = previous[0]
            for index in range(1, len(path_parts) + 1):
                current[index] = previous[index] or current[index - 1]
        else:
            for index in range(1, len(path_parts) + 1):
                current[index] = previous[index - 1] and fnmatchcase(path_parts[index - 1], segment)
        previous = current
    return previous[-1]


def path_is_denied(relative_path: str, deny_paths: tuple[str, ...]) -> bool:
    return any(path_matches_pattern(relative_path, pattern, case_sensitive=False) for pattern in deny_paths)


def _is_normalized_relative_path(relative_path: str) -> bool:
    return bool(relative_path) and not relative_path.startswith(("/", "\\")) and "\\" not in relative_path and not (len(relative_path) > 1 and relative_path[0].isalpha() and relative_path[1] == ":") and all(part not in {"", ".", ".."} for part in relative_path.split("/"))


def path_is_allowed(relative_path: str, allowed_paths: tuple[str, ...], deny_paths: tuple[str, ...]) -> bool:
    if not _is_normalized_relative_path(relative_path): return False
    return not path_is_denied(relative_path, deny_paths) and any(
        path_matches_pattern(relative_path, pattern, case_sensitive=os.name != "nt")
        for pattern in allowed_paths
    )


def _check_scoped_path(label: object, workspace: Path, allowed: tuple[str, ...], denied: tuple[str, ...], operation: str) -> str | None:
    if not isinstance(label, str) or not label:
        return f"ERROR: {operation} requires a non-empty path"
    try:
        resolved = resolve_safe_path(label, workspace)
        relative = resolved.relative_to(workspace.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError, SandboxViolation) as error:
        return f"ERROR: {error}"
    if not path_is_allowed(relative, allowed, denied):
        return f"ERROR: path is outside the task contract {operation} scope: {label}"
    return None


def check_tool_access(policy: EffectivePolicy, name: str, arguments: dict, workspace: Path) -> str | None:
    if name not in policy.allowed_tools:
        return f"ERROR: tool '{name}' is not allowed by the task contract"
    if name == "Read":
        return _check_scoped_path(arguments.get("path"), workspace, policy.read_paths, policy.deny_paths, "read")
    if name in _WRITE_TOOLS:
        if name in {"Edit", "NotebookEdit"}:
            denied = _check_scoped_path(
                arguments.get("path"), workspace, policy.read_paths,
                policy.deny_paths, "read",
            )
            if denied:
                return denied
        return _check_scoped_path(arguments.get("path"), workspace, policy.write_paths, policy.deny_paths, "write")
    if name == "Bash":
        return "ERROR: Bash is disabled for task-contract executions"
    if name in _READ_TOOLS:
        return None
    return f"ERROR: tool '{name}' has no task-contract policy"
