"""Provider-specific request settings shared by configuration and API children."""
from __future__ import annotations

from urllib.parse import urlsplit

DEFAULT_MAX_OUTPUT_TOKENS = 16_384


def is_deepseek_endpoint(value: str) -> bool:
    try:
        hostname = urlsplit(value).hostname
    except ValueError:
        return False
    return bool(hostname and (
        hostname.lower() == "deepseek.com" or hostname.lower().endswith(".deepseek.com")
    ))


def validate_max_output_tokens(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("max_output_tokens must be an integer")
    if not 1 <= value <= DEFAULT_MAX_OUTPUT_TOKENS:
        raise RuntimeError(
            f"max_output_tokens must be between 1 and {DEFAULT_MAX_OUTPUT_TOKENS}"
        )
    return value
