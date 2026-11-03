"""How far a delegated run got before it stopped."""
from __future__ import annotations

import time
from typing import Any


def progress_of(state: object) -> dict[str, Any]:
    """Summarize progress using the same keys a completed result reports.

    A run that died at turn five after thirty tool calls is not the same thing
    as a run that never started, and the host cannot re-plan credibly when the
    failure payload says nothing at all.
    """
    messages = getattr(state, "messages", ()) or ()
    turns = sum(
        1 for message in messages
        if isinstance(message, dict) and message.get("role") == "assistant"
    )
    prompt = int(getattr(state, "prompt_tokens", 0))
    completion = int(getattr(state, "completion_tokens", 0))
    started = float(getattr(state, "started", 0.0))
    mutations = getattr(state, "mutations", None)
    return {
        "turns_used": turns,
        "tool_calls": int(getattr(state, "tool_calls", 0)),
        "tokens": {
            "prompt": prompt,
            "completion": completion,
            "total": prompt + completion,
        },
        "mutations": mutations.payload() if mutations is not None else [],
        "duration_seconds": round(max(0.0, time.time() - started), 2) if started else 0.0,
    }


def attach_progress(error: BaseException, state: object) -> None:
    """Record progress on the failure without changing its type or its message."""
    if isinstance(error, Exception) and not hasattr(error, "progress"):
        error.progress = progress_of(state)  # type: ignore[attr-defined]
