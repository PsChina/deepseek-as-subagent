"""Deterministic aggregation of reproducible code-task benchmark results."""
from __future__ import annotations

from math import ceil

_MAX_CASES = 256
_VALID = frozenset({"passed", "failed", "incomplete"})


def _validate(row: object) -> dict:
    if not isinstance(row, dict) or set(row) != {
        "id", "verdict", "policy_passed", "duration_ms", "tool_calls"
    }:
        raise ValueError("benchmark record has an invalid schema")
    key = row["id"]
    if not isinstance(key, str) or not 1 <= len(key) <= 128 or key != key.strip():
        raise ValueError("benchmark case id is invalid")
    if not isinstance(row["verdict"], str) or row["verdict"] not in _VALID:
        raise ValueError("benchmark verdict is invalid")
    if not isinstance(row["policy_passed"], bool):
        raise ValueError("benchmark policy result must be boolean")
    for name in ("duration_ms", "tool_calls"):
        value = row[name]
        if type(value) is not int or not 0 <= value <= 3_600_000:
            raise ValueError("benchmark metric must be a bounded integer")
    return row


def summarize_benchmark(rows: object) -> dict:
    """Fail closed on incomplete cases; never count them as successful tasks."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= _MAX_CASES:
        raise ValueError("benchmark requires 1–256 case records")
    seen: set[str] = set()
    normalized = []
    for item in rows:
        result = _validate(item)
        if result["id"] in seen:
            raise ValueError("duplicate benchmark case id")
        seen.add(result["id"])
        normalized.append(result)
    count = len(normalized)
    durations = sorted(item["duration_ms"] for item in normalized)
    passed = sum(item["verdict"] == "passed" and item["policy_passed"]
                 for item in normalized)
    unsafe = sum(not item["policy_passed"] for item in normalized)
    incomplete = sum(item["verdict"] == "incomplete" for item in normalized)
    midpoint = (durations[(count - 1) // 2] + durations[count // 2]) / 2
    return {"schema_version": 1, "cases": count, "passed": passed,
            "incomplete": incomplete, "unsafe": unsafe,
            "task_success_rate": round(passed / count, 4),
            "policy_pass_rate": round((count - unsafe) / count, 4),
            "median_duration_ms": midpoint,
            "p95_duration_ms": durations[ceil(count * .95) - 1],
            "total_tool_calls": sum(item["tool_calls"] for item in normalized)}
