"""OpenAI-compatible tool schemas kept separate from tool execution logic."""
from __future__ import annotations

from .bash_tool import DEFAULT_BASH_TIMEOUT, MAX_BASH_TIMEOUT

TOOL_SCHEMAS = {
    "Read": {
        "name": "Read",
        "description": "Read a bounded UTF-8 text file. Use this before editing.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path within workspace."},
                "offset": {"type": "integer", "description": "Starting line, 0-indexed."},
                "limit": {"type": "integer", "description": "Number of lines to read."},
            },
            "required": ["path"],
        },
    },
    "Write": {
        "name": "Write",
        "description": (
            "Create a new bounded UTF-8 file inside the workspace. "
            "Use Edit for an existing file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path within workspace."},
                "content": {"type": "string", "description": "Full file content."},
            },
            "required": ["path", "content"],
        },
    },
    "Edit": {
        "name": "Edit",
        "description": "Replace exact text in a bounded workspace file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string", "description": "Exact text to find."},
                "new_string": {"type": "string", "description": "Replacement text."},
                "replace_all": {"type": "boolean", "description": "Replace every match."},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    "Bash": {
        "name": "Bash",
        "description": (
            "Run a shell command in a pinned, network-disabled container over a "
            "disposable read-only regular-file workspace snapshot. Host files are "
            f"not mounted writable. Timeout defaults to "
            f"{DEFAULT_BASH_TIMEOUT}s and is capped at {MAX_BASH_TIMEOUT}s."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command."},
                "timeout": {"type": "integer", "description": "Timeout in seconds."},
            },
            "required": ["command"],
        },
    },
    "Glob": {
        "name": "Glob",
        "description": "Find bounded workspace matches; external symlinks are hidden.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern."},
                "path": {"type": "string", "description": "Optional base directory."},
            },
            "required": ["pattern"],
        },
    },
    "Grep": {
        "name": "Grep",
        "description": "Search bounded workspace text files with a safe RE2 expression.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "RE2 expression; look-around/backreferences are unsupported.",
                },
                "path": {"type": "string", "description": "Optional base directory."},
                "glob": {"type": "string", "description": "File glob filter."},
                "max_matches": {"type": "integer", "description": "Result limit."},
            },
            "required": ["pattern"],
        },
    },
    "NotebookEdit": {
        "name": "NotebookEdit",
        "description": (
            "Edit a bounded Jupyter notebook cell-by-cell. Insert adds a new "
            "cell immediately after the target anchor, or appends when no valid "
            "anchor is supplied."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Notebook workspace path."},
                "edit_mode": {
                    "type": "string",
                    "enum": ["replace", "insert", "delete"],
                },
                "cell_id": {
                    "type": "string",
                    "description": "Stable target anchor; insert places the new cell after it.",
                },
                "cell_index": {
                    "type": "integer",
                    "description": "0-indexed target anchor; insert places the new cell after it.",
                },
                "new_source": {"type": "string", "description": "Replacement or new source."},
                "cell_type": {
                    "type": "string",
                    "enum": ["code", "markdown"],
                },
            },
            "required": ["path", "edit_mode"],
        },
    },
}


def build_tool_schemas(allowed: list[str]) -> list[dict]:
    """Return schemas in configured order, silently excluding unknown names."""
    return [
        {"type": "function", "function": TOOL_SCHEMAS[name]}
        for name in allowed
        if name in TOOL_SCHEMAS
    ]
