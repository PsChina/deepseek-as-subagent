"""Retry policy and run-level controls for isolated provider requests."""
from __future__ import annotations

import logging
import time
from typing import Protocol

from .hard_deadline import Deadline, remaining as deadline_remaining
from .mutation_outcome import MutationRecord

from .provider_process import (
    ProviderRequestCancelled,
    ProviderRequestDeadline,
    request_in_subprocess,
)

logger = logging.getLogger(__name__)

API_RETRY_ATTEMPTS = 2
API_RETRY_BACKOFF_SECONDS = 2.0


class AgentLoopError(Exception):
    """The delegated agent could not finish safely."""


class AgentLoopCancelled(AgentLoopError):
    """The parent cancelled the delegated agent."""


class MutationOutcomeError(AgentLoopError):
    """A safe, user-visible mutation result that must not be retried."""

    def __init__(
        self, message: str, records: tuple[MutationRecord, ...] = (),
    ) -> None:
        super().__init__(message)
        self.records = records


class MutationOutcomeCancelled(AgentLoopCancelled, MutationOutcomeError):
    """Cancellation won after a mutation may already have committed."""


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


def call_with_retry(
    config,
    messages: list[dict],
    tools: list[dict],
    turn: int,
    *,
    cancel_signal: CancellationSignal | None = None,
    deadline: Deadline | None = None,
):
    """Retry only redacted transient failures from the killable child process."""
    last_summary = "category=api"
    attempts = 1 + API_RETRY_ATTEMPTS
    for attempt in range(attempts):
        check_cancel(cancel_signal)
        response, summary, retryable = _request_once(
            config, messages, tools, cancel_signal, deadline
        )
        if response is not None:
            return response
        if not retryable:
            kind = "client" if summary == "category=client" else "API"
            raise AgentLoopError(
                f"DeepSeek {kind} error on turn {turn}: {summary}"
            ) from None
        last_summary = summary
        if attempt < API_RETRY_ATTEMPTS:
            _backoff(attempt, attempts, turn, summary, cancel_signal, deadline)
    raise AgentLoopError(
        f"DeepSeek API unreachable after {attempts} attempts on turn {turn}: "
        f"{last_summary}"
    ) from None


def _request_once(config, messages, tools, cancel_signal, deadline):
    try:
        return request_in_subprocess(
            config, messages, tools, cancel_signal, deadline
        )
    except ProviderRequestCancelled:
        raise AgentLoopCancelled(
            "DeepSeek job cancelled by parent agent"
        ) from None
    except ProviderRequestDeadline:
        raise AgentLoopError(
            f"run time budget exceeded ({config.max_run_seconds}s)"
        ) from None


def _backoff(
    attempt: int,
    attempts: int,
    turn: int,
    summary: str,
    cancel_signal: CancellationSignal | None,
    deadline: Deadline | None,
) -> None:
    wait = API_RETRY_BACKOFF_SECONDS * (attempt + 1)
    logger.warning(
        "Turn %d API transient error (attempt %d/%d): %s; retry in %.1fs",
        turn,
        attempt + 1,
        attempts,
        summary,
        wait,
    )
    remaining = remaining_seconds(deadline) if deadline is not None else wait
    wait_before_retry(min(wait, remaining), cancel_signal)


def check_cancel(cancel_signal: CancellationSignal | None) -> None:
    if cancel_signal is not None and cancel_signal.is_set():
        raise AgentLoopCancelled("DeepSeek job cancelled by parent agent")


def wait_before_retry(
    delay_seconds: float,
    cancel_signal: CancellationSignal | None,
) -> None:
    if cancel_signal is None:
        time.sleep(delay_seconds)
    elif cancel_signal.wait(delay_seconds):
        raise AgentLoopCancelled("DeepSeek job cancelled by parent agent")


def remaining_seconds(deadline: Deadline) -> float:
    remaining = deadline_remaining(deadline)
    if remaining <= 0:
        raise AgentLoopError("run time budget exceeded")
    return remaining
