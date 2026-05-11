"""6 个本地工具的实现 + OpenAI 风格 function schema 定义。

DeepSeek 调用时：
1. agent_loop.py 收到 tool_call(name, arguments)
2. 调度到这里的对应 _execute_xxx 函数
3. 函数返回字符串结果（成功结果 or 错误说明）
4. 字符串塞回 messages 给 DeepSeek 继续

设计原则：
- 失败不抛异常，返回 "ERROR: ..." 字符串让 DeepSeek 自己看到 + 决定下一步
- 输出截断：单次工具结果 > 50K chars 截断，防止把 DeepSeek context 撑爆
- 路径必须走 safety.resolve_safe_path，禁止任何裸路径操作
"""
from __future__ import annotations

import glob as _glob
import re
import subprocess
from pathlib import Path

from .safety import SandboxViolation, check_command, resolve_safe_path

MAX_TOOL_OUTPUT = 50_000  # 单次工具结果最大字符数


def _truncate(text: str) -> str:
    if len(text) > MAX_TOOL_OUTPUT:
        return (
            text[:MAX_TOOL_OUTPUT]
            + f"\n... [truncated, total {len(text)} chars, showing first {MAX_TOOL_OUTPUT}]"
        )
    return text


# ===== 工具实现 =====


def _execute_read(args: dict, workspace: Path) -> str:
    """读文件。args: {path: str, offset?: int, limit?: int}"""
    path = args.get("path", "")
    if not path:
        return "ERROR: missing required 'path' argument"
    try:
        abs_path = resolve_safe_path(path, workspace)
    except SandboxViolation as e:
        return f"ERROR: {e}"
    if not abs_path.exists():
        return f"ERROR: file not found: {path}"
    if not abs_path.is_file():
        return f"ERROR: not a file: {path}"
    try:
        text = abs_path.read_text(errors="replace")
    except Exception as e:
        return f"ERROR: failed to read {path}: {e}"

    offset = int(args.get("offset", 0))
    limit = args.get("limit")
    if offset or limit:
        lines = text.splitlines()
        end = offset + int(limit) if limit else len(lines)
        text = "\n".join(lines[offset:end])

    return _truncate(text)


def _execute_write(args: dict, workspace: Path) -> str:
    """写文件（覆盖）。args: {path: str, content: str}"""
    path = args.get("path", "")
    content = args.get("content", "")
    if not path:
        return "ERROR: missing required 'path' argument"
    try:
        abs_path = resolve_safe_path(path, workspace)
    except SandboxViolation as e:
        return f"ERROR: {e}"
    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content)
    except Exception as e:
        return f"ERROR: failed to write {path}: {e}"
    return f"OK: wrote {len(content)} chars to {path}"


def _execute_edit(args: dict, workspace: Path) -> str:
    """精确字符串替换。args: {path: str, old_string: str, new_string: str, replace_all?: bool}"""
    path = args.get("path", "")
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    replace_all = bool(args.get("replace_all", False))
    if not path or old == "":
        return "ERROR: missing required 'path' or 'old_string'"
    try:
        abs_path = resolve_safe_path(path, workspace)
    except SandboxViolation as e:
        return f"ERROR: {e}"
    if not abs_path.exists():
        return f"ERROR: file not found: {path}"
    try:
        text = abs_path.read_text(errors="replace")
    except Exception as e:
        return f"ERROR: failed to read {path}: {e}"

    count = text.count(old)
    if count == 0:
        return f"ERROR: old_string not found in {path}"
    if count > 1 and not replace_all:
        return (
            f"ERROR: old_string appears {count} times in {path}. "
            f"Use replace_all=true or provide more context to make it unique."
        )

    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    abs_path.write_text(new_text)
    return f"OK: replaced {count if replace_all else 1} occurrence(s) in {path}"


def _execute_bash(args: dict, workspace: Path) -> str:
    """跑 shell 命令。args: {command: str, timeout?: int(seconds)}"""
    command = args.get("command", "")
    if not command:
        return "ERROR: missing required 'command' argument"
    try:
        check_command(command)
    except SandboxViolation as e:
        return f"ERROR: {e}"
    timeout = int(args.get("timeout", 60))
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(workspace),
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR: failed to execute: {e}"

    out = result.stdout or ""
    err = result.stderr or ""
    combined = f"[exit {result.returncode}]\n--- stdout ---\n{out}"
    if err:
        combined += f"\n--- stderr ---\n{err}"
    return _truncate(combined)


