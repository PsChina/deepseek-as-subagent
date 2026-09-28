from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from deepseek_mcp.agent_loop import run_agent
from deepseek_mcp.config import Config


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        self.requests.append(request)
        if len(self.requests) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-read-1",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": json.dumps({"path": "input.txt"}),
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
    def test_local_chat_completions_runs_tool_loop_without_usage_or_deepseek_key(self) -> None:
        _FakeOpenAIHandler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.shutdown)

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "input.txt").write_text("hello local model", encoding="utf-8")
            config = Config(
                api_key="local-no-auth",
                workspace=workspace,
                model="qwen3.8-27b-q3",
                base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
                allowed_tools=["Read"],
                max_turns=4,
            )

            result = run_agent("Read input.txt and report its contents", config)

        self.assertEqual(result["final_message"], "Read completed: hello local model")
        self.assertEqual(result["turns_used"], 2)
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(len(_FakeOpenAIHandler.requests), 2)
        first, second = _FakeOpenAIHandler.requests
        self.assertEqual(first["model"], "qwen3.8-27b-q3")
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


if __name__ == "__main__":
    unittest.main()
