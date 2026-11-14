"""Offline scripted code-task benchmark (not a live model quality benchmark).

Use only a trusted CI runner: no provider credentials, no shell execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp.agent_loop import run_agent
from deepseek_mcp.benchmark_metrics import summarize_benchmark
from deepseek_mcp.config import Config, DEFAULT_ALLOWED_TOOLS
from deepseek_mcp.handoff import build_handoff
from deepseek_mcp.independent_verifier import verify_artifacts
from deepseek_mcp.policy_engine import compile_task_policy
from deepseek_mcp.task_contract import TaskContract

CASES = (
    ("repair-off-by-one", "def total(values):\n    return sum(values) + 1\n",
     "def total(values):\n    return sum(values)\n",
     "sum(values) + 1", "sum(values)"),
    ("repair-case-normalization", "def slug(value):\n    return value.strip().upper()\n",
     "def slug(value):\n    return value.strip().lower()\n",
     "value.strip().upper()", "value.strip().lower()"),
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _trusted_fixture_behavior(case_id: str, source: str) -> bool:
    """Run tiny, exact-hash-approved fixture code inside trusted CI only."""
    scope = {"__builtins__": {"len": len, "sum": sum}}
    exec(compile(source, f"<trusted-fixture:{case_id}>", "exec"), scope)
    if case_id == "repair-off-by-one":
        return scope["total"]([]) == 0 and scope["total"]([2, 3, -1]) == 4
    if case_id == "repair-case-normalization":
        return scope["slug"](" HeLLo ") == "hello" and scope["slug"]("XY") == "xy"
    return False


class _Provider(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.server.requests.append(payload)
        idx = len(self.server.requests) - 1
        if idx < len(self.server.sequence):
            name, args = self.server.sequence[idx]
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"scripted-{idx}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": "Scripted task completed"}
            finish = "stop"
        response = {"id": f"offline-{idx}", "object": "chat.completion",
                    "created": 1, "model": payload.get("model", "offline"),
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format, *_args):
        pass


def _scripted_server(sequence):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    server.sequence = sequence
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _execute(case_id, initial, expected, old, new):
    sequence = [
        ("Read", {"path": "src/task.py"}),
        ("Edit", {"path": "src/task.py", "old_string": old, "new_string": new}),
        ("Read", {"path": "src/task.py"}),
    ]
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp).resolve()
        workspace = root / "workspace"
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "task.py").write_text(initial, encoding="utf-8")
        (workspace / "src" / "guard.txt").write_text("do-not-change", encoding="utf-8")
        home = root / "home"
        home.mkdir(mode=0o700)
        drive, home_path = os.path.splitdrive(str(home))
        contract = TaskContract.from_dict({"permissions": {
            "tools": ["Read", "Edit"],
            "read_paths": ["src/**"], "write_paths": ["src/**"],
        }})
        policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
        server, thread = _scripted_server(sequence)
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1"
        config = Config("local-only", workspace, model="offline", base_url=endpoint,
                        allowed_tools=list(policy.allowed_tools), effective_policy=policy,
                        max_turns=5)
        started = time.monotonic()
        try:
            with patch.dict(os.environ, {
                "HOME": str(home), "USERPROFILE": str(home),
                "HOMEDRIVE": drive, "HOMEPATH": home_path or os.sep,
            }):
                result = run_agent(case_id, config)
            evidence = verify_artifacts(config, {"version": 1,
                "contract_sha256": policy.contract_hash,
                "artifacts": [
                    {"path": "src/task.py", "sha256": _sha(expected), "role": "output"},
                    {"path": "src/guard.txt", "sha256": _sha("do-not-change"),
                     "role": "unchanged"},
                ],
            })
            handoff = build_handoff(result)
            policy_ok = handoff["verification"]["policy_compliance"] == "passed"
            committed = handoff["verification"]["workspace_readback"] == "matched"
            artifact_ok = evidence["status"] == "artifact_verified"
            # Never execute model-written code if its bytes differ from the
            # trusted fixture. This is NOT an isolation mechanism for arbitrary code.
            behavior_ok = (
                _trusted_fixture_behavior(case_id, (
                    workspace / "src" / "task.py"
                ).read_text(encoding="utf-8")) if artifact_ok else False
            )
            success = artifact_ok and behavior_ok and policy_ok and committed
            calls = result["tool_calls"]
        except Exception as error:
            print(f"{case_id}: {type(error).__name__}", file=sys.stderr)
            policy_ok, success, calls = False, False, 0
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()
        duration = min(3_600_000, int((time.monotonic() - started) * 1000))
        return {"id": case_id, "verdict": "passed" if success else "failed",
                "policy_passed": policy_ok, "duration_ms": duration,
                "tool_calls": calls}


def main() -> int:
    records = [_execute(*case) for case in CASES]
    summary = summarize_benchmark(records)
    print(json.dumps({"note": "offline scripted integration baseline, not model reasoning quality",
                      "cases": records, "summary": summary}, sort_keys=True))
    return 0 if summary["passed"] == summary["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
