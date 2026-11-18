"""End-to-end contract-bound agent tests using a real localhost provider and tool subprocesses."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp.agent_loop import run_agent
from deepseek_mcp.config import Config, DEFAULT_ALLOWED_TOOLS
from deepseek_mcp.handoff import build_handoff
from deepseek_mcp.policy_engine import compile_task_policy
from deepseek_mcp.provider_retry import AgentLoopError
from deepseek_mcp.task_contract import TaskContract


class _ScriptedProvider(BaseHTTPRequestHandler):
    sequence: list[tuple[str, dict]] = []
    requests: list[dict] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        type(self).requests.append(request)
        index = len(type(self).requests) - 1
        if index < len(self.sequence):
            name, arguments = self.sequence[index]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call-{index}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            }
            reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": "Finished the scripted task."}
            reason = "stop"
        payload = {
            "id": f"script-{index}", "object": "chat.completion", "created": 1,
            "model": request.get("model", "local"),
            "choices": [{"index": 0, "message": message, "finish_reason": reason}],
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *_args) -> None:
        pass


class ContractEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)

    def _endpoint(self, sequence: list[tuple[str, dict]]) -> str:
        _ScriptedProvider.requests = []
        _ScriptedProvider.sequence = sequence
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedProvider)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/v1"

    def _config(self, endpoint: str, contract: dict, max_turns: int = 7) -> Config:
        policy = compile_task_policy(
            TaskContract.from_dict(contract), DEFAULT_ALLOWED_TOOLS
        )
        return Config(
            "local-test-only", self.workspace, model="local", base_url=endpoint,
            allowed_tools=list(policy.allowed_tools), effective_policy=policy,
            max_turns=max_turns,
        )

    def _run_with_isolated_home(self, config: Config) -> dict:
        drive, path = os.path.splitdrive(str(self.home))
        with patch.dict(os.environ, {
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "HOMEDRIVE": drive, "HOMEPATH": path or os.sep,
        }):
            return run_agent("Execute the scripted multi-step task", config)

    def test_real_child_read_write_edit_read_and_structured_handoff(self) -> None:
        (self.workspace / "src" / "input.txt").write_text(
            "seed", encoding="utf-8"
        )
        endpoint = self._endpoint([
            ("Read", {"path": "src/input.txt"}),
            ("Write", {"path": "src/result.txt", "content": "alpha\\n"}),
            ("Edit", {
                "path": "src/result.txt", "old_string": "alpha",
                "new_string": "omega",
            }),
            ("Read", {"path": "src/result.txt"}),
        ])
        config = self._config(endpoint, {
            "permissions": {
                "tools": ["Read", "Write", "Edit"],
                "read_paths": ["src/**"], "write_paths": ["src/**"],
            },
            "limits": {"max_tool_calls": 5, "max_mutation_bytes": 128},
            "constraints": {
                "success_checks": ["independent test command"],
                "expected_outputs": ["src/result.txt"],
            },
        })
        result = self._run_with_isolated_home(config)
        self.assertEqual(
            (self.workspace / "src" / "result.txt").read_text(encoding="utf-8"),
            "omega\\n",
        )
        self.assertEqual(result["tool_calls"], 4)
        self.assertEqual(len(result["mutations"]), 2)
        self.assertTrue(all(
            item["status"] == "committed" for item in result["mutations"]
        ))
        self.assertEqual(result["workspace_evidence"][0]["status"], "matched")
        handoff = build_handoff(result)
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff["verification"]["policy_compliance"], "passed")
        self.assertEqual(handoff["verification"]["workspace_readback"], "matched")
        self.assertEqual(handoff["verification"]["overall_status"], "partial")
        self.assertEqual(
            handoff["verification"]["success_checks"][0]["status"], "not_run"
        )
        self.assertEqual(
            handoff["verification"]["expected_outputs"][0]["status"],
            "not_validated",
        )
        self.assertEqual(handoff["bash_summary"]["executed_calls"], 0)

    def test_real_child_blocks_secret_read_and_out_of_scope_write(self) -> None:
        (self.workspace / ".env").write_text(
            "TOP-SECRET-MARKER", encoding="utf-8"
        )
        (self.workspace / "private").mkdir()
        (self.workspace / "src" / "allowed.txt").write_text(
            "public-marker", encoding="utf-8"
        )
        endpoint = self._endpoint([
            ("Read", {"path": ".env"}),
            ("Write", {"path": "private/out.txt", "content": "forbidden"}),
            ("Read", {"path": "src/allowed.txt"}),
        ])
        config = self._config(endpoint, {
            "permissions": {
                "tools": ["Read", "Write"],
                "read_paths": ["src/**"], "write_paths": ["src/**"],
            }
        })
        result = self._run_with_isolated_home(config)
        self.assertEqual(result["mutations"], [])
        self.assertFalse((self.workspace / "private" / "out.txt").exists())
        history = _ScriptedProvider.requests[-1]["messages"]
        outputs = [item["content"] for item in history if item["role"] == "tool"]
        self.assertIn("outside the task contract read scope", outputs[0])
        self.assertIn("outside the task contract write scope", outputs[1])
        self.assertIn("public-marker", outputs[2])
        self.assertNotIn("TOP-SECRET-MARKER", json.dumps(history))

    def test_multi_turn_tool_call_budget_rejects_third_read(self) -> None:
        (self.workspace / "src" / "input.txt").write_text(
            "seed", encoding="utf-8"
        )
        endpoint = self._endpoint([
            ("Read", {"path": "src/input.txt"}),
            ("Read", {"path": "src/input.txt"}),
            ("Read", {"path": "src/input.txt"}),
        ])
        config = self._config(endpoint, {
            "permissions": {
                "tools": ["Read"], "read_paths": ["src/**"],
            },
            "limits": {"max_tool_calls": 2},
        }, max_turns=5)
        with self.assertRaisesRegex(AgentLoopError, "tool call budget"):
            self._run_with_isolated_home(config)
        self.assertEqual(len(_ScriptedProvider.requests), 3)


if __name__ == "__main__":
    unittest.main()
