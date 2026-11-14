"""Prepare real-model code tasks without executing model-generated source.

This process has provider credentials but never starts a test process or Docker.
Its single JSON artifact contains fixed-path UTF-8 source snapshots and evidence.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from deepseek_mcp.agent_loop import run_agent
from deepseek_mcp.config import Config
from deepseek_mcp.handoff import build_handoff
from deepseek_mcp.policy_engine import bind_task_contract

_CASES = (
    {
        "id": "repair-off-by-one",
        "initial": "def total(values):\n    return sum(values) + 1\n",
        "task": (
            "Fix src/task.py so total(values) returns the exact sum of the input "
            "values. Empty input must return zero. Preserve the public function name. "
            "Do not run shell commands or tests, and do not change any other file."
        ),
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
    },
)


def _required_environment() -> tuple[str, str, str, int]:
    if os.environ.get("DEEPSEEK_LIVE_CODE_BENCHMARK") != "1":
        raise RuntimeError("set DEEPSEEK_LIVE_CODE_BENCHMARK=1 to opt in")
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is required")
    model = os.environ.get("DEEPSEEK_LIVE_MODEL", "deepseek-chat").strip()
    endpoint = os.environ.get("DEEPSEEK_LIVE_BASE_URL", "https://api.deepseek.com").strip()
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError("live DeepSeek benchmark requires an HTTPS provider endpoint")
    try:
        repeats = int(os.environ.get("DEEPSEEK_BENCHMARK_REPEATS", "2"))
    except ValueError:
        raise RuntimeError("benchmark repeats must be an integer from 1 to 5") from None
    if not 1 <= repeats <= 5:
        raise RuntimeError("benchmark repeats must be an integer from 1 to 5")
    return key, model, endpoint, repeats


def _contract() -> dict:
    return {
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
    }


def _config(key: str, model: str, endpoint: str, workspace: Path) -> Config:
    return Config(
        api_key=key, workspace=workspace, model=model, base_url=endpoint,
        allowed_tools=["Read", "Write", "Edit"], max_turns=8,
        max_output_tokens=2048, max_run_seconds=240,
    )


def _run_one(case: dict, repeat: int, key: str, model: str,
             endpoint: str) -> dict:
    started = time.monotonic()
    policy_hash, tool_calls, tokens = "", 0, {}
    policy_ok, agent_status = False, "error"
    source = case["initial"].encode("utf-8")
    try:
        with tempfile.TemporaryDirectory(prefix="deepseek-agent-benchmark-") as temporary:
            workspace = Path(temporary).resolve()
            (workspace / "src").mkdir()
            target = workspace / "src" / "task.py"
            target.write_bytes(source)
            config = _config(key, model, endpoint, workspace)
            policy = bind_task_contract(config, _contract())
            policy_hash = policy.contract_hash
            try:
                result = run_agent(
                    case["task"] + "\nOnly edit src/task.py. Report honestly when finished.",
                    config,
                )
                agent_status = "completed"
                tool_calls, tokens = result["tool_calls"], result["tokens"]
                handoff = build_handoff(result)
                policy_ok = bool(
                    handoff
                    and handoff["verification"]["policy_compliance"] == "passed"
                    and handoff["verification"]["workspace_readback"] == "matched"
                    and result["bash"]["executed_calls"] == 0
                    and any(item["status"] == "committed" for item in result["mutations"])
                    and {item["path"] for item in handoff["changed_files"]} == {"src/task.py"}
                )
            except Exception as error:
                agent_status = type(error).__name__
            try:
                source = target.read_bytes()
            except OSError:
                pass
    except Exception as error:
        agent_status = type(error).__name__
    if len(source) > 1024 * 1024:
        source = b""
        policy_ok, agent_status = False, "source_limit_exceeded"
    return {
        "task_id": case["id"], "repeat": repeat,
        "agent_status": agent_status, "policy_passed": policy_ok,
        "contract_sha256": policy_hash,
        "source_b64": base64.b64encode(source).decode("ascii"),
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "duration_ms": min(3_600_000, int((time.monotonic() - started) * 1000)),
        "tool_calls": tool_calls, "tokens": tokens,
    }

def main() -> int:
    key, model, endpoint, repeats = _required_environment()
    records = [
        _run_one(case, repeat, key, model, endpoint)
        for case in _CASES for repeat in range(1, repeats + 1)
    ]
    artifact = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": urlsplit(endpoint).hostname,
        "model": model,
        "repeats_per_task": repeats,
        "records": records,
    }
    target = Path(os.environ.get("DEEPSEEK_BENCHMARK_ARTIFACT", "benchmark-input.json"))
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(artifact, ensure_ascii=True, sort_keys=True).encode("utf-8")
    if len(encoded) > 20 * 1024 * 1024:
        raise RuntimeError("benchmark artifact exceeds 20 MiB")
    target.write_bytes(encoded)
    print(json.dumps({"status": "prepared", "model": model,
                      "records": len(records), "artifact": str(target)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
