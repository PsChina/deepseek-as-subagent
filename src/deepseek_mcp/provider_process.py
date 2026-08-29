"""Terminate-capable subprocess boundary for one DeepSeek provider request."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass

from .child_runtime import (
    child_working_directory,
    isolated_child_argv,
    sanitized_python_environment,
)
from .provider_child import (
    API_READ_TIMEOUT_SECONDS,
    MAX_OUTPUT_TOKENS_PER_REQUEST,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
)
from .parent_liveness import ParentLiveness, close_parent_liveness
from .provider_response import (
    ProviderResponse,
    ProviderResponseError,
    ProviderRetryableResponseError,
)
from .hard_deadline import Deadline, limited, remaining

PROCESS_STOP_SECONDS = 2.0
CONTROL_CHECK_SECONDS = 0.05
_NETWORK_ENVIRONMENT = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
)


class ProviderRequestCancelled(RuntimeError):
    pass


class ProviderRequestDeadline(RuntimeError):
    pass


@dataclass
class _Communication:
    output: bytes = b""
    error: bool = False


def _provider_environment() -> dict[str, str]:
    environment = {name: os.environ[name] for name in _NETWORK_ENVIRONMENT if name in os.environ}
    for name in ("SYSTEMROOT", "WINDIR"):
        if name in os.environ:
            environment[name] = os.environ[name]
    environment["PATH"] = os.defpath
    return sanitized_python_environment(environment)


def _stop_process(process: subprocess.Popen[bytes]) -> bool:
    if process.poll() is not None:
        process.wait()
        return True
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=PROCESS_STOP_SECONDS)
        return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=PROCESS_STOP_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return process.poll() is not None


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            with suppress(OSError, ValueError):
                stream.close()


def _request_end(deadline: Deadline | None) -> tuple[Deadline, bool]:
    return limited(deadline, API_READ_TIMEOUT_SECONDS)


def _encoded_request(config, messages: list[dict], tools: list[dict]) -> bytes:
    payload = {
        "settings": {
            "credential": getattr(config, "api_" + "key"),
            "base_url": config.base_url,
            "model": config.model,
            "reasoning_effort": config.reasoning_effort,
        },
        "messages": messages,
        "tools": tools,
    }
    output = bytearray()
    encoder = json.JSONEncoder(separators=(",", ":"), ensure_ascii=True)
    for text in encoder.iterencode(payload):
        chunk = text.encode("utf-8")
        if len(chunk) > MAX_REQUEST_BYTES - len(output):
            raise ProviderRequestDeadline(
                "provider request input exceeds its hard limit"
            )
        output.extend(chunk)
    return bytes(output)


def _communicate(
    process: subprocess.Popen[bytes], request: bytes, state: _Communication, ready: threading.Event
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


def _decode_response(raw: bytes) -> tuple[ProviderResponse | None, str, bool]:
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ProviderResponseError("provider envelope must be an object")
        if payload.get("kind") == "ok":
            return ProviderResponse.from_payload(payload["response"]), "", False
        summary = payload["summary"]
        retryable = payload["retryable"]
        if not isinstance(summary, str) or not isinstance(retryable, bool):
            raise ProviderResponseError("provider error envelope is invalid")
    except ProviderRetryableResponseError:
        return None, "category=api", True
    except (KeyError, TypeError, ValueError, ProviderResponseError, json.JSONDecodeError):
        return None, "category=client", False
    allowed = {"category=client", "category=connection", "category=rate_limit", "category=api"}
    base = summary.split(" status=", 1)[0]
    if base not in allowed:
        return None, "category=client", False
    return None, summary, retryable


def _start_provider(request_timeout: float) -> subprocess.Popen[bytes]:
    liveness = ParentLiveness.create()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            isolated_child_argv(
                "deepseek_mcp.provider_child",
                f"{request_timeout:.6f}",
                liveness.argument,
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_provider_environment(),
            cwd=child_working_directory(),
            shell=False,
            close_fds=True,
            pass_fds=liveness.inherited_fds,
            start_new_session=True,
        )
        liveness.attach(process)
        return process
    except BaseException:
        try:
            if process is not None:
                _stop_process(process)
        finally:
            if process is not None:
                _close_process_pipes(process)
            liveness.close()
        raise


def _wait_for_provider(
    ready: threading.Event,
    cancel_signal,
    request_end: Deadline,
    run_deadline: bool,
) -> bool:
    while not ready.wait(CONTROL_CHECK_SECONDS):
        if cancel_signal is not None and cancel_signal.is_set():
            raise ProviderRequestCancelled("provider request cancelled")
        if remaining(request_end) <= 0:
            if run_deadline:
                raise ProviderRequestDeadline("run time budget exceeded")
            return False
    return True


def _cleanup_provider_process(
    process: subprocess.Popen[bytes],
    communication: threading.Thread,
    started: bool,
) -> None:
    try:
        if not _stop_process(process):
            raise ProviderRequestDeadline("provider process cleanup failed")
        if started:
            communication.join(PROCESS_STOP_SECONDS)
        if started and communication.is_alive():
            raise ProviderRequestDeadline("provider pipe cleanup failed")
    finally:
        try:
            close_parent_liveness(process)
        finally:
            _close_process_pipes(process)


def request_in_subprocess(
    config,
    messages: list[dict],
    tools: list[dict],
    cancel_signal,
    deadline: Deadline | None,
) -> tuple[ProviderResponse | None, str, bool]:
    """Return a typed response or redacted failure; kill on cancel/deadline."""
    request_end, run_deadline = _request_end(deadline)
    request_seconds = remaining(request_end)
    if request_seconds <= 0:
        raise ProviderRequestDeadline("run time budget exceeded")
    request = _encoded_request(config, messages, tools)
    try:
        process = _start_provider(request_seconds)
    except (OSError, RuntimeError):
        return None, "category=client", False
    state = _Communication()
    ready = threading.Event()
    communication = threading.Thread(
        target=_communicate,
        args=(process, request, state, ready),
        daemon=True,
        name="deepseek-provider-pipe",
    )
    started = False
    try:
        try:
            communication.start()
            started = True
        except RuntimeError:
            return None, "category=client", False
        if not _wait_for_provider(ready, cancel_signal, request_end, run_deadline):
            return None, "category=timeout", True
        communication.join(PROCESS_STOP_SECONDS)
        if state.error or process.returncode != 0:
            return None, "category=client", False
        return _decode_response(state.output)
    finally:
        _cleanup_provider_process(process, communication, started)
