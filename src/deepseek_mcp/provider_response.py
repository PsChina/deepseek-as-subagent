"""Project-owned, provider-neutral response validation for the parent process."""
from __future__ import annotations

import copy
from dataclasses import dataclass


class ProviderResponseError(ValueError):
    pass


class ProviderRetryableResponseError(ProviderResponseError):
    pass


@dataclass(frozen=True)
class ProviderUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class ProviderFunction:
    name: str
    arguments: str


@dataclass(frozen=True)
class ProviderToolCall:
    id: str
    function: ProviderFunction


@dataclass(frozen=True)
class ProviderMessage:
    content: str | None
    tool_calls: list[ProviderToolCall] | None


@dataclass(frozen=True)
class ProviderChoice:
    message: ProviderMessage
    finish_reason: str


@dataclass(frozen=True)
class ProviderResponse:
    raw: dict
    usage: ProviderUsage
    choices: list[ProviderChoice]

    @classmethod
    def from_payload(cls, payload: object) -> "ProviderResponse":
        if not isinstance(payload, dict):
            raise ProviderResponseError("provider response must be an object")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderResponseError("provider response has no choices")
        first = choices[0]
        if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
            raise ProviderResponseError("provider response message is invalid")
        if first.get("finish_reason") == "insufficient_system_resource":
            raise ProviderRetryableResponseError(
                "provider inference resources were insufficient"
            )
        message = _message(first["message"])
        finish_reason = _finish_reason(first.get("finish_reason"), message)
        return cls(copy.deepcopy(payload), _usage(payload.get("usage")), [
            ProviderChoice(message, finish_reason)
        ])

    def model_dump(self, *, exclude_none: bool = True) -> dict:
        value = copy.deepcopy(self.raw)
        return _without_none(value) if exclude_none else value


def _usage(value: object) -> ProviderUsage:
    if not isinstance(value, dict):
        raise ProviderResponseError("provider usage is missing or invalid")
    prompt = value.get("prompt_tokens")
    completion = value.get("completion_tokens")
    if (
        isinstance(prompt, bool)
        or not isinstance(prompt, int)
        or prompt <= 0
        or isinstance(completion, bool)
        or not isinstance(completion, int)
        or completion < 0
    ):
        raise ProviderResponseError("provider usage counters are invalid")
    return ProviderUsage(prompt, completion)


def _finish_reason(value: object, message: ProviderMessage) -> str:
    allowed = {
        "stop", "length", "content_filter", "tool_calls",
    }
    if not isinstance(value, str) or value not in allowed:
        raise ProviderResponseError("provider finish reason is missing or invalid")
    expected = "tool_calls" if message.tool_calls else "stop"
    if value != expected:
        raise ProviderResponseError(f"provider response ended with {value}")
    return value


def _message(value: dict) -> ProviderMessage:
    content = value.get("content")
    if content is not None:
        content = _strict_text(content, "provider message content")
    reasoning = value.get("reasoning_content")
    if reasoning is not None:
        _strict_text(reasoning, "provider reasoning content")
    calls = value.get("tool_calls")
    if calls is None:
        return ProviderMessage(content, None)
    if not isinstance(calls, list):
        raise ProviderResponseError("provider tool calls are invalid")
    return ProviderMessage(content, [_tool_call(call) for call in calls])


def _tool_call(value: object) -> ProviderToolCall:
    if not isinstance(value, dict) or not isinstance(value.get("function"), dict):
        raise ProviderResponseError("provider tool call is invalid")
    identifier = value.get("id")
    function = value["function"]
    name, arguments = function.get("name"), function.get("arguments")
    identifier = _strict_text(identifier, "provider tool call id")
    name = _strict_text(name, "provider tool name")
    arguments = _strict_text(arguments, "provider tool arguments")
    return ProviderToolCall(identifier, ProviderFunction(name, arguments))


def _strict_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ProviderResponseError(f"{label} is invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ProviderResponseError(f"{label} is not valid Unicode text") from None
    return value


def _without_none(value):
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items()
                if item is not None}
    if isinstance(value, list):
        return [_without_none(item) for item in value]
    return value
