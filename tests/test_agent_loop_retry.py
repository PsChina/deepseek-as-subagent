from __future__ import annotations

import json
import threading
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import APIConnectionError, APIError

from deepseek_mcp.agent_loop import (
    AgentLoopCancelled,
    AgentLoopError,
    MAX_OUTPUT_TOKENS_PER_REQUEST,
    MAX_PROVIDER_HISTORY_BYTES,
    MAX_TOTAL_TOKENS_PER_RUN,
    _call_with_retry,
    _execute_one_tool,
    _execute_planned_tools,
    _record_response,
    _run_turn,
    run_agent,
)
from deepseek_mcp.config import Config
from deepseek_mcp.provider_child import MAX_API_RESPONSE_BYTES, execute_request
from deepseek_mcp.provider_process import ProviderRequestDeadline
from deepseek_mcp.provider_process import _decode_response
from deepseek_mcp.mutation_outcome import mutation_record
from deepseek_mcp.provider_retry import MutationOutcomeCancelled, MutationOutcomeError
from deepseek_mcp.resource_budget import (
    MAX_TOOL_CALLS_PER_RUN,
    MAX_TOOL_CALLS_PER_TURN,
    MutationBudget,
)


class _Create:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.kwargs = []

    def __call__(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        payload = outcome.model_dump(exclude_none=False)
        return _RawResponse(json.dumps(payload).encode("utf-8"))


class _RawResponse:
    def __init__(self, payload: bytes, *, content_encoding: str = "identity"):
        self.payload = payload
        self.headers = {"content-encoding": content_encoding}

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        pass

    def iter_bytes(self):
        yield self.payload


class _Completions:
    def __init__(self, create):
        self.with_streaming_response = SimpleNamespace(create=create)


class _Chat:
    def __init__(self, create):
        self.completions = _Completions(create)


class _Client:
    def __init__(self, create):
        self.chat = _Chat(create)


class _RecordingCancelSignal:
    def __init__(self) -> None:
        self.waiting = threading.Event()
        self._cancelled = threading.Event()

    def is_set(self) -> bool:
        return self._cancelled.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        self.waiting.set()
        return self._cancelled.wait(timeout)

    def cancel(self) -> None:
        self._cancelled.set()


def _connection_error() -> APIConnectionError:
    return APIConnectionError(
        request=httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    )


def _provider_error(marker: str, *, status: int | None = None) -> APIError:
    error = APIError(
        marker,
        request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
        body={"detail": marker},
    )
    if status is not None:
        error.status_code = status
    return error


def _exception_chain_text(error: BaseException) -> str:
    parts: list[str] = []
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        parts.extend((str(current), repr(current)))
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
    parts.extend(traceback.format_exception(error))
    return "\n".join(parts)


def _config() -> Config:
    return Config(
        api_key="sk-test", workspace=Path.cwd(), allowed_tools=["Read"]
    )


def _final_response():
    message = SimpleNamespace(content="done", tool_calls=None)
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        choices=[SimpleNamespace(message=message)],
        model_dump=lambda exclude_none=True: {
            "choices": [{"message": {"role": "assistant", "content": "done"}}]
        },
    )


def _tool_call(name: str = "Read", arguments: str = '{}'):
    return SimpleNamespace(
        id="tool-id",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _tool_response(name: str = "Write", arguments: str = '{}'):
    tool_call = _tool_call(name, arguments)
    message = SimpleNamespace(content=None, tool_calls=[tool_call])
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        choices=[SimpleNamespace(message=message)],
        model_dump=lambda exclude_none=True: {
            "choices": [{"message": {
                "role": "assistant", "content": "", "tool_calls": []
            }}]
        },
    )


def _tool_state(*, tool_calls: int = 0, deadline: float = 10_000.0):
    return SimpleNamespace(
        config=_config(),
        controls=SimpleNamespace(cancel=None, poll=None),
        messages=[],
        execution_lease_fd=None,
        mutation_budget=MutationBudget(),
        tool_calls=tool_calls,
        deadline=deadline,
        prompt_tokens=0,
        completion_tokens=0,
    )


