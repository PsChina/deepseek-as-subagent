"""Private stdio child that performs exactly one DeepSeek API request."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from typing import Any
from urllib.parse import urlsplit

from .process_hardening import harden_provider_process
from .parent_liveness import wait_for_parent_loss_or_timeout

os.environ.pop("OPENAI_LOG", None)

import httpx
from openai import APIConnectionError, APIError, OpenAI, RateLimitError

API_CONNECT_TIMEOUT_SECONDS = 15.0
API_READ_TIMEOUT_SECONDS = 180.0
API_WRITE_TIMEOUT_SECONDS = 30.0
API_POOL_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_TOKENS_PER_REQUEST = 16_384
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024
_REASONING_EFFORTS = frozenset({"none", "low", "high", "max"})

for _name in ("openai", "httpx", "httpcore"):
    _logger = logging.getLogger(_name)
    _logger.handlers.clear()
    _logger.addHandler(logging.NullHandler())
    _logger.propagate = False
    _logger.setLevel(logging.WARNING)


def _error_summary(error: APIError) -> tuple[str, bool]:
    status = getattr(error, "status_code", None)
    suffix = f" status={status}" if isinstance(status, int) else ""
    if isinstance(error, APIConnectionError):
        category = "connection"
    elif isinstance(error, RateLimitError):
        category = "rate_limit"
    else:
        category = "api"
    retryable = isinstance(error, (APIConnectionError, RateLimitError))
    retryable |= isinstance(status, int) and 500 <= status < 600
    return f"category={category}{suffix}", retryable


def _bounded_timeout(request_timeout: float) -> httpx.Timeout:
    return httpx.Timeout(
        connect=min(API_CONNECT_TIMEOUT_SECONDS, request_timeout),
        read=min(API_READ_TIMEOUT_SECONDS, request_timeout),
        write=min(API_WRITE_TIMEOUT_SECONDS, request_timeout),
        pool=min(API_POOL_TIMEOUT_SECONDS, request_timeout),
    )


def _trust_proxy_environment(base_url: str) -> bool:
    """Never send loopback plaintext traffic through an inherited proxy."""
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        return True
    hostname = parsed.hostname
    return not (
        parsed.scheme.lower() == "http"
        and hostname is not None
        and hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    )


def _apply_reasoning_settings(arguments: dict[str, Any], effort: object) -> None:
    if not isinstance(effort, str) or effort not in _REASONING_EFFORTS:
        raise ValueError("invalid reasoning effort")
    if effort == "none":
        arguments["extra_body"] = {"thinking": {"type": "disabled"}}
        return
    arguments["reasoning_effort"] = effort
    arguments["extra_body"] = {"thinking": {"type": "enabled"}}


def _request_arguments(
    settings: dict[str, str], messages: list[dict], tools: list[dict]
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "model": settings["model"],
        "messages": messages,
        "max_tokens": MAX_OUTPUT_TOKENS_PER_REQUEST,
    }
    if "reasoning_effort" in settings:
        _apply_reasoning_settings(arguments, settings["reasoning_effort"])
    if tools:
        arguments["tools"] = tools
    return arguments


def _streaming_completion(client: OpenAI, arguments: dict[str, Any]) -> dict:
    chunks: list[bytes] = []
    total = 0
    create = client.chat.completions.with_streaming_response.create
    with create(**arguments) as response:
        encoding = response.headers.get("content-encoding", "identity")
        if encoding.strip().lower() not in ("", "identity"):
            raise ValueError("encoded provider responses are not accepted")
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_API_RESPONSE_BYTES:
                raise ValueError("provider response exceeded its hard limit")
            chunks.append(chunk)
    payload = json.loads(b"".join(chunks))
    if not isinstance(payload, dict):
        raise ValueError("provider response is not an object")
    return payload


def execute_request(
    settings: dict[str, str],
    messages: list[dict],
    tools: list[dict],
    request_timeout: float,
) -> dict:
    """Return a JSON-safe, redacted response envelope for the parent process."""
    client: OpenAI | None = None
    http_client: httpx.Client | None = None
    try:
        http_client = httpx.Client(
            timeout=_bounded_timeout(request_timeout),
            trust_env=_trust_proxy_environment(settings["base_url"]),
        )
        client = OpenAI(
            **{"api_" + "key": settings["credential"]},
            base_url=settings["base_url"],
            default_headers={"Accept-Encoding": "identity"},
            max_retries=0,
            http_client=http_client,
        )
        response = _streaming_completion(
            client, _request_arguments(settings, messages, tools)
        )
        return {"kind": "ok", "response": response}
    except APIError as error:
        summary, retryable = _error_summary(error)
        return {"kind": "error", "summary": summary, "retryable": retryable}
    except BaseException:
        return {"kind": "error", "summary": "category=client", "retryable": False}
    finally:
        if client is not None:
            try:
                client.close()
            except BaseException:
                pass
        if http_client is not None:
            try:
                http_client.close()
            except BaseException:
                pass


def _exit_if_stalled(
    completed: threading.Event, timeout: float, liveness: str
) -> None:
    wait_for_parent_loss_or_timeout(liveness, max(0.001, timeout))
    if not completed.is_set():
        os._exit(124)


def _request_timeout(argument: str) -> float:
    try:
        timeout = float(argument)
    except (TypeError, ValueError):
        raise ValueError("invalid provider deadline") from None
    if not 0 < timeout <= API_READ_TIMEOUT_SECONDS:
        raise ValueError("invalid provider deadline")
    return timeout


def _read_payload() -> tuple[dict[str, str], list[dict], list[dict]]:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("provider request is too large")
    payload = json.loads(raw)
    settings = payload["settings"]
    messages = payload["messages"]
    tools = payload["tools"]
    if not isinstance(settings, dict) or not isinstance(messages, list):
        raise ValueError("provider request has invalid fields")
    if not isinstance(tools, list):
        raise ValueError("provider request has invalid fields")
    return settings, messages, tools


def _write_payload(payload: dict) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        encoded = b'{"kind":"error","summary":"category=client","retryable":false}'
    view = memoryview(encoded)
    while view:
        written = os.write(1, view)
        if written <= 0:
            os._exit(125)
        view = view[written:]


def main() -> None:
    completed = threading.Event()
    try:
        timeout = _request_timeout(sys.argv[1])
        liveness = sys.argv[2]
        harden_provider_process()
    except (IndexError, ValueError):
        return
    except RuntimeError:
        try:
            _write_payload(
                {"kind": "error", "summary": "category=client", "retryable": False}
            )
        except BaseException:
            pass
        return
    watchdog = threading.Thread(
        target=_exit_if_stalled,
        args=(completed, timeout, liveness),
        daemon=True,
        name="deepseek-provider-deadline",
    )
    watchdog.start()
    try:
        settings, messages, tools = _read_payload()
        _write_payload(execute_request(settings, messages, tools, timeout))
    except BaseException:
        try:
            _write_payload(
                {"kind": "error", "summary": "category=client", "retryable": False}
            )
        except BaseException:
            pass
    finally:
        completed.set()


if __name__ == "__main__":
    main()
