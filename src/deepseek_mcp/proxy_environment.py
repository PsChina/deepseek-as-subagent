"""Normalize proxy environment values that HTTP transports cannot parse."""
from __future__ import annotations

import contextlib
import logging
import os
import re
from collections.abc import Iterator, Mapping

logger = logging.getLogger(__name__)

NO_PROXY_NAMES = ("NO_PROXY", "no_proxy")
MAX_LOGGED_ENTRIES = 8
_BRACKETED_IPV6 = re.compile(r"^\[(?P<host>[^\]]*:[^\]]*)\](?::(?P<port>\d+))?$")


def normalize_no_proxy(value: str) -> tuple[str, list[str]]:
    """Unbracket IPv6 hosts in a ``no_proxy`` value, listing what changed.

    Docker Desktop writes ``NO_PROXY=localhost,127.0.0.1,[::1]`` and httpx
    parses every entry as a URL, so the bracketed literal raised
    ``InvalidURL: Invalid port: ':1]'`` inside the provider child. That was
    swallowed as ``category=client``, which reads like a DeepSeek outage
    instead of a local proxy configuration accident.
    """
    entries: list[str] = []
    changed: list[str] = []
    for raw in value.split(","):
        entry = raw.strip()
        if not entry:
            continue
        match = _BRACKETED_IPV6.fullmatch(entry)
        if match:
            port = match.group("port")
            host = match.group("host")
            entry = f"{host}:{port}" if port else host
            changed.append(raw.strip())
        entries.append(entry)
    return ",".join(entries), changed


def sanitize_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``environment`` with parsable ``no_proxy`` spellings."""
    result = dict(environment)
    for name in NO_PROXY_NAMES:
        value = result.get(name)
        if value is None:
            continue
        normalized, changed = normalize_no_proxy(value)
        if changed:
            result[name] = normalized
            _note_normalized(name, changed)
    return result


@contextlib.contextmanager
def sanitized_no_proxy_scope() -> Iterator[None]:
    """Fix this process's ``no_proxy`` only while a client reads the environment.

    httpx has no way to inject an environment, so the value must be corrected
    in place; restoring it immediately keeps a request from mutating the
    process for anything else running in it.
    """
    original = {name: os.environ.get(name) for name in NO_PROXY_NAMES}
    sanitized = sanitize_environment(
        {name: value for name, value in original.items() if value is not None}
    )
    try:
        for name, value in sanitized.items():
            os.environ[name] = value
        yield
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _note_normalized(name: str, changed: list[str]) -> None:
    shown = ", ".join(changed[:MAX_LOGGED_ENTRIES])
    suffix = ", ..." if len(changed) > MAX_LOGGED_ENTRIES else ""
    logger.warning(
        "normalized %d unparsable %s entries: %s%s", len(changed), name, shown, suffix
    )