class RetryPolicyTests(unittest.TestCase):
    def test_thinking_tool_request_omits_tool_choice_and_caps_output(self) -> None:
        create = _Create([_final_response(), _final_response()])
        tools = [{"type": "function", "function": {"name": "Read"}}]
        settings = {
            "credential": "placeholder",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-pro",
        }

        with patch("deepseek_mcp.provider_child.OpenAI") as openai:
            openai.return_value.chat.completions.with_streaming_response.create = create
            first = execute_request(settings, [], tools, 10)
            second = execute_request(settings, [], [], 10)

        self.assertNotIn("tool_choice", create.kwargs[0])
        self.assertEqual(create.kwargs[0]["max_tokens"], MAX_OUTPUT_TOKENS_PER_REQUEST)
        self.assertEqual(create.kwargs[0]["tools"], tools)
        self.assertNotIn("tools", create.kwargs[1])
        self.assertNotIn("tool_choice", create.kwargs[1])
        self.assertEqual(first["kind"], "ok")
        self.assertEqual(second["kind"], "ok")

    def test_provider_response_is_streamed_under_a_decoded_byte_cap(self) -> None:
        settings = {
            "credential": "placeholder",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-pro",
        }
        with patch("deepseek_mcp.provider_child.OpenAI") as openai:
            create = openai.return_value.chat.completions.with_streaming_response.create
            create.return_value = _RawResponse(b"x" * (MAX_API_RESPONSE_BYTES + 1))
            payload = execute_request(settings, [], [], 10)

        self.assertEqual(payload["kind"], "error")
        self.assertEqual(payload["summary"], "category=client")

    def test_provider_rejects_encoded_response_before_decoding_body(self) -> None:
        settings = {
            "credential": "placeholder",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-pro",
        }
        raw = _RawResponse(b"compressed", content_encoding="gzip")
        with patch("deepseek_mcp.provider_child.OpenAI") as openai:
            create = openai.return_value.chat.completions.with_streaming_response.create
            create.return_value = raw
            payload = execute_request(settings, [], [], 10)

        self.assertEqual(payload["kind"], "error")
        self.assertEqual(payload["summary"], "category=client")

    def test_thinking_tool_history_keeps_reasoning_and_non_null_content(self) -> None:
        state = _tool_state()
        tool_call = _tool_call()
        message = SimpleNamespace(content=None, tool_calls=[tool_call])
        response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            choices=[SimpleNamespace(message=message)],
            model_dump=lambda exclude_none=True: {
                "choices": [{"message": {
                    "role": "assistant",
                    "reasoning_content": "private reasoning",
                    "tool_calls": [{"id": "tool-id"}],
                }}]
            },
        )

        _record_response(state, response)

        self.assertEqual(state.messages[0]["content"], "")
        self.assertEqual(state.messages[0]["reasoning_content"], "private reasoning")

    def test_total_token_cap_prevents_another_provider_request(self) -> None:
        state = _tool_state(deadline=time.monotonic() + 10)
        state.prompt_tokens = MAX_TOTAL_TOKENS_PER_RUN
        with (
            patch("deepseek_mcp.agent_loop._call_with_retry") as provider,
            self.assertRaisesRegex(AgentLoopError, "token budget"),
        ):
            _run_turn(state, 1)
        provider.assert_not_called()

    def test_near_cap_request_is_rejected_before_provider_cost(self) -> None:
        state = _tool_state(deadline=time.monotonic() + 10)
        state.budget_tokens = 200_000
        state.messages = [{"role": "user", "content": "x" * 800_000}]
        with (
            patch("deepseek_mcp.agent_loop._call_with_retry") as provider,
            self.assertRaisesRegex(AgentLoopError, "cannot cover"),
        ):
            _run_turn(state, 1)
        provider.assert_not_called()

    def test_missing_usage_fails_closed(self) -> None:
        state = _tool_state(deadline=time.monotonic() + 10)
        response = _final_response()
        response.usage = None

        with self.assertRaisesRegex(AgentLoopError, "missing token usage"):
            _record_response(state, response)

    def test_local_metering_rejects_implausibly_low_provider_usage(self) -> None:
        state = _tool_state(deadline=time.monotonic() + 10)
        state.messages = [{"role": "user", "content": "x" * 600_000}]
        response = _final_response()

        _record_response(state, response, request_bytes=600_000)
        with self.assertRaisesRegex(AgentLoopError, "token budget"):
            _record_response(state, response, request_bytes=600_000)

    def test_conversation_history_has_an_independent_byte_cap(self) -> None:
        state = _tool_state(deadline=time.monotonic() + 10)
        state.messages = [
            {"role": "user", "content": "x" * (MAX_PROVIDER_HISTORY_BYTES + 1)}
        ]
        with (
            patch("deepseek_mcp.agent_loop._call_with_retry") as provider,
            self.assertRaisesRegex(AgentLoopError, "history budget"),
        ):
            _run_turn(state, 0)
        provider.assert_not_called()

    def test_per_turn_tool_call_cap_rejects_entire_batch(self) -> None:
        state = _tool_state()
        batch = [_tool_call() for _ in range(MAX_TOOL_CALLS_PER_TURN + 1)]
        with (
            patch("deepseek_mcp.agent_loop._execute_one_tool") as execute,
            self.assertRaisesRegex(AgentLoopError, "per turn"),
        ):
            _execute_planned_tools(state, batch, 0)
        execute.assert_not_called()
        self.assertEqual(state.tool_calls, 0)

    def test_steering_cannot_bypass_per_turn_tool_call_cap(self) -> None:
        state = _tool_state()
        state.controls.poll = lambda: ["new direction"]
        batch = [_tool_call() for _ in range(MAX_TOOL_CALLS_PER_TURN + 1)]
        with self.assertRaisesRegex(AgentLoopError, "per turn"):
            _execute_planned_tools(state, batch, 0)

        self.assertEqual(state.messages, [])
        self.assertEqual(state.tool_calls, 0)

    def test_per_run_tool_call_cap_rejects_entire_cross_turn_batch(self) -> None:
        state = _tool_state(tool_calls=MAX_TOOL_CALLS_PER_RUN - 1)
        with (
            patch("deepseek_mcp.agent_loop._execute_one_tool") as execute,
            self.assertRaisesRegex(AgentLoopError, "per run"),
        ):
            _execute_planned_tools(state, [_tool_call(), _tool_call()], 4)
        execute.assert_not_called()
        self.assertEqual(state.tool_calls, MAX_TOOL_CALLS_PER_RUN - 1)

    def test_mutation_budget_stops_before_over_budget_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                "sk-test", Path(tmpdir), allowed_tools=["Read", "Write"]
            )
            budget = MutationBudget(limit=10)
            first = _tool_call("Write", '{"path":"first","content":"123456"}')
            second = _tool_call("Write", '{"path":"second","content":"12345"}')

            self.assertTrue(
                _execute_one_tool(config, first, 0, mutation_budget=budget).startswith("OK:")
            )
            with self.assertRaisesRegex(AgentLoopError, "mutation output budget"):
                _execute_one_tool(config, second, 0, mutation_budget=budget)

            self.assertEqual(budget.used, 6)
            self.assertEqual((Path(tmpdir) / "first").read_bytes(), b"123456")
            self.assertFalse((Path(tmpdir) / "second").exists())

    def test_failed_mutation_reservation_is_never_refunded(self) -> None:
        budget = MutationBudget(limit=10)
        with self.assertRaisesRegex(RuntimeError, "after commit"):
            with budget.reserve(7):
                raise RuntimeError("after commit")
        self.assertEqual(budget.used, 7)

    def test_deadline_bounds_api_request_and_prevents_expired_call(self) -> None:
        response = object()
        with patch(
            "deepseek_mcp.provider_retry.request_in_subprocess",
            return_value=(response, "", False),
        ) as request:
            self.assertIs(
                _call_with_retry(_config(), [], [], 0, deadline=100.0),
                response,
            )
        self.assertEqual(request.call_args.args[-1], 100.0)

        with (
            patch(
                "deepseek_mcp.provider_retry.request_in_subprocess",
                side_effect=ProviderRequestDeadline("expired"),
            ) as expired,
            self.assertRaisesRegex(AgentLoopError, "time budget"),
        ):
            _call_with_retry(_config(), [], [], 0, deadline=100.0)
        expired.assert_called_once()

    def test_tool_logging_never_persists_argument_names_or_values(self) -> None:
        tool_call = SimpleNamespace(
            function=SimpleNamespace(
                name="Read",
                arguments='{"path":"short-secret-marker","token":"another-marker"}',
            )
        )

        with (
            patch("deepseek_mcp.agent_loop.execute_tool", return_value="OK"),
            self.assertLogs("deepseek_mcp.agent_loop", level="INFO") as logs,
        ):
            self.assertEqual(_execute_one_tool(_config(), tool_call, 0), "OK")

        output = "\n".join(logs.output)
        self.assertNotIn("short-secret-marker", output)
        self.assertNotIn("another-marker", output)
        self.assertNotIn("token", output)
        self.assertIn("arg_count=2", output)

    def test_non_object_tool_arguments_return_protocol_error(self) -> None:
        tool_call = SimpleNamespace(
            function=SimpleNamespace(name="Read", arguments='["README.md"]')
        )

        with patch("deepseek_mcp.agent_loop.execute_tool") as execute_tool:
            result = _execute_one_tool(_config(), tool_call, 0)

        self.assertEqual(result, "ERROR: tool arguments must be a JSON object")
        execute_tool.assert_not_called()

    def test_tool_execution_receives_workspace_lease_descriptor(self) -> None:
        tool_call = SimpleNamespace(
            function=SimpleNamespace(name="Read", arguments='{"path":"README.md"}')
        )

        with patch("deepseek_mcp.agent_loop.execute_tool", return_value="OK") as execute:
            result = _execute_one_tool(
                _config(),
                tool_call,
                0,
                execution_lease_fd=23,
            )

        self.assertEqual(result, "OK")
        execute.assert_called_once_with(
            "Read",
            {"path": "README.md"},
            _config(),
            execution_lease_fd=23,
        )

    def test_run_agent_uses_the_isolated_provider_boundary(self) -> None:
        config = _config()
        with patch(
            "deepseek_mcp.agent_loop._call_with_retry",
            return_value=_final_response(),
        ) as provider:
            result = run_agent("test", config)

        self.assertEqual(result["final_message"], "done")
        self.assertIs(provider.call_args.args[0], config)
        self.assertEqual(provider.call_args.args[1][1]["content"], "test")

    def test_run_agent_propagates_provider_failure_without_a_parent_client(self) -> None:
        with (
            patch(
                "deepseek_mcp.agent_loop._call_with_retry",
                side_effect=AgentLoopError("loop failed"),
            ),
            self.assertRaisesRegex(AgentLoopError, "loop failed"),
        ):
            run_agent("test", _config())

    def test_post_commit_warning_is_forced_into_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config("sk-test", Path(tmpdir), allowed_tools=["Write"])

            def execute(*args, **kwargs):
                (kwargs.get("outcome_reporter") or args[8])(
                    mutation_record("a" * 32, "Write", "committed", "durability")
                )
                return "OK: wrote; WARNING: durability; DO NOT RETRY"

            with (
                patch(
                    "deepseek_mcp.agent_loop._call_with_retry",
                    side_effect=[_tool_response(), _final_response()],
                ),
                patch(
                    "deepseek_mcp.agent_loop.execute_in_subprocess",
                    side_effect=execute,
                ),
            ):
                result = run_agent("test", config)

        self.assertIn("transaction safety", result["final_message"])
        self.assertIn("durability", result["final_message"])
        self.assertEqual(result["mutations"][0]["transaction_id"], "a" * 32)

    def test_clean_mutation_recovery_notice_leads_the_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config("sk-test", Path(tmpdir), allowed_tools=["Write"])

            def execute(*args, **kwargs):
                (kwargs.get("outcome_reporter") or args[8])(
                    mutation_record("e" * 32, "Write", "committed")
                )
                return "OK: wrote"

            with (
                patch(
                    "deepseek_mcp.agent_loop._call_with_retry",
                    side_effect=[_tool_response(), _final_response()],
                ),
                patch(
                    "deepseek_mcp.agent_loop.execute_in_subprocess",
                    side_effect=execute,
                ),
            ):
                result = run_agent("test", config)

        message = result["final_message"]
        self.assertTrue(message.startswith("[deepseek-mcp recovery required]"))
        self.assertIn("get_deepseek_recovery", message)
        self.assertIn("acknowledge_deepseek_mutations", message)
        self.assertIn("e" * 32, message)

    def test_later_provider_failure_preserves_previous_mutation_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config("sk-test", Path(tmpdir), allowed_tools=["Write"])

            def execute(*args, **kwargs):
                (kwargs.get("outcome_reporter") or args[8])(
                    mutation_record("b" * 32, "Write", "committed")
                )
                return "OK: wrote"

            with (
                patch(
                    "deepseek_mcp.agent_loop._call_with_retry",
                    side_effect=[_tool_response(), AgentLoopError("provider failed")],
                ),
                patch(
                    "deepseek_mcp.agent_loop.execute_in_subprocess",
                    side_effect=execute,
                ),
                self.assertRaises(MutationOutcomeError) as raised,
            ):
                run_agent("test", config)

        self.assertIn("DO NOT RETRY", str(raised.exception))
        self.assertIn("b" * 32, str(raised.exception))

    def test_unexpected_failure_after_mutation_is_also_non_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config("sk-test", Path(tmpdir), allowed_tools=["Write"])

            def execute(*args, **kwargs):
                (kwargs.get("outcome_reporter") or args[8])(
                    mutation_record("c" * 32, "Write", "committed")
                )
                return "OK: wrote"

            with (
                patch(
                    "deepseek_mcp.agent_loop._call_with_retry",
                    side_effect=[_tool_response(), RuntimeError("unexpected")],
                ),
                patch(
                    "deepseek_mcp.agent_loop.execute_in_subprocess",
                    side_effect=execute,
                ),
                self.assertRaises(MutationOutcomeError),
            ):
                run_agent("test", config)

    def test_cancel_after_clean_mutation_is_non_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config("sk-test", Path(tmpdir), allowed_tools=["Write"])
            cancel = threading.Event()

            def execute(*args, **kwargs):
                (kwargs.get("outcome_reporter") or args[8])(
                    mutation_record("d" * 32, "Write", "committed")
                )
                cancel.set()
                return "OK: wrote"

            with (
                patch(
                    "deepseek_mcp.agent_loop._call_with_retry",
                    return_value=_tool_response(),
                ),
                patch(
                    "deepseek_mcp.agent_loop.execute_in_subprocess",
                    side_effect=execute,
                ),
                self.assertRaises(MutationOutcomeCancelled),
            ):
                run_agent("test", config, cancel_signal=cancel)

    def test_transient_connection_error_retries_in_outer_loop_only(self) -> None:
        sentinel = object()
        outcomes = [
            (None, "category=connection", True),
            (None, "category=connection", True),
            (sentinel, "", False),
        ]

        with (
            patch(
                "deepseek_mcp.provider_retry.request_in_subprocess",
                side_effect=outcomes,
            ) as request,
            patch("deepseek_mcp.provider_retry.time.sleep") as sleep,
        ):
            result = _call_with_retry(_config(), [], [], 0)

        self.assertIs(result, sentinel)
        self.assertEqual(request.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 4.0])

    def test_api_error_text_is_not_exposed_to_host_or_logs(self) -> None:
        marker = "provider-response-secret-marker"
        error = _provider_error(marker)
        create = _Create([error])
        settings = {
            "credential": "placeholder",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-pro",
        }
        with patch("deepseek_mcp.provider_child.OpenAI") as openai:
            openai.return_value.chat.completions.with_streaming_response.create = create
            payload = execute_request(settings, [], [], 10)

        with (
            patch(
                "deepseek_mcp.provider_retry.request_in_subprocess",
                return_value=(None, payload["summary"], payload["retryable"]),
            ),
            self.assertRaises(AgentLoopError) as raised,
        ):
            _call_with_retry(_config(), [], [], 0)

        self.assertNotIn(marker, repr(payload))
        self.assertNotIn(marker, _exception_chain_text(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        self.assertIn("category=api", str(raised.exception))

    def test_retry_failure_drops_provider_body_from_logs_and_exception_chain(self) -> None:
        marker = "retry-provider-body-secret-marker"
        outcome = (None, "category=api status=503", True)

        with (
            self.assertLogs("deepseek_mcp.provider_retry", level="WARNING") as logs,
            patch(
                "deepseek_mcp.provider_retry.request_in_subprocess",
                side_effect=[outcome, outcome, outcome],
            ),
            patch("deepseek_mcp.provider_retry.time.sleep"),
            self.assertRaises(AgentLoopError) as raised,
        ):
            _call_with_retry(_config(), [], [], 0)

        self.assertNotIn(marker, "\n".join(logs.output))
        self.assertNotIn(marker, _exception_chain_text(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        self.assertIn("category=api status=503", str(raised.exception))

    def test_insufficient_system_resources_retry_then_recover(self) -> None:
        interrupted = json.dumps({
            "kind": "ok",
            "response": {
                "choices": [{
                    "finish_reason": "insufficient_system_resource",
                    "message": {"role": "assistant", "content": "partial"},
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        }).encode()
        recovered = json.dumps({
            "kind": "ok",
            "response": {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "done"},
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        }).encode()
        payloads = iter((interrupted, interrupted, recovered))

        with (
            patch(
                "deepseek_mcp.provider_retry.request_in_subprocess",
                side_effect=lambda *_args: _decode_response(next(payloads)),
            ) as request,
            patch("deepseek_mcp.provider_retry.time.sleep") as sleep,
        ):
            response = _call_with_retry(_config(), [], [], 0)

        self.assertEqual(response.choices[0].message.content, "done")
        self.assertEqual(request.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 4.0])

    def test_unknown_client_failure_has_no_raw_exception_chain(self) -> None:
        marker = "unknown-client-secret-marker"

        with (
            patch(
                "deepseek_mcp.provider_retry.request_in_subprocess",
                return_value=(None, "category=client", False),
            ),
            self.assertRaises(AgentLoopError) as raised,
        ):
            _call_with_retry(_config(), [], [], 0)

        self.assertNotIn(marker, _exception_chain_text(raised.exception))
        self.assertIsNone(raised.exception.__context__)
        self.assertEqual(
            str(raised.exception),
            "DeepSeek client error on turn 0: category=client",
        )

    def test_final_failure_does_not_sleep_after_last_attempt(self) -> None:
        outcome = (None, "category=connection", True)

        with (
            patch(
                "deepseek_mcp.provider_retry.request_in_subprocess",
                side_effect=[outcome, outcome, outcome],
            ) as request,
            patch("deepseek_mcp.provider_retry.time.sleep") as sleep,
            self.assertRaises(AgentLoopError),
        ):
            _call_with_retry(_config(), [], [], 0)

        self.assertEqual(request.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 4.0])

    def test_cancel_wakes_retry_backoff_before_another_api_attempt(self) -> None:
        marker = "cancelled-retry-provider-secret-marker"
        cancel_signal = _RecordingCancelSignal()
        errors: list[BaseException] = []

        def invoke() -> None:
            try:
                _call_with_retry(
                    _config(),
                    [],
                    [],
                    0,
                    cancel_signal=cancel_signal,
                )
            except BaseException as error:
                errors.append(error)

        outcome = (None, "category=api status=503", True)
        with patch(
            "deepseek_mcp.provider_retry.request_in_subprocess",
            side_effect=[outcome, (object(), "", False)],
        ) as request:
            worker = threading.Thread(target=invoke)
            worker.start()
            self.assertTrue(cancel_signal.waiting.wait(1.0))
            cancel_signal.cancel()
            worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AgentLoopCancelled)
        self.assertNotIn(marker, _exception_chain_text(errors[0]))
        self.assertIsNone(errors[0].__context__)

    def test_preexisting_cancel_skips_first_api_attempt(self) -> None:
        cancel_signal = _RecordingCancelSignal()
        cancel_signal.cancel()

        with (
            patch("deepseek_mcp.provider_retry.request_in_subprocess") as request,
            self.assertRaises(AgentLoopCancelled),
        ):
            _call_with_retry(
                _config(),
                [],
                [],
                0,
                cancel_signal=cancel_signal,
            )

        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
