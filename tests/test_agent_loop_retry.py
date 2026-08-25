from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import APIConnectionError

from deepseek_mcp.agent_loop import AgentLoopError, _call_with_retry, run_agent
from deepseek_mcp.config import Config


class _Create:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _Completions:
    def __init__(self, create):
        self.create = create


class _Chat:
    def __init__(self, create):
        self.completions = _Completions(create)


class _Client:
    def __init__(self, create):
        self.chat = _Chat(create)


def _connection_error() -> APIConnectionError:
    return APIConnectionError(
        request=httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    )


def _config() -> Config:
    return Config(api_key="sk-test", workspace=Path.cwd())


def _final_response():
    message = SimpleNamespace(content="done", tool_calls=None)
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(message=message)],
        model_dump=lambda exclude_none=True: {
            "choices": [{"message": {"role": "assistant", "content": "done"}}]
        },
    )


class RetryPolicyTests(unittest.TestCase):
    def test_run_agent_disables_sdk_retries_and_sets_explicit_timeout(self) -> None:
        with (
            patch("deepseek_mcp.agent_loop.OpenAI") as openai,
            patch("deepseek_mcp.agent_loop._call_with_retry", return_value=_final_response()),
        ):
            run_agent("test", _config())

        kwargs = openai.call_args.kwargs
        self.assertEqual(kwargs["max_retries"], 0)
        self.assertIsInstance(kwargs["timeout"], httpx.Timeout)
        self.assertEqual(kwargs["timeout"].connect, 15.0)
        self.assertEqual(kwargs["timeout"].read, 180.0)

    def test_transient_connection_error_retries_in_outer_loop_only(self) -> None:
        sentinel = object()
        create = _Create([_connection_error(), _connection_error(), sentinel])
        client = _Client(create)

        with patch("deepseek_mcp.agent_loop.time.sleep") as sleep:
            result = _call_with_retry(client, _config(), [], [], 0)

        self.assertIs(result, sentinel)
        self.assertEqual(create.calls, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 4.0])

    def test_final_failure_does_not_sleep_after_last_attempt(self) -> None:
        create = _Create([_connection_error(), _connection_error(), _connection_error()])
        client = _Client(create)

        with patch("deepseek_mcp.agent_loop.time.sleep") as sleep:
            with self.assertRaises(AgentLoopError):
                _call_with_retry(client, _config(), [], [], 0)

        self.assertEqual(create.calls, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 4.0])


if __name__ == "__main__":
    unittest.main()
