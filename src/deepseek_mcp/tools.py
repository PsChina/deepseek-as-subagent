"""Bounded, workspace-confined tools used by the DeepSeek agent loop."""
from __future__ import annotations
import json
import uuid
from pathlib import Path
from .bash_tool import execute_bash
from .config import Config
from .file_io import (
    MAX_TEXT_FILE_BYTES,
    MISSING_FILE,
    FileIdentity,
    MissingFile,
    MutationCommittedWarning,
    ToolInputError,
    WorkspaceFileNotFound,
    atomic_write_workspace_text as _atomic_write_workspace_text,
    read_workspace_text as _read_workspace_text,
)
from .file_identity import bounded_integer
from .safe_regex import RegexPattern, SafeRegexError, compile_safe_regex
from .resource_budget import MutationBudget, ResourceBudgetExceeded, apply_mutation
from .safety import SandboxViolation
from .tool_schemas import build_tool_schemas
from .transaction_report import mutation_warning
from .walk_support import WorkspaceEntryTooLarge
from .workspace_walk import WalkEntry, WorkspaceWalk
MAX_TOOL_OUTPUT = 50_000  # 单次工具结果最大字符数
MAX_WRITE_BYTES = 5_000_000  # 单次 Write 最大字节数（5MB，防 DeepSeek 写爆磁盘）
MAX_GLOB_RESULTS = 500
MAX_GREP_FILES = 10_000
MAX_GREP_LINE_CHARS = 2_000
def _truncate(text: str) -> str:
    if len(text) > MAX_TOOL_OUTPUT:
        return (
            text[:MAX_TOOL_OUTPUT]
            + f"\n... [truncated, total {len(text)} chars, showing first {MAX_TOOL_OUTPUT}]"
        )
    return text

def _committed_result(result: str, warning: Exception) -> str:
    mutation_warning(str(warning))
    return f"{result}; WARNING: update committed but post-commit checks failed ({warning}); DO NOT RETRY."

def _utf8_size(text: str) -> int:
    if len(text) > MAX_WRITE_BYTES:
        return MAX_WRITE_BYTES + 1
    return len(text.encode("utf-8", errors="replace"))

def _slice_lines(text: str, args: dict) -> str:
    if "offset" not in args and "limit" not in args:
        return text
    offset = bounded_integer(args.get("offset", 0), "offset", minimum=0)
    raw_limit = args.get("limit")
    limit = None if raw_limit is None else bounded_integer(
        raw_limit, "limit", minimum=0
    )
    lines = text.splitlines()
    end = offset + limit if limit is not None else len(lines)
    return "\n".join(lines[offset:end])

def _execute_read(args: dict, workspace: Path) -> str:
    """读文件。args: {path: str, offset?: int, limit?: int}"""
    path = args.get("path", "")
    if not isinstance(path, str) or not path:
        return "ERROR: missing required 'path' argument"
    try:
        text, _identity = _read_workspace_text(
            workspace, path, reject_binary=True
        )
        text = _slice_lines(text, args)
    except WorkspaceFileNotFound:
        return f"ERROR: file not found: {path}"
    except (OSError, SandboxViolation, ToolInputError) as e:
        return f"ERROR: failed to read {path}: {e}"
    return _truncate(text)

def _execute_write(
    args: dict, workspace: Path, mutation_budget: MutationBudget | None = None
) -> str:
    """写文件（覆盖）。args: {path: str, content: str}"""
    path = args.get("path", "")
    content = args.get("content", "")
    if not isinstance(path, str) or not path:
        return "ERROR: missing required 'path' argument"
    if not isinstance(content, str):
        return "ERROR: 'content' must be a string"
    if _utf8_size(content) > MAX_WRITE_BYTES:
        return f"ERROR: content exceeds {MAX_WRITE_BYTES} bytes; split into smaller writes."
    try:
        apply_mutation(
            mutation_budget, _utf8_size(content),
            lambda: _atomic_write_workspace_text(
                workspace, path, content, expected=MISSING_FILE
            ),
        )
    except ResourceBudgetExceeded:
        raise
    except MutationCommittedWarning as exc:
        return _committed_result(f"OK: wrote {len(content)} chars to {path}", exc)
    except ToolInputError as e:
        if "appeared during edit" in str(e):
            return f"ERROR: file already exists: {path}; use Edit for existing files"
        return f"ERROR: failed to write {path}: {e}"
    except Exception as e:
        return f"ERROR: failed to write {path}: {e}"
    return f"OK: wrote {len(content)} chars to {path}"

