"""Private stdio child that executes exactly one workspace tool call."""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
from pathlib import Path

from .config import Config, HARD_MAX_RUN_SECONDS
from .process_hardening import disable_core_dumps
from .parent_liveness import wait_for_parent_loss_or_timeout
from .resource_budget import MutationBudget, ResourceBudgetExceeded
from .tools import execute_tool
from .transaction_journal import (
    JournalUpdatePublishedWarning,
    TransactionJournalError,
    append_warning,
    record_intent,
)
from .transaction_report import bind_reporter
from .workspace_guard import bind_workspace_identity

# A 5 MB valid mutation can expand to 30 MB when every byte needs a six-byte
# JSON control-character escape. Tool results are capped at roughly 50k
# characters, whose equivalent worst-case response is below 512 KiB.
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
TERMINATION_GRACE_SECONDS = 2.0


class _ToolTermination(BaseException):
    """Interrupt the main thread so active tool finally blocks can unwind."""


def _terminate_tool(_signum, _frame) -> None:
    raise _ToolTermination()


def _install_termination_handler() -> None:
    if os.name == "posix":
        signal.signal(signal.SIGTERM, _terminate_tool)


def _timeout(value: str) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ValueError("invalid tool deadline") from None
    if not 0 < timeout <= HARD_MAX_RUN_SECONDS:
        raise ValueError("invalid tool deadline")
    return timeout


def _exit_if_stalled(
    completed: threading.Event, timeout: float, liveness: str
) -> None:
    wait_for_parent_loss_or_timeout(liveness, max(0.001, timeout))
    if completed.is_set():
        return
    if os.name != "posix":
        os._exit(124)
    os.killpg(os.getpgrp(), signal.SIGTERM)
    if not completed.wait(TERMINATION_GRACE_SECONDS):
        os._exit(124)


def _start_watchdog(
    completed: threading.Event, timeout: float, liveness: str,
) -> threading.Thread:
    mask = getattr(signal, "pthread_sigmask", None) if os.name == "posix" else None
    previous = mask(signal.SIG_BLOCK, {signal.SIGTERM}) if mask else None
    watchdog = threading.Thread(
        target=_exit_if_stalled,
        args=(completed, timeout, liveness),
        daemon=True,
        name="deepseek-tool-deadline",
    )
    try:
        watchdog.start()
    finally:
        if mask is not None:
            mask(signal.SIG_SETMASK, previous)
    return watchdog


def _read_payload() -> dict:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("tool request is too large")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("tool request is invalid")
    return payload


def _config(settings: object) -> Config:
    if not isinstance(settings, dict):
        raise ValueError("tool settings are invalid")
    values = dict(settings)
    workspace = values.get("workspace")
    if not isinstance(workspace, str):
        raise ValueError("tool workspace is invalid")
    values["workspace"] = Path(workspace)
    return Config(api_key="", **values)


def _budget(value: object) -> MutationBudget:
    if not isinstance(value, dict):
        raise ValueError("tool mutation budget is invalid")
    limit, used = value.get("limit"), value.get("used")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in (limit, used)):
        raise ValueError("tool mutation budget is invalid")
    if limit < 0 or used < 0 or used > limit:
        raise ValueError("tool mutation budget is invalid")
    return MutationBudget(limit=limit, used=used)


def _execute(payload: dict, lease_fd: int | None) -> dict:
    name, arguments = payload.get("name"), payload.get("arguments")
    if not isinstance(name, str) or not isinstance(arguments, dict):
        raise ValueError("tool call is invalid")
    max_bash_timeout = payload.get("max_bash_timeout")
    if max_bash_timeout is not None and (
        isinstance(max_bash_timeout, bool) or not isinstance(max_bash_timeout, int)
    ):
        raise ValueError("tool timeout is invalid")
    transaction_id = payload.get("transaction_id")
    if (
        not isinstance(transaction_id, str)
        or len(transaction_id) != 32
        or any(char not in "0123456789abcdef" for char in transaction_id)
    ):
        raise ValueError("tool transaction is invalid")
    os.environ["DEEPSEEK_TOOL_TRANSACTION_ID"] = transaction_id
    budget = _budget(payload.get("mutation_budget"))
    config = _config(payload.get("config"))
    assert config.expected_workspace_identity is not None
    with (
        bind_workspace_identity(config.expected_workspace_identity),
        bind_reporter(
            lambda digest: _persist_mutation_ready(
                config, transaction_id, name, arguments, digest
            ),
            lambda detail: _persist_mutation_warning(
                config, transaction_id, detail
            ),
        ),
    ):
        result = execute_tool(
            name,
            arguments,
            config,
            execution_lease_fd=lease_fd,
            mutation_budget=budget,
            max_bash_timeout=max_bash_timeout,
        )
    return {"kind": "ok", "result": result, "mutation_used": budget.used}


def _persist_mutation_ready(
    config, transaction_id: str, name: str, arguments: dict, digest: bytes,
) -> None:
    try:
        record_intent(config, transaction_id, name, arguments, digest)
    except JournalUpdatePublishedWarning:
        _write_mutation_ready(digest)
        raise
    _write_mutation_ready(digest)


def _write_mutation_ready(digest: bytes) -> None:
    _write_bytes(
        json.dumps(
            {"kind": "mutation_ready", "sha256": digest.hex()},
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
    )


def _persist_mutation_warning(config, transaction_id: str, detail: str) -> None:
    try:
        append_warning(config, transaction_id, detail)
    except JournalUpdatePublishedWarning:
        _write_mutation_warning(detail)
        raise
    except TransactionJournalError:
        _write_mutation_warning(
            "post-commit recovery warning could not be persisted"
        )
        raise
    _write_mutation_warning(detail)


def _write_mutation_warning(detail: str) -> None:
    _write_bytes(
        json.dumps(
            {"kind": "mutation_warning", "detail": detail},
            separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8") + b"\n"
    )


def _write_bytes(encoded: bytes) -> None:
    view = memoryview(encoded)
    while view:
        written = os.write(1, view)
        if written <= 0:
            os._exit(125)
        view = view[written:]


def _write_payload(payload: dict) -> None:
    encoded = json.dumps(
        payload, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        encoded = b'{"kind":"error"}'
    _write_bytes(encoded)


def _lease_fd(value: str | None) -> int | None:
    if value in (None, "-1"):
        return None
    try:
        descriptor = int(value)
        os.fstat(descriptor)
    except (TypeError, ValueError, OSError):
        raise ValueError("invalid execution lease") from None
    return descriptor


def main() -> None:
    completed = threading.Event()
    try:
        timeout = _timeout(sys.argv[1])
        lease_fd = _lease_fd(sys.argv[2] if len(sys.argv) > 2 else None)
        liveness = sys.argv[3]
        disable_core_dumps()
        _install_termination_handler()
    except (IndexError, RuntimeError, ValueError):
        return
    _start_watchdog(completed, timeout, liveness)
    try:
        _write_payload(_execute(_read_payload(), lease_fd))
    except ResourceBudgetExceeded:
        _write_payload({"kind": "budget"})
    except BaseException:
        try:
            _write_payload({"kind": "error"})
        except BaseException:
            pass
    finally:
        completed.set()


if __name__ == "__main__":
    main()