def _execute_glob(args: dict, workspace: Path) -> str:
    """文件名 pattern 匹配。args: {pattern: str, path?: str}"""
    pattern = args.get("pattern", "")
    if not pattern:
        return "ERROR: missing required 'pattern' argument"
    base = args.get("path", "")
    try:
        base_path = resolve_safe_path(base, workspace) if base else workspace
    except SandboxViolation as e:
        return f"ERROR: {e}"
    matches = sorted(_glob.glob(str(base_path / pattern), recursive=True))
    # 截短：返回相对 workspace 的路径，便于后续工具调用
    rel_matches = []
    for m in matches[:500]:  # 最多 500 条
        try:
            rel_matches.append(str(Path(m).relative_to(workspace)))
        except ValueError:
            rel_matches.append(m)
    summary = f"Found {len(matches)} match(es)"
    if len(matches) > 500:
        summary += " (showing first 500)"
    return summary + ":\n" + "\n".join(rel_matches)


def _execute_grep(args: dict, workspace: Path) -> str:
    """正则搜索文件内容。args: {pattern: str, path?: str, glob?: str, max_matches?: int}"""
    pattern = args.get("pattern", "")
    if not pattern:
        return "ERROR: missing required 'pattern' argument"
    base = args.get("path", "")
    file_glob = args.get("glob", "**/*")
    max_matches = int(args.get("max_matches", 100))

    try:
        base_path = resolve_safe_path(base, workspace) if base else workspace
    except SandboxViolation as e:
        return f"ERROR: {e}"
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"ERROR: invalid regex: {e}"

    results = []
    for filepath in _glob.iglob(str(base_path / file_glob), recursive=True):
        p = Path(filepath)
        if not p.is_file():
            continue
        try:
            for lineno, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                if regex.search(line):
                    rel = p.relative_to(workspace) if workspace in p.parents else p
                    results.append(f"{rel}:{lineno}: {line}")
                    if len(results) >= max_matches:
                        break
        except Exception:
            continue
        if len(results) >= max_matches:
            break

    if not results:
        return f"No matches found for pattern: {pattern}"
    header = f"Found {len(results)} match(es)"
    if len(results) >= max_matches:
        header += f" (limit {max_matches} reached)"
    return header + ":\n" + "\n".join(results)


# ===== 工具调度表 =====

TOOL_REGISTRY = {
    "Read": _execute_read,
    "Write": _execute_write,
    "Edit": _execute_edit,
    "Bash": _execute_bash,
    "Glob": _execute_glob,
    "Grep": _execute_grep,
}


def execute_tool(name: str, args: dict, workspace: Path) -> str:
    """调度入口：根据工具名调对应实现。"""
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return f"ERROR: unknown tool '{name}'. Available: {list(TOOL_REGISTRY.keys())}"
    return fn(args, workspace)


# ===== OpenAI function calling schema =====


def build_tool_schemas(allowed: list[str]) -> list[dict]:
    """生成 DeepSeek (OpenAI 兼容) 的 tools 参数。"""
    all_schemas = {
        "Read": {
            "name": "Read",
            "description": "Read a file's contents. Use this before editing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace (or absolute path within workspace)."},
                    "offset": {"type": "integer", "description": "Starting line number (0-indexed). Optional."},
                    "limit": {"type": "integer", "description": "Number of lines to read. Optional."},
                },
                "required": ["path"],
            },
        },
        "Write": {
            "name": "Write",
            "description": "Write/overwrite a file with given content. Creates parent dirs if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace."},
                    "content": {"type": "string", "description": "Full file content to write."},
                },
                "required": ["path", "content"],
            },
        },
        "Edit": {
            "name": "Edit",
            "description": "Replace exact text in a file. Fails if old_string appears multiple times unless replace_all=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string", "description": "Exact text to find (must be unique unless replace_all)."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences. Default false."},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
        "Bash": {
            "name": "Bash",
            "description": "Run a shell command in the workspace directory. Dangerous commands are blocked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 60)."},
                },
                "required": ["command"],
            },
        },
        "Glob": {
            "name": "Glob",
            "description": "Find files matching a glob pattern (e.g. **/*.py, src/**/*.ts).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern."},
                    "path": {"type": "string", "description": "Base directory (relative to workspace). Optional."},
                },
                "required": ["pattern"],
            },
        },
        "Grep": {
            "name": "Grep",
            "description": "Search file contents with a regex pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern."},
                    "path": {"type": "string", "description": "Base directory. Optional."},
                    "glob": {"type": "string", "description": "File glob filter (default **/*)."},
                    "max_matches": {"type": "integer", "description": "Max matches to return (default 100)."},
                },
                "required": ["pattern"],
            },
        },
    }
    return [
        {"type": "function", "function": all_schemas[name]}
        for name in allowed
        if name in all_schemas
    ]
