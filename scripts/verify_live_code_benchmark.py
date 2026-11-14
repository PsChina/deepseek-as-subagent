"""Verify model-produced snapshots with private tests in a no-secret sandbox job."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from deepseek_mcp.benchmark_metrics import summarize_benchmark
from deepseek_mcp.config import Config
from deepseek_mcp.policy_engine import bind_task_contract
from deepseek_mcp.sandbox_executor import (
    compute_test_suite_digest,
    execute_sandboxed_tests,
)

ROOT = Path(__file__).resolve().parents[1]
_HIDDEN = ROOT / "benchmarks" / "hidden"
_SUITES = {
    "repair-off-by-one": _HIDDEN / "off_by_one",
    "repair-case-normalization": _HIDDEN / "case_normalization",
}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ARTIFACT_BYTES = 20 * 1024 * 1024


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("benchmark artifact contains duplicate JSON keys")
        value[key] = item
    return value


def _read_artifact(path: Path) -> dict:
    if os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("verification job must not receive provider credentials")
    payload = path.read_bytes()
    if len(payload) > _MAX_ARTIFACT_BYTES:
        raise ValueError("benchmark artifact exceeds 20 MiB")
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("benchmark artifact is invalid JSON") from None
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "created_at", "provider", "model",
        "repeats_per_task", "records",
    }:
        raise ValueError("benchmark artifact has an invalid schema")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported benchmark artifact version")
    if (not isinstance(value["created_at"], str) or len(value["created_at"]) > 64
            or not isinstance(value["provider"], str) or not value["provider"]
            or len(value["provider"]) > 253
            or not isinstance(value["model"], str) or not value["model"].strip()
            or len(value["model"]) > 128):
        raise ValueError("benchmark artifact metadata is invalid")
    repeats = value["repeats_per_task"]
    if type(repeats) is not int or not 1 <= repeats <= 5:
        raise ValueError("benchmark repeat count is invalid")
    if not isinstance(value["records"], list) or len(value["records"]) != 2 * repeats:
        raise ValueError("benchmark artifact has an invalid record count")
    return value


def _decode_record(raw: object) -> tuple[dict, bytes]:
    required = {
        "task_id", "repeat", "agent_status", "policy_passed",
        "contract_sha256", "source_b64", "source_sha256",
        "duration_ms", "tool_calls", "tokens",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("benchmark task record has an invalid schema")
    task_id = raw["task_id"]
    if not isinstance(task_id, str) or task_id not in _SUITES:
        raise ValueError("benchmark task ID is not recognized")
    if type(raw["repeat"]) is not int or not 1 <= raw["repeat"] <= 5:
        raise ValueError("benchmark repeat index is invalid")
    if not isinstance(raw["agent_status"], str) or not 1 <= len(raw["agent_status"]) <= 64:
        raise ValueError("benchmark Agent status is invalid")
    if not isinstance(raw["policy_passed"], bool):
        raise ValueError("benchmark policy result must be boolean")
    for name in ("contract_sha256", "source_sha256"):
        if not isinstance(raw[name], str) or _SHA256.fullmatch(raw[name]) is None:
            raise ValueError("benchmark digest is invalid")
    for name in ("duration_ms", "tool_calls"):
        if type(raw[name]) is not int or not 0 <= raw[name] <= 3_600_000:
            raise ValueError("benchmark metric is invalid")
    tokens = raw["tokens"]
    if (not isinstance(tokens, dict) or set(tokens) != {"prompt", "completion", "total"}
            or any(type(tokens[name]) is not int or not 0 <= tokens[name] <= 1_000_000_000 for name in tokens)):
        raise ValueError("benchmark token usage is invalid")
    encoded = raw["source_b64"]
    if not isinstance(encoded, str) or len(encoded) > 2 * 1024 * 1024:
        raise ValueError("benchmark source payload is invalid")
    try:
        source = base64.b64decode(encoded, validate=True)
        source.decode("utf-8")
    except (ValueError, UnicodeError):
        raise ValueError("benchmark source is not valid base64 UTF-8") from None
    if len(source) > 1024 * 1024 or hashlib.sha256(source).hexdigest() != raw["source_sha256"]:
        raise ValueError("benchmark source digest mismatch")
    return raw, source


def _config(workspace: Path):
    config = Config(
        api_key="", workspace=workspace, allowed_tools=["Read", "Write", "Edit"],
    )
    policy = bind_task_contract(config, {
        "intent": "implement",
        "permissions": {
            "tools": ["Read", "Write", "Edit"],
            "read_paths": ["src/**"], "write_paths": ["src/**"],
        },
        "constraints": {
            "success_checks": ["trusted host-side hidden acceptance tests"],
            "expected_outputs": ["src/task.py"],
        },
        "limits": {"max_tool_calls": 8, "max_mutation_bytes": 4096},
    })
    return config, policy


def _run_record(raw: dict, source: bytes, image: str) -> dict:
    started = time.monotonic()
    sandbox = {"status": "not_run", "duration_ms": 0}
    try:
        with tempfile.TemporaryDirectory(prefix="deepseek-hidden-eval-") as temporary:
            workspace = Path(temporary).resolve()
            (workspace / "src").mkdir()
            (workspace / "src" / "task.py").write_bytes(source)
            config, policy = _config(workspace)
            if policy.contract_hash != raw["contract_sha256"]:
                raise ValueError("prepared Task Contract does not match verifier contract")
            suite = _SUITES[raw["task_id"]]
            plan = {
                "version": 1,
                "contract_sha256": policy.contract_hash,
                "test_suite_sha256": compute_test_suite_digest(suite),
                "source_files": [{
                    "path": "src/task.py",
                    "sha256": hashlib.sha256(source).hexdigest(),
                }],
            }
            sandbox = execute_sandboxed_tests(
                config, plan, suite, image=image, timeout_seconds=60,
            )
    except Exception as error:
        sandbox = {"status": "incomplete", "error_type": type(error).__name__,
                   "duration_ms": int((time.monotonic() - started) * 1000)}
    status = sandbox.get("status")
    incomplete = status in {"not_run", "incomplete", "sandbox_unavailable",
                            "runner_error", "timed_out", "output_limit"}
    verdict = "passed" if raw["policy_passed"] and status == "passed" else (
        "incomplete" if incomplete else "failed"
    )
    return {
        "id": f"{raw['task_id']}-run-{raw['repeat']}",
        "verdict": verdict,
        "policy_passed": raw["policy_passed"],
        "duration_ms": raw["duration_ms"] + sandbox.get("duration_ms", 0),
        "tool_calls": raw["tool_calls"],
        "task_id": raw["task_id"], "repeat": raw["repeat"],
        "agent_status": raw["agent_status"],
        "sandbox_status": status,
        "sandbox_exit_code": sandbox.get("exit_code"),
        "sandbox_output_bytes": sandbox.get("output_bytes_captured", 0),
        "tokens": raw["tokens"],
    }


def main() -> int:
    artifact_path = Path(os.environ.get("DEEPSEEK_BENCHMARK_ARTIFACT", "benchmark-input.json"))
    image = os.environ.get("DEEPSEEK_TEST_SANDBOX_IMAGE", "")
    artifact = _read_artifact(artifact_path)
    records, seen = [], set()
    for raw in artifact["records"]:
        item, source = _decode_record(raw)
        key = (item["task_id"], item["repeat"])
        if key in seen:
            raise ValueError("benchmark artifact contains a duplicate task run")
        seen.add(key)
        records.append(_run_record(item, source, image))
    summary = summarize_benchmark([
        {key: row[key] for key in (
            "id", "verdict", "policy_passed", "duration_ms", "tool_calls"
        )}
        for row in records
    ])
    report = {
        "schema_version": 1,
        "created_at": artifact["created_at"],
        "provider": artifact["provider"],
        "model": artifact["model"],
        "repeats_per_task": artifact["repeats_per_task"],
        "hidden_tests": "private; not sent to provider",
        "execution": "separate no-secret job; Docker network=none; non-root; read-only mounts",
        "records": records,
        "summary": summary,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if summary["passed"] == summary["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
