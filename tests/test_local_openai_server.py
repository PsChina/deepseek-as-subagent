from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp.agent_loop import run_agent
from deepseek_mcp.config import Config


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    paths: list[str] = []
    authorizations: list[str] = []
    sequence: list[tuple[str, dict]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        self.requests.append(request)
        self.paths.append(self.path)
        self.authorizations.append(self.headers.get("Authorization", ""))
        index = len(self.requests) - 1
        if index < len(self.sequence):
            name, arguments = self.sequence[index]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call-{index}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }],
            }
            finish_reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": "Read completed: hello local model"}
            finish_reason = "stop"
        payload = {
            "id": f"chatcmpl-{len(self.requests)}",
            "object": "chat.completion",
            "created": 1,
            "model": request.get("model", "local-model"),
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }],
            # Deliberately omit usage to exercise the byte-budget fallback.
        }
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args) -> None:
        pass


class LocalOpenAIEndpointTests(unittest.TestCase):
    def _start_server(self, sequence: list[tuple[str, dict]]) -> str:
        _FakeOpenAIHandler.requests = []
        _FakeOpenAIHandler.paths = []
        _FakeOpenAIHandler.authorizations = []
        _FakeOpenAIHandler.sequence = sequence
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/v1"

    def test_local_chat_completions_runs_tool_loop_without_usage_or_deepseek_key(self) -> None:
        base_url = self._start_server([("Read", {"path": "input.txt"})])

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "input.txt").write_text("hello local model", encoding="utf-8")
            with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-private"}, clear=True), patch(
                "deepseek_mcp.config._load_data", return_value={
                    "workspace": str(workspace), "flash": "qwen3.8-27b-q3",
                    "pro": "qwen3.8-27b-q3", "base_url": base_url,
                    "allowed_tools": ["Read"], "max_output_tokens": 512,
                },
            ):
                config = Config.load()

            result = run_agent("Read input.txt and report its contents", config)

        self.assertEqual(result["final_message"], "Read completed: hello local model")
        self.assertEqual(result["turns_used"], 2)
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(len(_FakeOpenAIHandler.requests), 2)
        first, second = _FakeOpenAIHandler.requests
        self.assertEqual(first["model"], "qwen3.8-27b-q3")
        self.assertEqual(first["max_tokens"], 512)
        self.assertEqual(_FakeOpenAIHandler.paths, ["/v1/chat/completions"] * 2)
        self.assertEqual(_FakeOpenAIHandler.authorizations, ["Bearer local-no-auth"] * 2)
        self.assertIn("tools", first)
        system_prompt = first["messages"][0]["content"]
        self.assertIn("coding sub-agent", system_prompt)
        self.assertNotIn("You are DeepSeek", system_prompt)
        self.assertNotIn("reasoning_effort", first)
        self.assertNotIn("thinking", first.get("extra_body", {}))
        self.assertEqual(
            next(item["content"] for item in second["messages"] if item["role"] == "tool"),
            "hello local model",
        )
        self.assertEqual(result["tokens"]["total"], 0)

    def test_local_coding_loop_commits_and_verifies_file_changes(self) -> None:
        base_url = self._start_server([
            ("Read", {"path": "input.txt"}),
            ("Write", {"path": "output.txt", "content": "original\n"}),
            ("Edit", {"path": "output.txt", "old_string": "original", "new_string": "verified"}),
            ("Bash", {"command": "test \"$(cat output.txt)\" = verified && printf VERIFIED"}),
        ])
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            (workspace / "input.txt").write_text("hello local model", encoding="utf-8")
            config = Config("local-auth", workspace, model="local", base_url=base_url,
                            allowed_tools=["Read", "Write", "Edit", "Bash"], max_turns=6)
            result = run_agent("Create and verify output.txt", config)
            self.assertEqual((workspace / "output.txt").read_text(), "verified\n")
        self.assertEqual(result["tool_calls"], 4)
        self.assertEqual(result["turns_used"], 5)
        final_history = _FakeOpenAIHandler.requests[-1]["messages"]
        outputs = [item["content"] for item in final_history if item["role"] == "tool"]
        self.assertTrue(all(not output.startswith("ERROR:") for output in outputs), outputs)
        self.assertIn("VERIFIED", outputs[-1])
        self.assertEqual(_FakeOpenAIHandler.authorizations, ["Bearer local-auth"] * 5)

    def test_readonly_local_loop_rejects_model_requested_write(self) -> None:
        base_url = self._start_server([("Write", {"path": "forbidden.txt", "content": "x"})])
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            config = Config("local", workspace, model="local", base_url=base_url,
                            allowed_tools=["Read"], delegation_capability="readonly")
            run_agent("Inspect only", config)
            self.assertFalse((workspace / "forbidden.txt").exists())
        history = _FakeOpenAIHandler.requests[-1]["messages"]
        self.assertTrue(next(item["content"] for item in history if item["role"] == "tool").startswith("ERROR:"))


if __name__ == "__main__":
    unittest.main()
