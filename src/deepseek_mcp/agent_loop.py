"""DeepSeek sub-agent loop with bounded tools, tokens, and wall-clock time."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import Config
from .hard_deadline import Deadline, HardDeadline
from .mutation_outcome import (
    MutationAccumulator,
    MutationRecord,
    mutation_failure_message,
)
from .provider_process import MAX_OUTPUT_TOKENS_PER_REQUEST
from .provider_retry import (
    AgentLoopCancelled,
    AgentLoopError,
    CancellationSignal,
    call_with_retry as _call_with_retry,
    check_cancel as _check_cancel,
    remaining_seconds as _remaining_seconds,
    MutationOutcomeCancelled,
    MutationOutcomeError,
)
from .resource_budget import (
    MAX_TOOL_CALLS_PER_RUN,
    MAX_TOOL_CALLS_PER_TURN,
    MutationBudget,
    ResourceBudgetExceeded,
)
from .tools import build_tool_schemas, execute_tool
from .tool_process import execute_in_subprocess

logger = logging.getLogger(__name__)

MAX_TOTAL_TOKENS_PER_RUN = 1_000_000
MAX_PROVIDER_HISTORY_BYTES = 12 * 1024 * 1024

SYSTEM_PROMPT_TEMPLATE = """You are DeepSeek working as a sub-agent for a parent coding agent.

You're given a focused task to complete autonomously within a workspace.
You have local tools: {tools}

Rules:
1. Stay strictly within the workspace: {workspace}
2. Read before editing. Don't guess file contents.
3. For batch tasks (translating, extracting, refactoring many files), iterate file-by-file.
4. When done, return a final message summarizing:
   - What you did (file paths affected)
   - Any issues / files you couldn't process
   - A brief summary the parent agent can use without re-reading everything
5. Don't ask clarifying questions back to the parent. Make reasonable assumptions
   and document them in your final message.
6. If a tool returns "ERROR: ...", read the error and decide: retry with fixed input,
   skip the file, or report and stop. Don't blindly loop on the same error.
