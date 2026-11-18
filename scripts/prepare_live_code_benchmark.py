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
from deepseek_mcp.file_io import read_workspace_text
from deepseek_mcp.handoff import build_handoff
from deepseek_mcp.policy_enforcement import bind_file_policy
from deepseek_mcp.policy_engine import bind_task_contract
from deepseek_mcp.task_contract import TaskContract
from deepseek_mcp.workspace_guard import bind_workspace_identity

if __package__:
    from .live_benchmark_contract import TASK_PATH, TASK_TOOLS, task_contract
else:
    from live_benchmark_contract import TASK_PATH, TASK_TOOLS, task_contract

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


def _config(key: str, model: str, endpoint: str, workspace: Path) -> Config:
    return Config(
        api_key=key, workspace=workspace, model=model, base_url=endpoint,
        allowed_tools=list(TASK_TOOLS), max_turns=8,
        max_output_tokens=2048, max_run_seconds=240,
    )


def _policy_passed(result: dict, source_sha256: str) -> bool:
    handoff = build_handoff(result)
    mutations = result["mutations"]
    if not handoff or not mutations:
        return False
    verification = handoff["verification"]
    return (
        verification["policy_compliance"] == "passed"
        and verification["resource_compliance"] == "passed"
        and verification["mutation_integrity"] == "verified_at_commit"
        and verification["workspace_readback"] == "matched"
        and all(item["status"] == "committed" and item["path"] == TASK_PATH for item in mutations)
        and mutations[-1]["sha256"] == source_sha256
    )


def _run_one(case: dict, repeat: int, key: str, model: str,
             endpoint: str) -> dict:
    started = time.monotonic()
    contract = task_contract()
    policy_hash = TaskContract.from_dict(contract).digest()
    tool_calls = 0
    tokens = {"prompt": 0, "completion": 0, "total": 0}
    policy_ok, agent_status = False, "not_started"
    source = case["initial"].encode("utf-8")
    try:
        with tempfile.TemporaryDirectory(prefix="deepseek-agent-benchmark-") as temporary:
            workspace = Path(temporary).resolve()
            (workspace / "src").mkdir()
            target = workspace / TASK_PATH
            target.write_bytes(source)
            config = _config(key, model, endpoint, workspace)
            policy = bind_task_contract(config, contract)
            result = run_agent(
                case["task"] + "\nOnly edit src/task.py. Report honestly when finished.",
                config,
            )
            tool_calls, tokens = result["tool_calls"], result["tokens"]
            with bind_workspace_identity(config.expected_workspace_identity), bind_file_policy(policy, "Read"):
                content, identity = read_workspace_text(
                    workspace, TASK_PATH, require_canonical=True, strict_utf8=True, reject_binary=True,
                )
            source = content.encode("utf-8")
            current_digest = identity.digest.hex() if identity.digest is not None else ""
            policy_ok = _policy_passed(result, current_digest)
            agent_status = "completed"
    except Exception as error:
        agent_status = type(error).__name__
    if len(source) > 1024 * 1024:
        source = b""
        policy_ok, agent_status = False, "source_limit_exceeded"
    if key.encode("utf-8") in source:
        source = b""
        policy_ok, agent_status = False, "credential_redacted"
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
