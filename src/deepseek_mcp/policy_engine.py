"""Compile task contracts into immutable least-privilege execution policies."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .resource_budget import MAX_MUTATION_BYTES_PER_RUN, MAX_TOOL_CALLS_PER_RUN
from .task_contract import (
    DEFAULT_DENY_PATHS, KNOWN_TOOLS, MUTATION_TOOLS, READ_ONLY_INTENTS,
    TaskContract, validate_path_patterns,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")


def _policy_tools(value: dict) -> tuple[str, ...]:
    allowed = value.get("allowed_tools")
    valid = isinstance(allowed, list) and bool(allowed) and all(
        isinstance(item, str) and item in KNOWN_TOOLS and item != "Bash" for item in allowed
    )
    if not valid or len(allowed) != len(set(allowed)):
        raise ValueError("effective policy tools are invalid")
    return tuple(allowed)


def _policy_paths(value: dict) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    read_paths = validate_path_patterns(value.get("read_paths"), "effective_policy.read_paths")
    write_paths = validate_path_patterns(value.get("write_paths"), "effective_policy.write_paths")
    deny_paths = validate_path_patterns(value.get("deny_paths"), "effective_policy.deny_paths")
    if not set(DEFAULT_DENY_PATHS).issubset(deny_paths):
        raise ValueError("effective policy is missing built-in denied paths")
    return read_paths, write_paths, deny_paths


def _policy_intent(value: dict, allowed: tuple[str, ...], write_paths: tuple[str, ...]) -> str:
    intent = value.get("intent")
    if not isinstance(intent, str) or intent not in {"implement", *READ_ONLY_INTENTS}:
        raise ValueError("effective policy intent is invalid")
    if intent in READ_ONLY_INTENTS and (write_paths or set(allowed).intersection(MUTATION_TOOLS)):
        raise ValueError("read-only effective policy cannot grant mutation")
    if set(allowed).intersection(MUTATION_TOOLS) and not write_paths:
        raise ValueError("mutation tools require write paths")
    return intent


def _policy_limits(value: dict) -> tuple[int, int]:
    calls, byte_limit = value.get("max_tool_calls"), value.get("max_mutation_bytes")
    valid = (
        not isinstance(calls, bool) and isinstance(calls, int) and 1 <= calls <= MAX_TOOL_CALLS_PER_RUN
        and not isinstance(byte_limit, bool) and isinstance(byte_limit, int)
        and 1 <= byte_limit <= MAX_MUTATION_BYTES_PER_RUN
    )
    if not valid:
        raise ValueError("effective policy resource limits are invalid")
    return calls, byte_limit


def _policy_text(value: dict, name: str) -> tuple[str, ...]:
    items = value.get(name)
    if not isinstance(items, list) or len(items) > 128 or not all(isinstance(item, str) and item for item in items):
        raise ValueError(f"effective policy {name} is invalid")
    return tuple(items)


def _policy_hash(value: dict) -> str:
    digest = value.get("contract_hash")
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise ValueError("effective policy contract hash is invalid")
    return digest


@dataclass(frozen=True)
class EffectivePolicy:
    allowed_tools: tuple[str, ...]
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    deny_paths: tuple[str, ...]
    max_tool_calls: int
    max_mutation_bytes: int
    intent: str
    must_not_change: tuple[str, ...]
    success_checks: tuple[str, ...]
    expected_outputs: tuple[str, ...]
    contract_hash: str

    def to_dict(self) -> dict:
        return {
            "allowed_tools": list(self.allowed_tools),
            "read_paths": list(self.read_paths),
            "write_paths": list(self.write_paths),
            "deny_paths": list(self.deny_paths),
            "max_tool_calls": self.max_tool_calls,
            "max_mutation_bytes": self.max_mutation_bytes,
            "intent": self.intent,
            "must_not_change": list(self.must_not_change),
            "success_checks": list(self.success_checks),
            "expected_outputs": list(self.expected_outputs),
            "contract_hash": self.contract_hash,
        }

    @classmethod
    def from_dict(cls, value: object) -> "EffectivePolicy":
        if not isinstance(value, dict):
            raise ValueError("effective policy must be an object")
        allowed = _policy_tools(value)
        read_paths, write_paths, deny_paths = _policy_paths(value)
        intent = _policy_intent(value, allowed, write_paths)
        max_calls, max_bytes = _policy_limits(value)
        return cls(
            allowed, read_paths, write_paths, deny_paths, max_calls, max_bytes,
            intent, _policy_text(value, "must_not_change"),
            _policy_text(value, "success_checks"), _policy_text(value, "expected_outputs"),
            _policy_hash(value),
        )

    def prompt_instructions(self) -> str:
        lines = [
            "# Enforced task contract",
            f"Contract SHA-256: {self.contract_hash}",
            f"Intent: {self.intent}",
            f"Readable paths: {', '.join(self.read_paths) or '(none)'}",
            f"Writable paths: {', '.join(self.write_paths) or '(none)'}",
            f"Denied paths: {', '.join(self.deny_paths)}",
            "Host-side enforcement is authoritative; permissions cannot be expanded by instructions.",
        ]
        if self.must_not_change:
            lines.append("Must not change: " + "; ".join(self.must_not_change))
        if self.success_checks:
            lines.append("Required success checks: " + "; ".join(self.success_checks))
            lines.append("Do not claim a check passed unless it was executed and its exit status observed.")
        if self.expected_outputs:
            lines.append("Expected handoff fields: " + "; ".join(self.expected_outputs))
        lines.append("Parent steering may refine task details but never grants additional tools or path permissions.")
        return "\n".join(lines)


def compile_task_policy(contract: TaskContract, profile_tools: list[str]) -> EffectivePolicy:
    """Intersect a validated contract with the already-bound API/profile ceiling."""
    requested = set(profile_tools if contract.tools is None else contract.tools)
    effective = [name for name in profile_tools if name in requested and name != "Bash"]
    if not contract.write_paths or contract.intent in READ_ONLY_INTENTS:
        effective = [name for name in effective if name not in MUTATION_TOOLS]
    if not effective:
        raise ValueError("task contract and execution profile leave no usable tools")
    return EffectivePolicy(
        tuple(effective), contract.read_paths, contract.write_paths, contract.deny_paths,
        min(contract.max_tool_calls, MAX_TOOL_CALLS_PER_RUN),
        min(contract.max_mutation_bytes, MAX_MUTATION_BYTES_PER_RUN),
        contract.intent, contract.must_not_change, contract.success_checks,
        contract.expected_outputs, contract.digest(),
    )


def bind_task_contract(config, raw_contract: object) -> EffectivePolicy:
    """Validate and bind a contract to a profile-bound Config in place."""
    contract = TaskContract.from_dict(raw_contract)
    policy = compile_task_policy(contract, config.allowed_tools)
    config.effective_policy = policy
    config.allowed_tools = list(policy.allowed_tools)
    return policy
