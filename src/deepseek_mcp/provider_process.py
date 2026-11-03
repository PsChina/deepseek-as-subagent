"""Terminate-capable subprocess boundary for one DeepSeek provider request."""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass, field

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
from .proxy_environment import sanitize_environment
from .provider_response import (
    ProviderContentFiltered,
    ProviderOutputTruncated,
    ProviderResponse,
    ProviderResponseError,
    ProviderRetryableResponseError,
)
from .hard_deadline import Deadline, limited, remaining

PROCESS_STOP_SECONDS = 2.0
CONTROL_CHECK_SECONDS = 0.05
STDERR_TAIL_BYTES = 4 * 1024
STDERR_READ_BYTES = 4 * 1024
STDERR_LOG_CHARS = 900
_SECRET_FORM = re.compile(
    r"(?i)(?:api[_-]?key|authorization|x-api-key|access[_-]?token|bearer|token)"
    r"\s*[:=]?\s*(?:basic\s+|bearer\s+)?[\"']?[^\s\"',;]{8,}|sk-[A-Za-z0-9_-]{8,}"
)
_NETWORK_ENVIRONMENT = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
)

logger = logging.getLogger(__name__)


class ProviderRequestCancelled(RuntimeError):
    pass


class ProviderRequestDeadline(RuntimeError):
    pass


@dataclass
class _Communication:
    output: bytes = b""
    error: bool = False


@dataclass
class _StderrTail:
    content: bytearray = field(default_factory=bytearray)
    total: int = 0

    def extend(self, chunk: bytes) -> None:
        self.total += len(chunk)
        self.content.extend(chunk)
        if len(self.content) > STDERR_TAIL_BYTES:
            del self.content[: len(self.content) - STDERR_TAIL_BYTES]


def _drain_stderr(descriptor: int, sink: _StderrTail) -> None:
    with suppress(OSError):
        while True:
            chunk = os.read(descriptor, STDERR_READ_BYTES)
            if not chunk:
                return
            sink.extend(chunk)


class _StderrCapture:
    """Bounded stderr capture that cannot deadlock the request pipe.

    The write end is handed to the child as a raw descriptor instead of
    ``subprocess.PIPE`` so ``communicate()`` neither reads nor buffers it; this
    side drains continuously and retains only the tail. If the pipe cannot be
    created the capture degrades to the previous ``DEVNULL`` behaviour.
    """

    def __init__(self) -> None:
        self.read_fd = -1
        self.write_fd = -1
        self.sink = _StderrTail()
        self.reader: threading.Thread | None = None

    @property
    def target(self) -> int:
        return self.write_fd if self.write_fd >= 0 else subprocess.DEVNULL

    def open(self) -> None:
        try:
            self.read_fd, self.write_fd = os.pipe()
        except OSError:
            self.read_fd = self.write_fd = -1

    def start_reader(self) -> None:
        if self.read_fd < 0 or self.reader is not None:
            return
        candidate = threading.Thread(
            target=_drain_stderr,
            args=(self.read_fd, self.sink),
            daemon=True,
            name="deepseek-provider-stderr",
        )
        try:
            candidate.start()
        except RuntimeError:
            # Diagnostics must never cost the request its transport.
            return
        self.reader = candidate

    def close(self) -> None:
        for name in ("write_fd", "read_fd"):
            descriptor = getattr(self, name)
            if descriptor >= 0:
                setattr(self, name, -1)
                with suppress(OSError):
                    os.close(descriptor)

    def tail(self, limit_seconds: float) -> str:
        """Close the local write end, then wait for EOF so the tail is complete."""
        if self.write_fd >= 0:
            with suppress(OSError):
                os.close(self.write_fd)
            self.write_fd = -1
        if self.reader is not None:
            self.reader.join(limit_seconds)
            self.reader = None
        if self.read_fd >= 0:
            with suppress(OSError):
                os.close(self.read_fd)
            self.read_fd = -1
        return bytes(self.sink.content).decode("utf-8", "replace")


def _redacted_stderr_detail(tail: str, secret: str) -> str:
    text = " ".join(tail.split())
    if secret:
        text = text.replace(secret, "[redacted]")
    text = _SECRET_FORM.sub("[redacted]", text)
    return text[-STDERR_LOG_CHARS:]


def _log_child_failure(config, returncode: object, capture: _StderrCapture) -> None:
    """Record why the child died without putting raw stderr in the MCP payload.

    The payload contract keeps only the allowlisted ``category=`` summaries, so
    diagnostics belong in the local log; they are bounded, single line, and
    credential-scrubbed because the child is the process that holds the key.
    """
    secret = str(getattr(config, "api_" + "key", "") or "")
    logger.warning(
        "provider child failed returncode=%s stderr_bytes=%d stderr_tail=%s",
        returncode,
        capture.sink.total,
        _redacted_stderr_detail(capture.tail(PROCESS_STOP_SECONDS), secret),
    )


def _provider_environment() -> dict[str, str]:
    environment = {name: os.environ[name] for name in _NETWORK_ENVIRONMENT if name in os.environ}
    for name in ("SYSTEMROOT", "WINDIR"):
        if name in os.environ:
            environment[name] = os.environ[name]
    environment["PATH"] = os.defpath
    return sanitized_python_environment(sanitize_environment(environment))


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
    settings = {
        "credential": getattr(config, "api_" + "key"),
        "base_url": config.base_url,
        "model": config.model,
        "max_output_tokens": getattr(config, "max_output_tokens", MAX_OUTPUT_TOKENS_PER_REQUEST),
    }
    reasoning_effort = getattr(config, "reasoning_effort", None)
    if reasoning_effort not in (None, "provider-default"):
        settings["reasoning_effort"] = reasoning_effort
    payload = {
        "settings": settings,
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
    except ProviderOutputTruncated:
        return None, "category=output_truncated", False
    except ProviderContentFiltered:
        return None, "category=content_filtered", False
    except (KeyError, TypeError, ValueError, ProviderResponseError, json.JSONDecodeError):
        return None, "category=client", False
    allowed = {
        "category=client", "category=connection", "category=rate_limit",
        "category=api", "category=output_truncated", "category=content_filtered",
    }
    base = summary.split(" status=", 1)[0]
    if base not in allowed:
        return None, "category=client", False
    return None, summary, retryable


def _start_provider(
    request_timeout: float, stderr_target: int
) -> subprocess.Popen[bytes]:
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
            stderr=stderr_target,
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
    capture = _StderrCapture()
    capture.open()
    try:
        process = _start_provider(request_seconds, capture.target)
    except (OSError, RuntimeError):
        capture.close()
        return None, "category=client", False
    capture.start_reader()
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
            _log_child_failure(config, None, capture)
            return None, "category=client", False
        if not _wait_for_provider(ready, cancel_signal, request_end, run_deadline):
            return None, "category=timeout", True
        communication.join(PROCESS_STOP_SECONDS)
        if state.error or process.returncode != 0:
            _log_child_failure(config, process.returncode, capture)
            return None, "category=client", False
        return _decode_response(state.output)
    finally:
        _cleanup_provider_process(process, communication, started)
        capture.close()
