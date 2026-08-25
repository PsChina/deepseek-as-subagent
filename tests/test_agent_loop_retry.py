from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from openai import APIConnectionError

from deepseek_mcp.agent_loop import AgentLoopError, _call_with_retry
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
    return APIConnectionError(request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"))


class RetryPolicyTests(unittest.TestCase):
    def test_transient_connection_error_retries_in_outer_loop_only(self) -> None:
        sentinel = object()
        create = _Create([_connection_error(), _connection_error(), sentinel])
        client = _Client(create)
        config = Config(api_key="sk-test", workspace=httpx.URL("https://example.com"))  # type: ignore[arg-type]

        with patch("deepseek_mcp.agent_loop.time.sleep") as sleep:
            result = _call_with_retry(client, config, [], [], 0)

        self.assertIs(result, sentinel)
        self.assertEqual(create.calls, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 4.0])

    def test_final_failure_does_not_sleep_after_last_attempt(self) -> None:
        create = _Create([_connection_error(), _connection_error(), _connection_error()])
        client = _Client(create)
        config = Config(api_key="sk-test", workspace=httpx.URL("https://example.com"))  # type: ignore[arg-type]

        with patch("deepseek_mcp.agent_loop.time.sleep") as sleep:
            with self.assertRaises(AgentLoopError):
                _call_with_retry(client, config, [], [], 0)

        self.assertEqual(create.calls, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 4.0])


if __name__ == "__main__":
    unittest.main()
