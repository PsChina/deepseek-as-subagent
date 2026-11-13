"""Versioned, bounded task contracts supplied by the parent agent."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

from .resource_budget import MAX_MUTATION_BYTES_PER_RUN, MAX_TOOL_CALLS_PER_RUN

CONTRACT_VERSION = 1
MAX_CONTRACT_BYTES = 32 * 1024
MAX_LIST_ITEMS = 128
MAX_TEXT_CHARS = 2_048
MAX_PATH_PATTERN_CHARS = 256
MAX_PATH_PATTERN_SEGMENTS = 64
KNOWN_TOOLS = frozenset({"Read", "Write", "Edit", "Bash", "Glob", "Grep", "NotebookEdit"})
MUTATION_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
READ_ONLY_INTENTS = frozenset({"readonly_scan", "audit", "review"})
VALID_INTENTS = frozenset({"implement", *READ_ONLY_INTENTS})
DEFAULT_DENY_PATHS = (
    ".env*", "**/.env*", ".npmrc", "**/.npmrc", ".pypirc", "**/.pypirc",
    ".netrc", "**/.netrc", ".git-credentials", "**/.git-credentials",
    "credentials", "**/credentials", "**/credentials.*", "secrets/**", "**/secrets/**",
    "**/.ssh/**", "**/.aws/**", "**/.azure/**", "**/.gnupg/**", "**/.kube/**",
    "**/.docker/config.json", "**/.config/gcloud/**", "**/.config/gh/**",
    "**/.config/containers/auth.json", "**/.terraform.d/credentials.tfrc.json",
    "**/terraform.rc", "**/.cargo/credentials*", "**/.npm/_auth*",
    "**/id_rsa*", "**/id_ed25519*", "**/id_ecdsa*", "**/id_dsa*",
    "**/*.key", "**/*.pem", "**/*.token", "**/*.p12", "**/*.pfx",
    "**/*.tfstate", "**/*.tfstate.*", "**/*.p8", "**/*.jks", "**/*.keystore",
)


def _mapping(value: object, label: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown {label} field(s): {', '.join(unknown)}")
    return value


def _string_list(value: object, label: str, max_items: int = MAX_LIST_ITEMS) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError(f"{label} must be a list with at most {max_items} items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip() or len(item) > MAX_TEXT_CHARS or "\x00" in item:
            raise ValueError(f"{label} must contain non-empty bounded strings")
        result.append(item)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must not contain duplicates")
    return tuple(result)


def validate_path_patterns(value: object, label: str, max_items: int = MAX_LIST_ITEMS) -> tuple[str, ...]:
    patterns = _string_list(value, label, max_items)
    for pattern in patterns:
        if len(pattern) > MAX_PATH_PATTERN_CHARS or len(pattern.split("/")) > MAX_PATH_PATTERN_SEGMENTS:
            raise ValueError(f"{label} contains an overlong path pattern")
        if pattern.startswith("/") or "\\" in pattern or ":" in pattern or any(
            part in {"", ".", ".."} for part in pattern.split("/")
        ):
            raise ValueError(f"{label} must contain normalized workspace-relative glob patterns")
    return patterns


def _bounded_integer(value: object, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be an integer between 1 and {maximum}")
    return value



def _scalar_text_size(value: object) -> int | None:
    if isinstance(value, str):
        return len(value)
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        if value.bit_length() > 64:
            raise ValueError("task contract contains an oversized integer")
        return 0
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("task contract contains a non-finite number")
        return 0
    return None


def _bounded_text_size(total: int, addition: int) -> int:
    total += addition
    if total > MAX_CONTRACT_BYTES:
        raise ValueError("task contract exceeds the maximum text size")
    return total


def _check_json_shape(value: object, max_depth: int = 64, max_nodes: int = 10_000) -> None:
    """Reject excessive nesting, size and non-JSON values before serialization."""
    stack = [(value, 0)]
    visited = 0
    text_chars = 0
    while stack:
        current, depth = stack.pop()
        visited += 1
        if visited > max_nodes:
            raise ValueError("task contract exceeds the maximum JSON node count")
        if depth > max_depth:
            raise ValueError("task contract exceeds the maximum JSON nesting depth")
        scalar_size = _scalar_text_size(current)
        if scalar_size is not None:
            text_chars = _bounded_text_size(text_chars, scalar_size)
            continue
        if isinstance(current, list):
            if visited + len(stack) + len(current) > max_nodes:
                raise ValueError("task contract exceeds the maximum JSON node count")
            stack.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            if visited + len(stack) + len(current) > max_nodes:
                raise ValueError("task contract exceeds the maximum JSON node count")
            for key, item in current.items():
                if not isinstance(key, str):
                    raise ValueError("task contract object keys must be strings")
                text_chars = _bounded_text_size(text_chars, len(key))
                stack.append((item, depth + 1))
            continue
        raise ValueError("task contract contains a non-JSON value")


def _validated_input(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("task contract must be an object")
    try:
        _check_json_shape(value)
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("task contract must contain bounded JSON-compatible values") from None
    if len(encoded.encode("utf-8")) > MAX_CONTRACT_BYTES:
        raise ValueError(f"task contract exceeds {MAX_CONTRACT_BYTES} bytes")
    return _mapping(value, "task contract", {"version", "intent", "permissions", "constraints", "limits"})


def _parse_intent(data: dict) -> str:
    version = data.get("version", CONTRACT_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version != CONTRACT_VERSION:
        raise ValueError(f"unsupported task contract version: {version!r}")
    intent = data.get("intent", "implement")
    if not isinstance(intent, str) or intent not in VALID_INTENTS:
        raise ValueError(f"intent must be one of: {', '.join(sorted(VALID_INTENTS))}")
    return intent


def _parse_permissions(data: dict, intent: str) -> tuple:
    permissions = _mapping(data.get("permissions", {}), "permissions", {"tools", "read_paths", "write_paths", "deny_paths"})
    raw_tools = permissions.get("tools")
    tools = None if raw_tools is None else _string_list(raw_tools, "permissions.tools")
    if tools is not None:
        unknown = sorted(set(tools) - KNOWN_TOOLS)
        if unknown:
            raise ValueError(f"unknown tool(s): {', '.join(unknown)}")
        if "Bash" in tools:
            raise ValueError("Bash is not available in task-contract mode; use an explicit future sandbox policy")
    read_paths = validate_path_patterns(permissions.get("read_paths", ["**/*"]), "permissions.read_paths")
    write_paths = validate_path_patterns(permissions.get("write_paths", []), "permissions.write_paths")
    extra_denies = validate_path_patterns(permissions.get("deny_paths", []), "permissions.deny_paths", MAX_LIST_ITEMS - len(DEFAULT_DENY_PATHS))
    deny_paths = tuple(dict.fromkeys((*DEFAULT_DENY_PATHS, *extra_denies)))
    if intent in READ_ONLY_INTENTS and write_paths:
        raise ValueError(f"intent '{intent}' cannot declare write_paths")
    if tools is not None and set(tools).intersection(MUTATION_TOOLS) and not write_paths:
        raise ValueError("mutation tools require explicit permissions.write_paths")
    if intent in READ_ONLY_INTENTS and tools is not None and set(tools).intersection(MUTATION_TOOLS):
        raise ValueError(f"intent '{intent}' cannot request mutation tools")
    return tools, read_paths, write_paths, deny_paths


def _parse_constraints(data: dict) -> tuple:
    constraints = _mapping(data.get("constraints", {}), "constraints", {"must_not_change", "success_checks", "expected_outputs"})
    return (
        _string_list(constraints.get("must_not_change", []), "constraints.must_not_change"),
        _string_list(constraints.get("success_checks", []), "constraints.success_checks"),
        _string_list(constraints.get("expected_outputs", []), "constraints.expected_outputs"),
    )


def _parse_limits(data: dict) -> tuple[int, int]:
    limits = _mapping(data.get("limits", {}), "limits", {"max_tool_calls", "max_mutation_bytes"})
    return (
        _bounded_integer(limits.get("max_tool_calls", MAX_TOOL_CALLS_PER_RUN), "limits.max_tool_calls", MAX_TOOL_CALLS_PER_RUN),
        _bounded_integer(limits.get("max_mutation_bytes", MAX_MUTATION_BYTES_PER_RUN), "limits.max_mutation_bytes", MAX_MUTATION_BYTES_PER_RUN),
    )


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
        data = _validated_input(value)
        intent = _parse_intent(data)
        permissions = _parse_permissions(data, intent)
        constraints = _parse_constraints(data)
        limits = _parse_limits(data)
        return cls(CONTRACT_VERSION, intent, *permissions, *constraints, *limits)

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
