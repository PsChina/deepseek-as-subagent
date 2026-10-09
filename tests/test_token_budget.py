from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp import agent_loop
from deepseek_mcp.config import Config
from deepseek_mcp.mutation_outcome import mutation_record
from deepseek_mcp.provider_response import ProviderResponse
from deepseek_mcp.provider_retry import MutationOutcomeError


def _response(prompt_tokens: int | None, completion_tokens: int = 128, *, read=False):
    message = {"role": "assistant", "content": "done"}
    if read:
        message["tool_calls"] = [{
            "id": "read-file", "type": "function",
            "function": {"name": "Read", "arguments": '{"path":"doc.txt"}'},
        }]
    payload = {
        "choices": [{
            "finish_reason": "tool_calls" if read else "stop", "message": message,
        }],
    }
    if prompt_tokens is not None:
        payload["usage"] = {
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
        }
    return ProviderResponse.from_payload(payload)


def _state(*, max_output_tokens=1):
    config = Config(
        "sk-test", Path.cwd(), allowed_tools=["Read"],
        max_output_tokens=max_output_tokens,
    )
    return agent_loop._create_agent_state(
        "read documents", config, [], agent_loop._AgentControls(None, None, None), None,
    )


class TokenBudgetTests(unittest.TestCase):
    def test_large_read_history_completes_fourteen_turns(self) -> None:
        for with_usage in (True, False):
            with self.subTest(with_usage=with_usage), tempfile.TemporaryDirectory() as root:
                config = Config(
                    "sk-test", Path(root), allowed_tools=["Read"], max_turns=20,
                )

                def provider(_config, messages, tools, turn, **_kwargs):
                    encoded = json.dumps(
                        {"messages": messages, "tools": tools},
                        ensure_ascii=False, separators=(",", ":"),
                    ).encode("utf-8")
                    prompt = (len(encoded) + 3) // 4 if with_usage else None
                    return _response(prompt, read=turn < 13)

                with (
                    patch.object(agent_loop, "_call_with_retry", side_effect=provider) as request,
                    patch.object(agent_loop, "execute_in_subprocess", return_value="x" * 27_000),
                ):
                    result = agent_loop.run_agent("read documents", config)

                self.assertEqual(request.call_count, 14)
                self.assertEqual(result["turns_used"], 14)
                self.assertEqual(result["tool_calls"], 13)
                self.assertLess(result["tokens"]["total"], 1_000_000)
                if not with_usage:
                    self.assertEqual(result["tokens"]["total"], 0)

    def test_reported_usage_counts_every_prompt_and_completion(self) -> None:
        state = _state()
        response = _response(600_000, 100)
        agent_loop._record_response(state, response, request_bytes=400)

        with self.assertRaisesRegex(agent_loop.AgentLoopError, "token budget"):
            agent_loop._record_response(state, response, request_bytes=400)

        self.assertEqual(state.prompt_tokens, 1_200_000)
        self.assertEqual(state.completion_tokens, 200)

    def test_missing_usage_is_not_forgotten_when_usage_resumes(self) -> None:
        state = _state()
        agent_loop._record_response(state, _response(None), request_bytes=4_000)
        with patch.object(agent_loop, "MAX_TOTAL_TOKENS_PER_RUN", 2_500):
            with self.assertRaisesRegex(agent_loop.AgentLoopError, "token budget"):
                agent_loop._record_response(state, _response(2_000), request_bytes=400)

    def test_reported_completion_cannot_hide_underreported_prompt(self) -> None:
        state = _state()
        with patch.object(agent_loop, "MAX_TOTAL_TOKENS_PER_RUN", 1_000):
            with self.assertRaisesRegex(agent_loop.AgentLoopError, "token budget"):
                agent_loop._record_response(state, _response(1, 600), request_bytes=2_000)

    def test_request_reserves_configured_output_limit(self) -> None:
        state = _state(max_output_tokens=50)
        state.budget_tokens = 800
        with patch.object(agent_loop, "MAX_TOTAL_TOKENS_PER_RUN", 1_000):
            agent_loop._ensure_request_budget(state, request_bytes=400)
            with self.assertRaisesRegex(agent_loop.AgentLoopError, "cannot cover"):
                agent_loop._ensure_request_budget(state, request_bytes=604)

    def test_request_reserves_previous_reported_prompt(self) -> None:
        state = _state()
        agent_loop._record_response(state, _response(800, 20), request_bytes=40)
        with patch.object(agent_loop, "MAX_TOTAL_TOKENS_PER_RUN", 1_000):
            with self.assertRaisesRegex(agent_loop.AgentLoopError, "cannot cover"):
                agent_loop._ensure_request_budget(state, request_bytes=40)

    def test_tool_schemas_count_toward_request_estimate(self) -> None:
        state = _state()
        state.messages = []
        state.tools = [{"type": "function", "function": {"description": "x" * 400_000}}]
        with (
            patch.object(agent_loop, "MAX_TOTAL_TOKENS_PER_RUN", 100_000),
            patch.object(agent_loop, "_call_with_retry") as provider,
            self.assertRaisesRegex(agent_loop.AgentLoopError, "cannot cover"),
        ):
            agent_loop._run_turn(state, 0)
        provider.assert_not_called()

    def test_unicode_estimate_does_not_charge_json_escape_overhead(self) -> None:
        state = _state()
        state.messages = [{"role": "user", "content": "中" * 1_000}]
        with (
            patch.object(agent_loop, "MAX_TOTAL_TOKENS_PER_RUN", 1_000),
            patch.object(agent_loop, "_call_with_retry", return_value=_response(None)) as provider,
        ):
            result = agent_loop._run_turn(state, 0)
        provider.assert_called_once()
        self.assertEqual(result["tokens"]["total"], 0)
        self.assertLess(state.budget_tokens, 1_000)

    def test_provider_metadata_surrogate_does_not_break_utf8_estimate(self) -> None:
        state = _state()
        payload = _response(None).model_dump()
        payload["choices"][0]["message"]["provider_metadata"] = {"label": "\ud800"}
        agent_loop._record_response(state, ProviderResponse.from_payload(payload))
        with patch.object(agent_loop, "_call_with_retry", return_value=_response(None)):
            result = agent_loop._run_turn(state, 1)
        self.assertEqual(result["final_message"], "done")
        self.assertGreater(state.budget_tokens, 0)

    def test_budget_failure_after_mutation_keeps_recovery_record(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = Config(
                "sk-test", Path(root), allowed_tools=["Write"], max_output_tokens=1,
            )
            payload = _response(6_000, read=True).model_dump()
            payload["choices"][0]["message"]["tool_calls"][0]["function"] = {
                "name": "Write", "arguments": '{"path":"doc.txt","content":"x"}',
            }
            response = ProviderResponse.from_payload(payload)

            def execute(*args, **kwargs):
                (kwargs.get("outcome_reporter") or args[8])(
                    mutation_record("a" * 32, "Write", "committed"),
                )
                return "OK: wrote"

            with (
                patch.object(agent_loop, "MAX_TOTAL_TOKENS_PER_RUN", 10_000),
                patch.object(agent_loop, "_call_with_retry", return_value=response) as provider,
                patch.object(agent_loop, "execute_in_subprocess", side_effect=execute),
                self.assertRaises(MutationOutcomeError) as raised,
            ):
                agent_loop.run_agent("write doc", config)

        provider.assert_called_once()
        self.assertEqual(raised.exception.records[0].transaction_id, "a" * 32)
        self.assertIn("DO NOT RETRY", str(raised.exception))
        self.assertIn("token budget cannot cover", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
