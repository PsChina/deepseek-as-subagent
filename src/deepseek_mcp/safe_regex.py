"""Linear-time regular expressions for model-controlled Grep patterns."""
from __future__ import annotations

from typing import Protocol

import re2

MAX_PATTERN_CHARS = 4_096
MAX_REGEX_MEMORY_BYTES = 8 * 1024 * 1024


class RegexPattern(Protocol):
    pattern: str

    def search(self, text: str): ...


class SafeRegexError(ValueError):
    """The requested expression exceeds the safe RE2 contract."""


def compile_safe_regex(pattern: str) -> RegexPattern:
    if len(pattern) > MAX_PATTERN_CHARS:
        raise SafeRegexError(f"pattern exceeds {MAX_PATTERN_CHARS} characters")
    try:
        pattern.encode("utf-8")
    except UnicodeEncodeError:
        raise SafeRegexError("pattern is not valid Unicode text") from None
    options = re2.Options()
    options.log_errors = False
    options.max_mem = MAX_REGEX_MEMORY_BYTES
    try:
        return re2.compile(pattern, options=options)
    except (re2.error, UnicodeError, ValueError) as exc:
        raise SafeRegexError(str(exc)) from exc