def _parse_edit_request(args: dict) -> tuple[str, str, str, bool]:
    path = args.get("path", "")
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    replace_all = args.get("replace_all", False)
    if not isinstance(path, str) or not path:
        raise ToolInputError("missing required 'path' argument")
    if not isinstance(old, str) or old == "":
        raise ToolInputError("missing required 'path' or 'old_string'")
    if not isinstance(new, str):
        raise ToolInputError("'old_string' and 'new_string' must be strings")
    if not isinstance(replace_all, bool):
        raise ToolInputError("'replace_all' must be a boolean")
    return path, old, new, replace_all

def _read_edit_target(path: str, workspace: Path) -> tuple[str, FileIdentity]:
    try:
        return _read_workspace_text(
            workspace, path, strict_utf8=True, reject_binary=True
        )
    except WorkspaceFileNotFound:
        raise ToolInputError(f"file not found: {path}") from None
    except (OSError, ToolInputError) as exc:
        raise ToolInputError(f"failed to read {path}: {exc}") from exc

def _build_replacement(
    text: str, old: str, new: str, replace_all: bool, path: str
) -> tuple[str, int]:
    count = text.count(old)
    if count == 0:
        raise ToolInputError(f"old_string not found in {path}")
    if count > 1 and not replace_all:
        raise ToolInputError(
            f"old_string appears {count} times in {path}. "
            f"Use replace_all=true or provide more context to make it unique."
        )
    replacements = count if replace_all else 1
    output_size = _utf8_size(text) + replacements * (
        _utf8_size(new) - _utf8_size(old)
    )
    if output_size > MAX_WRITE_BYTES:
        raise ToolInputError(f"edited content exceeds {MAX_WRITE_BYTES} bytes")
    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    return new_text, replacements

def _execute_edit(
    args: dict, workspace: Path, mutation_budget: MutationBudget | None = None
) -> str:
    """精确字符串替换。args: {path: str, old_string: str, new_string: str, replace_all?: bool}"""
    try:
        path, old, new, replace_all = _parse_edit_request(args)
        text, identity = _read_edit_target(path, workspace)
        new_text, replacements = _build_replacement(
            text, old, new, replace_all, path
        )
    except (SandboxViolation, ToolInputError) as exc:
        return f"ERROR: {exc}"
    try:
        apply_mutation(
            mutation_budget, _utf8_size(new_text),
            lambda: _atomic_write_workspace_text(
                workspace, path, new_text, expected=identity
            ),
        )
    except ResourceBudgetExceeded:
        raise
    except MutationCommittedWarning as exc:
        return _committed_result(
            f"OK: replaced {replacements} occurrence(s) in {path}", exc
        )
    except Exception as e:
        return f"ERROR: failed to write {path}: {e}"
    return f"OK: replaced {replacements} occurrence(s) in {path}"

def _parse_glob_request(args: dict, workspace: Path) -> WorkspaceWalk:
    pattern = args.get("pattern", "")
    if not isinstance(pattern, str) or not pattern:
        raise ToolInputError("missing required 'pattern' argument")
    base = args.get("path", "")
    if not isinstance(base, str):
        raise ToolInputError("'path' must be a string")
    return WorkspaceWalk(base, workspace, pattern)

def _scan_glob_matches(walk: WorkspaceWalk) -> tuple[list[Path], bool]:
    matches: list[Path] = []
    for entry in walk:
        if len(matches) >= MAX_GLOB_RESULTS:
            return matches, True
        matches.append(entry.path)
    return matches, False

def _format_glob_result(
    matches: list[Path], result_limit: bool, traversal_limit: bool, root: Path
) -> str:
    matches.sort()
    rel_matches = [str(path.relative_to(root)) for path in matches]
    truncated = result_limit or traversal_limit
    prefix = "Found at least" if truncated else "Found"
    summary = f"{prefix} {len(matches)} match(es)"
    if result_limit:
        summary += f" (limit {MAX_GLOB_RESULTS} reached)"
    elif traversal_limit:
        summary += " (configured traversal limit reached)"
    return _truncate(summary + ":\n" + "\n".join(rel_matches))


def _execute_glob(args: dict, workspace: Path) -> str:
    """文件名 pattern 匹配。args: {pattern: str, path?: str}"""
    try:
        with _parse_glob_request(args, workspace) as walk:
            matches, result_limit = _scan_glob_matches(walk)
            traversal_limit = walk.truncated
    except (SandboxViolation, ToolInputError) as exc:
        return f"ERROR: {exc}"
    return _format_glob_result(
        matches, result_limit, traversal_limit, workspace.resolve()
    )


