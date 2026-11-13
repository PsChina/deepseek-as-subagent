"""Versioned, bounded task contracts supplied by the parent agent."""
from __future__ import annotations

import json
from dataclasses import dataclass

from .resource_budget import MAX_MUTATION_BYTES_PER_RUN, MAX_TOOL_CALLS_PER_RUN

CONTRACT_VERSION = 1
MAX_CONTRACT_BYTES = 32 * 1024
MAX_LIST_ITEMS = 128
MAX_TEXT_CHARS = 2_048
KNOWN_TOOLS = frozenset({"Read", "Write", "Edit", "Bash", "Glob", "Grep", "NotebookEdit"})
MUTATION_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
READ_ONLY_INTENTS = frozenset({"readonly_scan", "audit", "review"})
VALID_INTENTS = frozenset({"implement", *READ_ONLY_INTENTS})
DEFAULT_DENY_PATHS = (
    ".env*", "**/.env*", "secrets/**", "**/secrets/**",
    "**/*.key", "**/*.pem", "**/*.token",
)


def _mapping(value: object, label: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown {label} field(s): {', '.join(unknown)}")
    return value


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_LIST_ITEMS:
        raise ValueError(f"{label} must be a list with at most {MAX_LIST_ITEMS} items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip() or len(item) > MAX_TEXT_CHARS or "\x00" in item:
            raise ValueError(f"{label} must contain non-empty bounded strings")
        result.append(item)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must not contain duplicates")
    return tuple(result)


def validate_path_patterns(value: object, label: str) -> tuple[str, ...]:
    patterns = _string_list(value, label)
    for pattern in patterns:
        if pattern.startswith("/") or "\\" in pattern or ":" in pattern or any(
            part in {"", ".", ".."} for part in pattern.split("/")
        ):
            raise ValueError(f"{label} must contain normalized workspace-relative glob patterns")
    return patterns


def _bounded_integer(value: object, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be an integer between 1 and {maximum}")
    return value


@dataclass(frozen=True)
class TaskContract:
    version: int
    intent: str
    tools: tuple[str, ...] | None
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    deny_paths: tuple[str, ...]
    must_not_change: tuple[str, ...]
    success_checks: tuple[str, ...]
    expected_outputs: tuple[str, ...]
    max_tool_calls: int
    max_mutation_bytes: int

    @classmethod
    def from_dict(cls, value: object) -> "TaskContract":
        if not isinstance(value, dict):
            raise ValueError("task contract must be an object")
        try:
            encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            raise ValueError("task contract must contain JSON-compatible values") from None
        if len(encoded.encode("utf-8")) > MAX_CONTRACT_BYTES:
            raise ValueError(f"task contract exceeds {MAX_CONTRACT_BYTES} bytes")
        data = _mapping(value, "task contract", {"version", "intent", "permissions", "constraints", "limits"})
        version = data.get("version", CONTRACT_VERSION)
        if isinstance(version, bool) or version != CONTRACT_VERSION:
            raise ValueError(f"unsupported task contract version: {version!r}")
        intent = data.get("intent", "implement")
        if not isinstance(intent, str) or intent not in VALID_INTENTS:
            raise ValueError(f"intent must be one of: {', '.join(sorted(VALID_INTENTS))}")

        permissions = _mapping(data.get("permissions", {}), "permissions", {"tools", "read_paths", "write_paths", "deny_paths"})
        raw_tools = permissions.get("tools")
        tools = None if raw_tools is None else _string_list(raw_tools, "permissions.tools")
        if tools is not None:
            unknown_tools = sorted(set(tools) - KNOWN_TOOLS)
            if unknown_tools:
                raise ValueError(f"unknown tool(s): {', '.join(unknown_tools)}")
            if "Bash" in tools:
                raise ValueError("Bash is not available in task-contract mode; use an explicit future sandbox policy")

        read_paths = validate_path_patterns(permissions.get("read_paths", ["**/*"]), "permissions.read_paths")
        write_paths = validate_path_patterns(permissions.get("write_paths", []), "permissions.write_paths")
        extra_denies = validate_path_patterns(permissions.get("deny_paths", []), "permissions.deny_paths")
        deny_paths = tuple(dict.fromkeys((*DEFAULT_DENY_PATHS, *extra_denies)))
        if intent in READ_ONLY_INTENTS and write_paths:
            raise ValueError(f"intent '{intent}' cannot declare write_paths")
        if tools is not None and set(tools).intersection(MUTATION_TOOLS) and not write_paths:
            raise ValueError("mutation tools require explicit permissions.write_paths")
        if intent in READ_ONLY_INTENTS and tools is not None and set(tools).intersection(MUTATION_TOOLS):
            raise ValueError(f"intent '{intent}' cannot request mutation tools")

        constraints = _mapping(data.get("constraints", {}), "constraints", {"must_not_change", "success_checks", "expected_outputs"})
        must_not_change = _string_list(constraints.get("must_not_change", []), "constraints.must_not_change")
        success_checks = _string_list(constraints.get("success_checks", []), "constraints.success_checks")
        expected_outputs = _string_list(constraints.get("expected_outputs", []), "constraints.expected_outputs")
        limits = _mapping(data.get("limits", {}), "limits", {"max_tool_calls", "max_mutation_bytes"})
        max_tool_calls = _bounded_integer(limits.get("max_tool_calls", MAX_TOOL_CALLS_PER_RUN), "limits.max_tool_calls", MAX_TOOL_CALLS_PER_RUN)
        max_mutation_bytes = _bounded_integer(limits.get("max_mutation_bytes", MAX_MUTATION_BYTES_PER_RUN), "limits.max_mutation_bytes", MAX_MUTATION_BYTES_PER_RUN)
        return cls(
            CONTRACT_VERSION, intent, tools, read_paths, write_paths, deny_paths,
            must_not_change, success_checks, expected_outputs,
            max_tool_calls, max_mutation_bytes,
        )

    def canonical_dict(self) -> dict:
        return {
            "version": self.version,
            "intent": self.intent,
            "permissions": {
                **({"tools": list(self.tools)} if self.tools is not None else {}),
                "read_paths": list(self.read_paths),
                "write_paths": list(self.write_paths),
                "deny_paths": list(self.deny_paths),
            },
            "constraints": {
                "must_not_change": list(self.must_not_change),
                "success_checks": list(self.success_checks),
                "expected_outputs": list(self.expected_outputs),
            },
            "limits": {
                "max_tool_calls": self.max_tool_calls,
                "max_mutation_bytes": self.max_mutation_bytes,
            },
        }

    def digest(self) -> str:
        import hashlib
        payload = json.dumps(self.canonical_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
