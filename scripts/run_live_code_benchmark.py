"""Opt-in real DeepSeek code benchmark with private tests in isolated containers.

The Agent receives no Bash tool and never sees the hidden test suite. The
trusted host runs fixed unittest discovery only after the model has finished.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from deepseek_mcp.agent_loop import run_agent
from deepseek_mcp.benchmark_metrics import summarize_benchmark
from deepseek_mcp.config import Config
from deepseek_mcp.handoff import build_handoff
from deepseek_mcp.policy_engine import bind_task_contract
from deepseek_mcp.sandbox_executor import (
    SandboxUnavailableError,
    compute_test_suite_digest,
    execute_sandboxed_tests,
    _docker_binary,
)

ROOT = Path(__file__).resolve().parents[1]
_HIDDEN = ROOT / "benchmarks" / "hidden"
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}@sha256:[0-9a-f]{64}\Z")
_CASES = (
    {
        "id": "repair-off-by-one",
        "initial": "def total(values):\n    return sum(values) + 1\n",
        "task": (
            "Fix src/task.py so total(values) returns the exact sum of the input "
            "values. Empty input must return zero. Preserve the public function name. "
            "Do not run shell commands or tests, and do not change any other file."
        ),
        "suite": _HIDDEN / "off_by_one",
    },
    {
        "id": "repair-case-normalization",
        "initial": "def slug(value):\n    return value.strip().upper()\n",
        "task": (
            "Fix src/task.py so slug(value) trims surrounding whitespace and "
            "normalizes letters to lowercase. Preserve internal spaces and the "
            "public function name. Do not run shell commands or tests, and do not "
            "change any other file."
        ),
        "suite": _HIDDEN / "case_normalization",
    },
)


def _required_environment() -> tuple[str, str, str, int]:
    if os.environ.get("DEEPSEEK_LIVE_CODE_BENCHMARK") != "1":
        raise RuntimeError("set DEEPSEEK_LIVE_CODE_BENCHMARK=1 to opt in")
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is required")
    image = os.environ.get("DEEPSEEK_TEST_SANDBOX_IMAGE", "").strip()
    if _IMAGE.fullmatch(image) is None:
        raise RuntimeError("DEEPSEEK_TEST_SANDBOX_IMAGE must be pinned by SHA-256 digest")
    repeats_text = os.environ.get("DEEPSEEK_BENCHMARK_REPEATS", "2")
    try:
        repeats = int(repeats_text)
    except ValueError:
        raise RuntimeError("DEEPSEEK_BENCHMARK_REPEATS must be an integer from 1 to 5") from None
    if not 1 <= repeats <= 5:
        raise RuntimeError("DEEPSEEK_BENCHMARK_REPEATS must be an integer from 1 to 5")
    model = os.environ.get("DEEPSEEK_LIVE_MODEL", "deepseek-chat").strip()
    endpoint = os.environ.get("DEEPSEEK_LIVE_BASE_URL", "https://api.deepseek.com").strip()
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError("live DeepSeek benchmark requires an HTTPS provider endpoint")
    return key, image, model, repeats


def _task_contract() -> dict:
    return {
        "intent": "implement",
        "permissions": {
            "tools": ["Read", "Write", "Edit"],
            "read_paths": ["src/**"],
            "write_paths": ["src/**"],
        },
        "constraints": {
            "success_checks": ["trusted host-side hidden acceptance tests"],
            "expected_outputs": ["src/task.py"],
        },
        "limits": {"max_tool_calls": 8, "max_mutation_bytes": 4096},
    }


def _config(key: str, model: str, endpoint: str, workspace: Path) -> Config:
    return Config(
        api_key=key, workspace=workspace, model=model, base_url=endpoint,
        allowed_tools=["Read", "Write", "Edit"], max_turns=8,
        max_output_tokens=2048, max_run_seconds=240,
    )


def _run_one(case: dict, repeat: int, key: str, image: str,
             model: str, endpoint: str) -> dict:
    started = time.monotonic()
    tool_calls, policy_ok, sandbox = 0, False, {"status": "not_run"}
    token_usage = {}
    try:
        with tempfile.TemporaryDirectory(prefix="deepseek-live-benchmark-") as temporary:
            workspace = Path(temporary).resolve()
            (workspace / "src").mkdir()
            (workspace / "src" / "task.py").write_bytes(case["initial"].encode("utf-8"))
            config = _config(key, model, endpoint, workspace)
            policy = bind_task_contract(config, _task_contract())
            result = run_agent(
                case["task"] + "\nOnly edit src/task.py. Report honestly when finished.",
                config,
            )
            tool_calls = result["tool_calls"]
            token_usage = result["tokens"]
            handoff = build_handoff(result)
            policy_ok = bool(
                handoff
                and handoff["verification"]["policy_compliance"] == "passed"
                and handoff["verification"]["workspace_readback"] == "matched"
                and result["bash"]["executed_calls"] == 0
                and any(item["status"] == "committed" for item in result["mutations"])
            )
            source = (workspace / "src" / "task.py").read_bytes()
            plan = {
                "version": 1,
                "contract_sha256": policy.contract_hash,
                "test_suite_sha256": compute_test_suite_digest(case["suite"]),
                "source_files": [{
                    "path": "src/task.py",
                    "sha256": hashlib.sha256(source).hexdigest(),
                }],
            }
            sandbox = execute_sandboxed_tests(
                config, plan, case["suite"], image=image, timeout_seconds=60,
            )
    except SandboxUnavailableError:
        sandbox = {"status": "sandbox_unavailable"}
    except Exception as error:
        sandbox = {"status": "incomplete", "error_type": type(error).__name__}
    elapsed = min(3_600_000, int((time.monotonic() - started) * 1000))
    passed = policy_ok and sandbox.get("status") == "passed"
    verdict = "passed" if passed else (
        "incomplete" if sandbox.get("status") in {"not_run", "sandbox_unavailable", "incomplete", "runner_error"}
        else "failed"
    )
    return {
        "id": f"{case['id']}-run-{repeat}",
        "verdict": verdict,
        "policy_passed": policy_ok,
        "duration_ms": elapsed,
        "tool_calls": tool_calls,
        "model": model,
        "task_id": case["id"],
        "repeat": repeat,
        "tokens": token_usage,
        "sandbox": {key: value for key, value in sandbox.items()
                    if key not in {"output"}},
    }


def main() -> int:
    try:
        key, image, model, repeats = _required_environment()
        _docker_binary()
    except (RuntimeError, SandboxUnavailableError) as error:
        print(json.dumps({"status": "not_started", "reason": str(error)}))
        return 2
    endpoint = os.environ.get("DEEPSEEK_LIVE_BASE_URL", "https://api.deepseek.com")
    records = [
        _run_one(case, repeat, key, image, model, endpoint)
        for case in _CASES for repeat in range(1, repeats + 1)
    ]
    summary = summarize_benchmark([
        {key: row[key] for key in (
            "id", "verdict", "policy_passed", "duration_ms", "tool_calls"
        )}
        for row in records
    ])
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": urlsplit(endpoint).hostname,
        "model": model,
        "repeats_per_task": repeats,
        "hidden_tests": "private; not sent to provider",
        "execution": "Docker; no network; read-only mounts; non-root",
        "records": records,
        "summary": summary,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if summary["passed"] == summary["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
