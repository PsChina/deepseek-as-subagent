"""Runtime checks for immutable task-contract policies."""
from __future__ import annotations

from functools import lru_cache
from fnmatch import fnmatchcase
from pathlib import Path

from .policy_engine import EffectivePolicy
from .safety import SandboxViolation, resolve_safe_path

_READ_TOOLS = frozenset({"Read", "Glob", "Grep"})
_WRITE_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})


@lru_cache(maxsize=8192)
def path_matches_pattern(relative_path: str, pattern: str) -> bool:
    path_parts = tuple(part for part in relative_path.split("/") if part)
    pattern_parts = tuple(pattern.split("/"))

    @lru_cache(maxsize=None)
    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        segment = pattern_parts[pattern_index]
        if segment == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatchcase(path_parts[path_index], segment)
            and match(path_index + 1, pattern_index + 1)
        )
    return match(0, 0)


def path_is_denied(relative_path: str, deny_paths: tuple[str, ...]) -> bool:
    return any(path_matches_pattern(relative_path, pattern) for pattern in deny_paths)


def path_is_allowed(relative_path: str, allowed_paths: tuple[str, ...], deny_paths: tuple[str, ...]) -> bool:
    return not path_is_denied(relative_path, deny_paths) and any(
        path_matches_pattern(relative_path, pattern) for pattern in allowed_paths
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
        return _check_scoped_path(arguments.get("path"), workspace, policy.write_paths, policy.deny_paths, "write")
    if name == "Bash":
        return "ERROR: Bash is disabled for task-contract executions"
    if name in _READ_TOOLS:
        return None
    return f"ERROR: tool '{name}' has no task-contract policy"
