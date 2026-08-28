"""Killable subprocess boundary for one local workspace tool call."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass
from typing import Callable

from .child_runtime import (
    child_working_directory,
    isolated_child_argv,
    sanitized_python_environment,
)
from .provider_process import (
    CONTROL_CHECK_SECONDS,
    PROCESS_STOP_SECONDS,
    _close_process_pipes,
    _stop_process,
)
from .provider_retry import (
    AgentLoopCancelled,
    AgentLoopError,
    MutationOutcomeCancelled,
    MutationOutcomeError,
)
from .file_identity import ToolInputError
from .file_io import read_workspace_text
from .hard_deadline import Deadline, remaining as deadline_remaining
from .lease_inheritance import ChildLeaseAnchor
from .parent_liveness import ParentLiveness, close_parent_liveness
from .resource_budget import MutationBudget, ResourceBudgetExceeded
from .mutation_outcome import MutationRecord, mutation_record
from .tool_child import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES
from .tool_cleanup import cleanup_tool_artifacts
from .workspace_guard import bind_workspace_identity, require_workspace_identity

_TOOL_ENVIRONMENT = (
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PATH",
    "SYSTEMROOT", "WINDIR",
    "DOCKER_CONTEXT", "DOCKER_HOST", "CONTAINER_HOST",
)


@dataclass
class _Communication:
    output: bytes = b""
    error: bool = False


@dataclass
class _ActiveTool:
    transaction_id: str
    process: subprocess.Popen[bytes]
    state: _Communication
    ready: threading.Event
    communication: threading.Thread


def _environment() -> dict[str, str]:
    values = {name: os.environ[name] for name in _TOOL_ENVIRONMENT if name in os.environ}
    values.setdefault("PATH", os.defpath)
    return sanitized_python_environment(values)


def _config_payload(config) -> dict:
    return {
        "workspace": str(config.workspace),
        "model": config.model,
        "max_turns": config.max_turns,
        "allowed_tools": list(config.allowed_tools),
        "base_url": config.base_url,
        "max_run_seconds": config.max_run_seconds,
        "bash_backend": config.bash_backend,
        "bash_runtime": config.bash_runtime,
        "bash_image": config.bash_image,
        "bash_memory": config.bash_memory,
        "bash_cpus": config.bash_cpus,
        "bash_pids_limit": config.bash_pids_limit,
        "expected_workspace_identity": config.expected_workspace_identity,
    }


def _encoded_request(
    config, name: str, arguments: dict, budget: MutationBudget,
    max_bash_timeout: int, transaction_id: str,
) -> bytes:
    payload = {
        "config": _config_payload(config),
        "name": name,
        "arguments": arguments,
        "mutation_budget": {"limit": budget.limit, "used": budget.used},
        "max_bash_timeout": max_bash_timeout,
        "transaction_id": transaction_id,
    }
    encoded = json.dumps(
        payload, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise AgentLoopError("tool request input exceeds its hard limit")
    return encoded


def _child_options(
    liveness: ParentLiveness,
    inherited: int | None,
    lease_anchor: ChildLeaseAnchor,
) -> dict:
    descriptors = list(liveness.inherited_fds)
    if inherited is not None:
        try:
            os.fstat(inherited)
        except OSError:
            raise AgentLoopError(
                "workspace execution lease is unavailable"
            ) from None
        descriptors.append(inherited)
    if os.name == "posix":
        return {"pass_fds": tuple(descriptors)}
    if lease_anchor.startupinfo is not None:
        return {"startupinfo": lease_anchor.startupinfo}
    return {}


def _start_tool(timeout: float, lease_fd: int | None) -> subprocess.Popen[bytes]:
    liveness = ParentLiveness.create()
    inherited = lease_fd if os.name == "posix" else None
    lease_anchor = ChildLeaseAnchor()
    process: subprocess.Popen[bytes] | None = None
    try:
        lease_anchor = ChildLeaseAnchor.create(lease_fd)
        arguments = (
            f"{timeout:.6f}",
            str(inherited if inherited is not None else -1),
            liveness.argument,
        )
        options = _child_options(liveness, inherited, lease_anchor)
        process = subprocess.Popen(
            isolated_child_argv("deepseek_mcp.tool_child", *arguments),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_environment(),
            cwd=child_working_directory(),
            shell=False,
            close_fds=True,
            start_new_session=True,
            **options,
        )
        lease_anchor.close_parent_copy()
        liveness.attach(process)
        return process
    except BaseException:
        if process is not None:
            _stop_process(process)
            _close_process_pipes(process)
        liveness.close()
        try:
            lease_anchor.close_parent_copy()
        except OSError:
            pass
        raise


def _communicate(
    process: subprocess.Popen[bytes], request: bytes, state: _Communication,
    ready: threading.Event,
) -> None:
    try:
        stdout, _ = process.communicate(request)
        if len(stdout) <= MAX_RESPONSE_BYTES:
            state.output = stdout
        else:
            state.error = True
    except BaseException:
        state.error = True
    finally:
        ready.set()


def _decode(raw: bytes, budget: MutationBudget) -> str:
    try:
        payload = json.loads(raw.splitlines()[-1])
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        raise AgentLoopError("tool process returned an invalid response") from None
    if not isinstance(payload, dict):
        raise AgentLoopError("tool process returned an invalid response")
    if payload.get("kind") == "budget":
        raise ResourceBudgetExceeded(
            f"mutation output budget exceeded ({budget.limit} bytes per run)"
        )
    result, used = payload.get("result"), payload.get("mutation_used")
    if payload.get("kind") != "ok" or not isinstance(result, str):
        raise AgentLoopError("tool process failed")
    if isinstance(used, bool) or not isinstance(used, int):
        raise AgentLoopError("tool process returned an invalid budget")
    if not budget.used <= used <= budget.limit:
        raise AgentLoopError("tool process returned an invalid budget")
    budget.used = used
    return result


def _mutation_digest(raw: bytes) -> bytes | None:
    for line in raw.splitlines():
        try:
            payload = json.loads(line)
            digest = payload.get("sha256")
            if payload.get("kind") == "mutation_ready" and isinstance(digest, str):
                if len(digest) == 64:
                    return bytes.fromhex(digest)
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def _mutation_warning(raw: bytes) -> str | None:
    warnings: list[str] = []
    for line in raw.splitlines():
        try:
            payload = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if (
            isinstance(payload, dict)
            and payload.get("kind") == "mutation_warning"
            and isinstance(detail, str)
        ):
            if detail not in warnings:
                warnings.append(detail)
    return "; ".join(warnings) or None


def _interrupted_mutation_state(config, name: str, arguments: dict, raw: bytes) -> str | None:
    digest = _mutation_digest(raw)
    if digest is None or name not in {"Write", "Edit", "NotebookEdit"}:
        return None
    label = arguments.get("path")
    if not isinstance(label, str) or not label:
        return "uncertain"
    try:
        assert config.expected_workspace_identity is not None
        with bind_workspace_identity(config.expected_workspace_identity):
            _text, identity = read_workspace_text(config.workspace, label)
    except Exception:
        return "uncertain"
    return "committed" if identity.digest == digest else "uncertain"


def _wait_for_tool(ready: threading.Event, cancel_signal, deadline: Deadline) -> None:
    while not ready.wait(CONTROL_CHECK_SECONDS):
        if cancel_signal is not None and cancel_signal.is_set():
            raise AgentLoopCancelled("tool execution cancelled")
        if deadline_remaining(deadline) <= 0:
            raise AgentLoopError("run time budget exceeded")


def _cleanup_tool_process(
    process: subprocess.Popen[bytes],
    communication: threading.Thread,
    started: bool,
    config,
    name: str,
    arguments: dict,
    transaction_id: str,
) -> None:
    try:
        if not _stop_process(process):
            raise AgentLoopError("tool process cleanup failed")
        if started:
            communication.join(PROCESS_STOP_SECONDS)
        if started and communication.is_alive():
            raise AgentLoopError("tool pipe cleanup failed")
    finally:
        try:
            close_parent_liveness(process)
        finally:
            _close_process_pipes(process)
    try:
        cleanup_tool_artifacts(config, name, arguments, transaction_id)
    except (OSError, RuntimeError, ToolInputError):
        raise AgentLoopError("tool artifact cleanup failed") from None


def _cleanup_after_result(
    process, communication, started, config, name, arguments,
    transaction_id: str, result: str | None,
) -> bool:
    try:
        _cleanup_tool_process(
            process, communication, started, config, name, arguments,
            transaction_id,
        )
    except AgentLoopError:
        committed = name in {"Write", "Edit", "NotebookEdit"}
        if not committed or result is None or not result.startswith("OK:"):
            raise
        return False
    return True


def _interrupted_error(
    failure: BaseException, record: MutationRecord,
) -> AgentLoopError:
    message = (
        f"mutation outcome requires review; {record.summary()}; call "
        "get_deepseek_recovery, verify the file, then call "
        "acknowledge_deepseek_mutations; DO NOT RETRY"
    )
    error_type = (
        MutationOutcomeCancelled
        if isinstance(failure, AgentLoopCancelled)
        else MutationOutcomeError
    )
    return error_type(message, (record,))


def _receive_tool_result(
    process, communication: threading.Thread, ready: threading.Event,
    cancel_signal, deadline: Deadline, state: _Communication,
    budget: MutationBudget,
) -> str:
    try:
        communication.start()
    except RuntimeError:
        raise AgentLoopError("tool process communication could not start") from None
    _wait_for_tool(ready, cancel_signal, deadline)
    communication.join(PROCESS_STOP_SECONDS)
    if state.error or process.returncode != 0:
        raise AgentLoopError("tool process failed")
    return _decode(state.output, budget)


def _finish_interrupted_call(
    failure: BaseException, cleanup_error: AgentLoopError | None,
    config, name: str, arguments: dict, output: bytes, transaction_id: str,
    outcome_reporter: Callable[[MutationRecord], None] | None,
) -> None:
    mutation_state = _interrupted_mutation_state(
        config, name, arguments, output
    )
    if mutation_state is not None:
        warning = _mutation_warning(output)
        if cleanup_error is not None:
            warning = "; ".join(
                filter(None, (warning, f"cleanup failed: {cleanup_error}"))
            )
        record = mutation_record(
            transaction_id, name, mutation_state, warning
        )
        if outcome_reporter is not None:
            outcome_reporter(record)
        raise _interrupted_error(failure, record)
    if cleanup_error is not None:
        raise cleanup_error
    raise failure


def _completed_mutation_record(
    active: _ActiveTool, config, name: str, arguments: dict,
    cleanup_ok: bool, cleanup_error: AgentLoopError | None,
) -> MutationRecord | None:
    state = _interrupted_mutation_state(
        config, name, arguments, active.state.output
    )
    if state is None:
        return None
    warning = _mutation_warning(active.state.output)
    if cleanup_error is not None:
        warning = "; ".join(filter(None, (warning, f"cleanup failed: {cleanup_error}")))
    elif not cleanup_ok:
        warning = "; ".join(filter(None, (warning, "artifact cleanup failed")))
    return mutation_record(active.transaction_id, name, state, warning)


def _launch_tool_call(
    config, name: str, arguments: dict, budget: MutationBudget,
    max_bash_timeout: int, lease_fd: int | None, seconds: float,
) -> _ActiveTool:
    assert config.expected_workspace_identity is not None
    try:
        require_workspace_identity(
            config.workspace, config.expected_workspace_identity
        )
    except ToolInputError:
        raise AgentLoopError(
            "workspace identity changed since delegation started"
        ) from None
    transaction_id = uuid.uuid4().hex
    request = _encoded_request(
        config, name, arguments, budget, max_bash_timeout, transaction_id
    )
    try:
        process = _start_tool(seconds, lease_fd)
    except (OSError, RuntimeError):
        raise AgentLoopError("tool process could not start") from None
    state, ready = _Communication(), threading.Event()
    communication = threading.Thread(
        target=_communicate, args=(process, request, state, ready), daemon=True,
        name="deepseek-tool-pipe",
    )
    return _ActiveTool(transaction_id, process, state, ready, communication)


def _complete_tool_call(
    active: _ActiveTool, config, name: str, arguments: dict,
    result: str | None, failure: BaseException | None,
    cleanup_ok: bool, cleanup_error: AgentLoopError | None,
    outcome_reporter: Callable[[MutationRecord], None] | None,
) -> str:
    if failure is not None:
        _finish_interrupted_call(
            failure, cleanup_error, config, name, arguments,
            active.state.output, active.transaction_id, outcome_reporter,
        )
    assert result is not None
    record = _completed_mutation_record(
        active, config, name, arguments, cleanup_ok, cleanup_error
    )
    if record is None:
        if cleanup_error is not None:
            raise cleanup_error
        if name in {"Write", "Edit", "NotebookEdit"} and result.startswith("OK:"):
            raise MutationOutcomeError(
                "workspace mutation completed without a verified intent event; "
                "call get_deepseek_recovery before any retry; DO NOT RETRY"
            )
        return result
    if outcome_reporter is not None:
        outcome_reporter(record)
    if (
        cleanup_error is not None
        or not cleanup_ok
        or not result.startswith("OK:")
        or record.status != "committed"
        or record.warning is not None
    ):
        raise _interrupted_error(
            AgentLoopError("mutation did not complete cleanly"), record
        )
    return result


def execute_in_subprocess(
    config,
    name: str,
    arguments: dict,
    budget: MutationBudget,
    max_bash_timeout: int,
    lease_fd: int | None,
    cancel_signal,
    deadline: Deadline,
    outcome_reporter: Callable[[MutationRecord], None] | None = None,
) -> str:
    """Execute one tool and kill its process group at cancellation/deadline."""
    remaining = deadline_remaining(deadline)
    if remaining <= 0:
        raise AgentLoopError("run time budget exceeded")
    active = _launch_tool_call(
        config, name, arguments, budget, max_bash_timeout, lease_fd, remaining
    )
    result: str | None = None
    failure: BaseException | None = None
    try:
        result = _receive_tool_result(
            active.process, active.communication, active.ready, cancel_signal,
            deadline, active.state, budget,
        )
    except BaseException as error:
        failure = error
    cleanup_error: AgentLoopError | None = None
    try:
        cleanup_ok = _cleanup_after_result(
            active.process,
            active.communication,
            active.communication.ident is not None,
            config, name, arguments, active.transaction_id, result,
        )
    except AgentLoopError as error:
        cleanup_ok, cleanup_error = False, error
    return _complete_tool_call(
        active, config, name, arguments, result, failure,
        cleanup_ok, cleanup_error, outcome_reporter,
    )