def _new_notebook() -> dict:
    return {
        "cells": [],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _load_notebook(
    workspace: Path, label: str, edit_mode: str
) -> tuple[dict, FileIdentity | MissingFile]:
    try:
        text, identity = _read_workspace_text(
            workspace, label, strict_utf8=True
        )
    except WorkspaceFileNotFound:
        if edit_mode == "insert":
            return _new_notebook(), MISSING_FILE
        raise ToolInputError(f"notebook not found: {label}")
    try:
        notebook = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolInputError(f"failed to parse notebook JSON: {exc}") from exc
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        raise ToolInputError("not a valid notebook (missing 'cells' array)")
    return notebook, identity


def _find_cell_index(cells: list, args: dict, edit_mode: str, label: str) -> int | None:
    cell_id = args.get("cell_id")
    if cell_id is not None:
        for index, cell in enumerate(cells):
            if isinstance(cell, dict) and cell.get("id") == cell_id:
                return index
        if edit_mode != "insert":
            raise ToolInputError(f"cell_id '{cell_id}' not found in {label}")
        return None
    raw_index = args.get("cell_index")
    if raw_index is not None:
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            raise ToolInputError("cell_index must be an integer")
        index = raw_index
        if 0 <= index < len(cells):
            return index
        if edit_mode != "insert":
            raise ToolInputError(
                f"cell_index {index} out of range (0..{len(cells) - 1})"
            )
        return None
    if edit_mode != "insert":
        raise ToolInputError("replace/delete require cell_id or cell_index")
    return None


def _split_source(source: str) -> list[str]:
    if not source:
        return [""]
    lines = source.splitlines(keepends=True)
    return lines if lines else [""]


def _replace_cell(cells: list, index: int | None, source: str) -> str:
    if index is None or not isinstance(cells[index], dict):
        raise ToolInputError("target notebook cell is invalid")
    cell = cells[index]
    cell["source"] = _split_source(source)
    if cell.get("cell_type") == "code":
        cell["outputs"] = []
        cell["execution_count"] = None
    return f"OK: replaced cell at index {index} (id={cell.get('id', 'n/a')})"


def _insert_cell(cells: list, index: int | None, source: str, cell_type: str) -> str:
    if cell_type not in ("code", "markdown"):
        raise ToolInputError(f"invalid cell_type '{cell_type}' (must be code or markdown)")
    cell: dict = {
        "cell_type": cell_type,
        "id": uuid.uuid4().hex[:8],
        "source": _split_source(source),
        "metadata": {},
    }
    if cell_type == "code":
        cell.update({"outputs": [], "execution_count": None})
    insert_at = index + 1 if index is not None else len(cells)
    cells.insert(insert_at, cell)
    return f"OK: inserted {cell_type} cell at index {insert_at} (id={cell['id']})"


def _apply_notebook_edit(cells: list, args: dict, mode: str, index: int | None) -> str:
    source = args.get("new_source", "")
    if mode in ("replace", "insert") and not isinstance(source, str):
        raise ToolInputError("'new_source' must be a string")
    if isinstance(source, str) and _utf8_size(source) > MAX_WRITE_BYTES:
        raise ToolInputError(f"new_source exceeds {MAX_WRITE_BYTES} bytes")
    if mode == "replace":
        return _replace_cell(cells, index, source)
    if mode == "insert":
        return _insert_cell(cells, index, source, args.get("cell_type", "code"))
    if index is None or not isinstance(cells[index], dict):
        raise ToolInputError("target notebook cell is invalid")
    removed = cells.pop(index)
    return f"OK: deleted cell at index {index} (was id={removed.get('id', 'n/a')})"


def _save_notebook(
    workspace: Path,
    label: str,
    notebook: dict,
    expected: FileIdentity | MissingFile,
    mutation_budget: MutationBudget | None = None,
) -> None:
    content = json.dumps(notebook, ensure_ascii=False, indent=1) + "\n"
    if _utf8_size(content) > MAX_WRITE_BYTES:
        raise ToolInputError(f"notebook exceeds {MAX_WRITE_BYTES} bytes after edit")
    apply_mutation(
        mutation_budget,
        _utf8_size(content),
        lambda: _atomic_write_workspace_text(
            workspace, label, content, expected=expected
        ),
    )


def _execute_notebook_edit(
    args: dict, workspace: Path, mutation_budget: MutationBudget | None = None
) -> str:
    path = args.get("path", "")
    if not isinstance(path, str) or not path:
        return "ERROR: missing required 'path' argument"
    if not path.endswith(".ipynb"):
        return f"ERROR: not an .ipynb file: {path}"
    edit_mode = args.get("edit_mode", "replace")
    if edit_mode not in ("replace", "insert", "delete"):
        return f"ERROR: invalid edit_mode '{edit_mode}' (must be replace/insert/delete)"
    try:
        notebook, identity = _load_notebook(workspace, path, edit_mode)
        cells = notebook["cells"]
        index = _find_cell_index(cells, args, edit_mode, path)
        result = _apply_notebook_edit(cells, args, edit_mode, index)
        _save_notebook(workspace, path, notebook, identity, mutation_budget)
    except ResourceBudgetExceeded:
        raise
    except MutationCommittedWarning as exc:
        return _committed_result(result + f" (total cells: {len(cells)})", exc)
    except (SandboxViolation, ToolInputError, OSError) as exc:
        return f"ERROR: {exc}"
    return result + f" (total cells: {len(cells)})"


def _grep_file(entry: WalkEntry, regex: RegexPattern, limit: int) -> list[str]:
    data = entry.read_bytes(MAX_TEXT_FILE_BYTES)
    if b"\x00" in data[:8192]:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    matches: list[str] = []
    for line_number, line in enumerate(lines, 1):
        if not regex.search(line):
            continue
        displayed = line[:MAX_GREP_LINE_CHARS]
        if len(line) > MAX_GREP_LINE_CHARS:
            displayed += "... [line truncated]"
        matches.append(f"{entry.relative_to_workspace}:{line_number}: {displayed}")
        if len(matches) >= limit:
            break
    return matches

def _parse_grep_request(
    args: dict, workspace: Path
) -> tuple[WorkspaceWalk, RegexPattern, int]:
    pattern = args.get("pattern", "")
    file_glob = args.get("glob", "**/*")
    if not isinstance(pattern, str) or not pattern:
        raise ToolInputError("missing required 'pattern' argument")
    if not isinstance(file_glob, str) or not file_glob:
        raise ToolInputError("'glob' must be a non-empty string")
    base = args.get("path", "")
    if not isinstance(base, str):
        raise ToolInputError("'path' must be a string")
    limit = bounded_integer(
        args.get("max_matches", 100), "max_matches", minimum=1, maximum=1000
    )
    walk = WorkspaceWalk(base, workspace, file_glob, open_files=True)
    return walk, compile_safe_regex(pattern), limit


def _scan_grep(
    walk: WorkspaceWalk, regex: RegexPattern, limit: int
) -> tuple[list[str], bool]:
    results: list[str] = []
    scanned = 0
    incomplete = False
    for entry in walk:
        if not entry.is_file:
            continue
        scanned += 1
        if scanned > MAX_GREP_FILES:
            return results, True
        try:
            results.extend(_grep_file(entry, regex, limit - len(results)))
        except WorkspaceEntryTooLarge:
            incomplete = True
        if len(results) >= limit:
            return results, True
    return results, incomplete


def _format_grep_result(results: list[str], pattern: str, truncated: bool) -> str:
    if not results:
        if truncated:
            return f"Search results incomplete for pattern: {pattern}"
        return f"No matches found for pattern: {pattern}"
    header = f"Found {len(results)} match(es)"
    if truncated:
        header += " (results incomplete)"
    return _truncate(header + ":\n" + "\n".join(results))

def _execute_grep(args: dict, workspace: Path) -> str:
    """正则搜索文件内容。args: {pattern: str, path?: str, glob?: str, max_matches?: int}"""
    try:
        walk, regex, limit = _parse_grep_request(args, workspace)
        with walk:
            results, match_limit = _scan_grep(walk, regex, limit)
            traversal_limit = walk.truncated
    except (SandboxViolation, ToolInputError, SafeRegexError) as exc:
        return f"ERROR: {exc}"
    return _format_grep_result(
        results, regex.pattern, match_limit or traversal_limit
    )
TOOL_REGISTRY = {
    "Read": _execute_read,
    "Write": _execute_write,
    "Edit": _execute_edit,
    "Glob": _execute_glob,
    "Grep": _execute_grep,
    "NotebookEdit": _execute_notebook_edit,
}


def execute_tool(
    name: str,
    args: dict,
    config: Config,
    *,
    execution_lease_fd: int | None = None,
    mutation_budget: MutationBudget | None = None,
    max_bash_timeout: int | None = None,
) -> str:
    """调度入口：根据工具名调对应实现。"""
    if not isinstance(name, str):
        return "ERROR: tool name must be a string"
    if not isinstance(args, dict):
        return "ERROR: tool arguments must be an object"
    if name not in config.allowed_tools:
        return f"ERROR: tool '{name}' is not allowed by configuration"
    if name == "Bash":
        kwargs = {"lease_fd": execution_lease_fd}
        if max_bash_timeout is not None:
            kwargs["max_timeout"] = max_bash_timeout
        return execute_bash(args, config, **kwargs)
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        available = [*TOOL_REGISTRY.keys(), "Bash"]
        return f"ERROR: unknown tool '{name}'. Available: {available}"
    if name in {"Write", "Edit", "NotebookEdit"}:
        return fn(args, config.workspace, mutation_budget)
    return fn(args, config.workspace)
