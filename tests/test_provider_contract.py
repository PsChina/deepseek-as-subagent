from __future__ import annotations

import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from deepseek_mcp.agent_loop import _execute_planned_tools, _record_response
from deepseek_mcp.provider_child import execute_request
from deepseek_mcp.provider_response import ProviderResponse
from deepseek_mcp.resource_budget import MutationBudget


class _RawResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.headers = {"content-encoding": "identity"}

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        pass

    def iter_bytes(self):
        yield self.payload


class ProviderContractTests(unittest.TestCase):
    def test_v4_reasoning_tool_history_is_forwarded_on_next_request(self) -> None:
        tool_payload = {
            "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "inspect the requested file first",
                        "tool_calls": [
                            {
                                "id": "call-readme",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": '{"path":"README.md"}',
                                },
                            }
                        ],
                    },
                }
            ],
        }
        response = ProviderResponse.from_payload(tool_payload)
        state = SimpleNamespace(
            config=SimpleNamespace(),
            controls=SimpleNamespace(cancel=None, poll=None),
            messages=[],
            execution_lease_fd=None,
            mutation_budget=MutationBudget(),
            tool_calls=0,
            deadline=time.monotonic() + 30,
            prompt_tokens=0,
            completion_tokens=0,
            budget_tokens=0,
            mutations=SimpleNamespace(add=lambda _record: None),
        )

        _record_response(state, response)
        with patch(
            "deepseek_mcp.agent_loop._execute_one_tool",
            return_value="README contents",
        ):
            _execute_planned_tools(
                state, response.choices[0].message.tool_calls, 0
            )

        final_payload = {
            "usage": {"prompt_tokens": 24, "completion_tokens": 4},
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "done",
                    },
                }
            ],
        }
        captured: dict = {}

        def create(**kwargs):
            captured.update(kwargs)
            return _RawResponse(json.dumps(final_payload).encode("utf-8"))

        client = MagicMock()
        client.chat.completions.with_streaming_response.create.side_effect = create
        settings = {
            "credential": "placeholder",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-pro",
        }
        tools = [{"type": "function", "function": {"name": "Read"}}]

        with patch("deepseek_mcp.provider_child.OpenAI", return_value=client):
            result = execute_request(settings, state.messages, tools, 10)

        self.assertEqual(result["kind"], "ok")
        history = captured["messages"]
        self.assertEqual(history[0]["role"], "assistant")
        self.assertEqual(
            history[0]["reasoning_content"],
            "inspect the requested file first",
        )
        self.assertEqual(history[0]["content"], "")
        self.assertEqual(history[0]["tool_calls"][0]["id"], "call-readme")
        self.assertEqual(
            history[0]["tool_calls"][0]["function"]["name"], "Read"
        )
        self.assertEqual(
            history[1],
            {
                "role": "tool",
                "tool_call_id": "call-readme",
                "content": "README contents",
            },
        )


if __name__ == "__main__":
    unittest.main()