"""


@dataclass(frozen=True)
class _AgentControls:
    poll: Callable[[], list[str]] | None
    finalize: Callable[[], list[str]] | None
    cancel: CancellationSignal | None


@dataclass
class _AgentState:
    config: Config
    tools: list[dict]
    controls: _AgentControls
    messages: list[dict]
    started: float
    deadline: Deadline
    execution_lease_fd: int | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    budget_tokens: int = 0
    tool_calls: int = 0
    mutation_budget: MutationBudget = field(default_factory=MutationBudget)
    mutations: MutationAccumulator = field(default_factory=MutationAccumulator)


def run_agent(
    task: str,
    config: Config,
    *,
    control_poll: Callable[[], list[str]] | None = None,
    control_finalize: Callable[[], list[str]] | None = None,
    cancel_signal: CancellationSignal | None = None,
    execution_lease_fd: int | None = None,
) -> dict:
    """Run until a final response or a configured safety limit is reached."""
    controls = _AgentControls(control_poll, control_finalize, cancel_signal)
    state = _create_agent_state(
        task,
        config,
        build_tool_schemas(config.allowed_tools),
        controls,
        execution_lease_fd,
    )
    try:
        for turn in range(config.max_turns):
            result = _run_turn(state, turn)
            if result is not None:
                return result
        _raise_max_turns(state)
    except (AgentLoopCancelled, AgentLoopError) as error:
        _raise_with_mutation_records(state, error)
    except Exception as error:
        if state.mutations.records:
            _raise_with_mutation_records(state, error)
        raise


def _raise_with_mutation_records(
    state: _AgentState, error: BaseException,
) -> None:
    if not state.mutations.records:
        raise error
    reason = error if isinstance(error, AgentLoopError) else "unexpected internal failure"
    message = mutation_failure_message(state.mutations.records, reason)
    error_type = (
        MutationOutcomeCancelled
        if isinstance(error, AgentLoopCancelled)
        else MutationOutcomeError
    )
    raise error_type(message, tuple(state.mutations.records)) from None


def _create_agent_state(
    task: str,
    config: Config,
    tools: list[dict],
    controls: _AgentControls,
    execution_lease_fd: int | None,
) -> _AgentState:
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        tools=", ".join(config.allowed_tools),
        workspace=config.workspace,
    )
    started = time.time()
    return _AgentState(
        config=config,
        tools=tools,
        controls=controls,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ],
        started=started,
        deadline=HardDeadline.after(config.max_run_seconds),
        execution_lease_fd=execution_lease_fd,
    )


def _run_turn(state: _AgentState, turn: int) -> dict | None:
    _remaining_seconds(state.deadline)
    _ensure_token_budget_available(state)
    _check_cancel(state.controls.cancel)
    _append_control_messages(state.messages, state.controls.poll)
    request_bytes = _enforce_history_budget(state.messages)
    _ensure_request_budget(state, request_bytes)
    response = _call_with_retry(
        state.config,
        state.messages,
        state.tools,
        turn,
        cancel_signal=state.controls.cancel,
        deadline=state.deadline,
    )
    message = _record_response(state, response, request_bytes)
    _check_cancel(state.controls.cancel)
    if not message.tool_calls:
        return _finalize_or_steer(state, message, turn)
    _execute_planned_tools(state, message.tool_calls, turn)
    return None


def _record_response(state: _AgentState, response, request_bytes: int = 0):
    usage = response.usage
    if usage is None:
        raise AgentLoopError("provider response is missing token usage")
    message = response.choices[0].message
    raw = response.model_dump(exclude_none=True)
    assistant_message = raw["choices"][0]["message"]
    if message.tool_calls and assistant_message.get("content") is None:
        assistant_message["content"] = ""
    response_bytes = _encoded_size(assistant_message)
    reported = usage.prompt_tokens + usage.completion_tokens
    state.prompt_tokens += usage.prompt_tokens
    state.completion_tokens += usage.completion_tokens
    state.budget_tokens = getattr(state, "budget_tokens", 0) + max(
        reported, request_bytes + response_bytes
    )
    if max(
        state.prompt_tokens + state.completion_tokens, state.budget_tokens
    ) > MAX_TOTAL_TOKENS_PER_RUN:
        raise AgentLoopError("run token budget exceeded")
    state.messages.append(assistant_message)
    _enforce_history_budget(state.messages)
    return message


def _ensure_token_budget_available(state: _AgentState) -> None:
    metered = max(
        state.prompt_tokens + state.completion_tokens,
        getattr(state, "budget_tokens", 0),
    )
    if metered >= MAX_TOTAL_TOKENS_PER_RUN:
        raise AgentLoopError("run token budget exhausted")


def _ensure_request_budget(state: _AgentState, request_bytes: int) -> None:
    used = max(
        state.prompt_tokens + state.completion_tokens,
        getattr(state, "budget_tokens", 0),
    )
    reserved = request_bytes + MAX_OUTPUT_TOKENS_PER_REQUEST
    if reserved > MAX_TOTAL_TOKENS_PER_RUN - used:
        raise AgentLoopError("run token budget cannot cover another provider request")


def _encoded_size(value: object) -> int:
    encoder = json.JSONEncoder(separators=(",", ":"), ensure_ascii=True)
    return sum(len(chunk.encode("utf-8")) for chunk in encoder.iterencode(value))


def _enforce_history_budget(messages: list[dict]) -> int:
    size = _encoded_size(messages)
    if size > MAX_PROVIDER_HISTORY_BYTES:
        raise AgentLoopError("provider conversation history budget exceeded")
    return size


def _finalize_or_steer(state: _AgentState, message, turn: int) -> dict | None:
    updates = _poll_control_messages(state.controls.finalize or state.controls.poll)
    if updates:
        _append_steering_update(state.messages, updates)
        _enforce_history_budget(state.messages)
        return None
    return _build_result(state, message.content, turn)


def _build_result(state: _AgentState, content: str | None, turn: int) -> dict:
    total_tokens = state.prompt_tokens + state.completion_tokens
    final_message = content or "(empty response)"
    notices = filter(
        None,
        (state.mutations.recovery_notice(), state.mutations.warning_notice()),
    )
    final_message = "\n\n".join((*notices, final_message))
    return {
        "final_message": final_message,
        "turns_used": turn + 1,
        "tokens": {
            "prompt": state.prompt_tokens,
            "completion": state.completion_tokens,
            "total": total_tokens,
        },
        "tool_calls": state.tool_calls,
        "duration_seconds": round(max(0.0, time.time() - state.started), 2),
        "mutations": state.mutations.payload(),
    }


def _execute_planned_tools(state: _AgentState, tool_calls, turn: int) -> None:
    _check_cancel(state.controls.cancel)
    _validate_tool_batch(state, tool_calls)
    updates = _poll_control_messages(state.controls.poll)
    if updates:
        _steer_before_tools(state, tool_calls, updates)
        return
    for index, tool_call in enumerate(tool_calls):
        _check_cancel(state.controls.cancel)
        updates = _poll_control_messages(state.controls.poll)
        if updates:
            _steer_before_tools(state, tool_calls[index:], updates)
            return
        _execute_and_record_tool(state, tool_call, turn)


def _steer_before_tools(state: _AgentState, tool_calls, updates: list[str]) -> None:
    _append_skipped_tool_responses(state.messages, tool_calls)
    _append_steering_update(state.messages, updates)
    _enforce_history_budget(state.messages)


def _execute_and_record_tool(state: _AgentState, tool_call, turn: int) -> None:
    remaining = _remaining_seconds(state.deadline)
    state.tool_calls += 1
    result = _execute_one_tool(
        state.config,
        tool_call,
        turn,
        execution_lease_fd=state.execution_lease_fd,
        mutation_budget=state.mutation_budget,
        max_bash_timeout=max(1, int(remaining)),
        cancel_signal=state.controls.cancel,
        deadline=state.deadline,
        outcome_reporter=state.mutations.add,
    )
    state.messages.append(
        {"role": "tool", "tool_call_id": tool_call.id, "content": result}
    )
    _enforce_history_budget(state.messages)
    _check_cancel(state.controls.cancel)


def _execute_one_tool(
    config: Config,
    tool_call,
    turn: int,
    *,
    execution_lease_fd: int | None = None,
    mutation_budget: MutationBudget | None = None,
    max_bash_timeout: int | None = None,
    cancel_signal: CancellationSignal | None = None,
    deadline: Deadline | None = None,
    outcome_reporter: Callable[[MutationRecord], None] | None = None,
) -> str:
    tool_name = tool_call.function.name
    try:
        args = json.loads(tool_call.function.arguments)
    except json.JSONDecodeError as error:
        return f"ERROR: invalid JSON in tool arguments: {error}"
    if not isinstance(args, dict):
        return "ERROR: tool arguments must be a JSON object"
    logged_name = tool_name if tool_name in config.allowed_tools else "<unknown>"
    logger.info("Turn %d tool_call: %s arg_count=%d", turn, logged_name, len(args))
    kwargs = _tool_execution_options(
        execution_lease_fd, mutation_budget, max_bash_timeout
    )
    try:
        if deadline is not None:
            budget = mutation_budget or MutationBudget()
            return execute_in_subprocess(
                config,
                tool_name,
                args,
                budget,
                max_bash_timeout or max(1, int(_remaining_seconds(deadline))),
                execution_lease_fd,
                cancel_signal,
                deadline,
                outcome_reporter,
            )
        return execute_tool(tool_name, args, config, **kwargs)
    except ResourceBudgetExceeded as exc:
        raise AgentLoopError(str(exc)) from None


def _tool_execution_options(execution_lease_fd, mutation_budget, max_bash_timeout):
    options = {"execution_lease_fd": execution_lease_fd}
    if mutation_budget is not None:
        options["mutation_budget"] = mutation_budget
    if max_bash_timeout is not None:
        options["max_bash_timeout"] = max_bash_timeout
    return options


def _validate_tool_batch(state: _AgentState, tool_calls) -> None:
    planned = len(tool_calls)
    if planned > MAX_TOOL_CALLS_PER_TURN:
        raise AgentLoopError(
            f"tool call batch exceeds {MAX_TOOL_CALLS_PER_TURN} per turn"
        )
    if state.tool_calls + planned > MAX_TOOL_CALLS_PER_RUN:
        raise AgentLoopError(
            f"tool call budget exceeds {MAX_TOOL_CALLS_PER_RUN} per run"
        )


def _raise_max_turns(state: _AgentState) -> None:
    raise AgentLoopError(f"Agent loop exceeded max_turns ({state.config.max_turns})")


def _poll_control_messages(
    control_poll: Callable[[], list[str]] | None,
) -> list[str]:
    if control_poll is None:
        return []
    try:
        return [message.strip() for message in control_poll() if message.strip()]
    except Exception as error:
        raise AgentLoopError("control channel failed") from error


def _append_steering_update(messages: list[dict], updates: list[str]) -> int:
    if not updates:
        return 0
    body = "\n\n".join(
        f"Update {index + 1}:\n{text}" for index, text in enumerate(updates)
    )
    messages.append(
        {
            "role": "user",
            "content": (
                "# Parent agent steering update\n"
                "The parent sent newer instructions. Apply them from this point; "
                "they override conflicting earlier task details.\n\n"
                f"{body}"
            ),
        }
    )
    logger.info("Applied %d parent steering message(s)", len(updates))
    return len(updates)


def _append_control_messages(
    messages: list[dict],
    control_poll: Callable[[], list[str]] | None,
) -> int:
    return _append_steering_update(messages, _poll_control_messages(control_poll))


def _append_skipped_tool_responses(messages: list[dict], tool_calls) -> None:
    for tool_call in tool_calls:
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": (
                    "SKIPPED: the parent sent a newer steering instruction before "
                    "this tool ran. Re-plan using the latest instruction."
                ),
            }
        )
    logger.info("Skipped %d stale tool call(s) due to parent steering", len(tool_calls))
