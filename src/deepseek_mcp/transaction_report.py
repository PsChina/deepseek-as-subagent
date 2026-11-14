"""One-way mutation intent events from a killable tool child to its parent."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
from typing import Callable, Iterator

from .file_identity import ExpectedIdentity

_Reporter = Callable[[bytes, ExpectedIdentity], None]
_WarningReporter = Callable[[str], None]
_REPORTER: ContextVar[_Reporter | None] = ContextVar(
    "deepseek_mutation_reporter", default=None
)
_WARNING_REPORTER: ContextVar[_WarningReporter | None] = ContextVar(
    "deepseek_mutation_warning_reporter", default=None
)
_CANONICAL_PATH: ContextVar[str | None] = ContextVar(
    "deepseek_mutation_canonical_path", default=None
)


@contextmanager
def bind_reporter(
    reporter: _Reporter, warning_reporter: _WarningReporter | None = None,
) -> Iterator[None]:
    state = _REPORTER.set(reporter)
    warning_state = _WARNING_REPORTER.set(warning_reporter)
    try:
        yield
    finally:
        _WARNING_REPORTER.reset(warning_state)
        _REPORTER.reset(state)


def mutation_ready(
    digest: bytes, baseline: ExpectedIdentity = None,
    canonical_path: str | None = None,
) -> None:
    """Persist commit evidence while binding the exact path selected by file I/O."""
    reporter = _REPORTER.get()
    if reporter is not None:
        if canonical_path is None:
            reporter(digest, baseline)
        else:
            token = _CANONICAL_PATH.set(canonical_path)
            try:
                reporter(digest, baseline)
            finally:
                _CANONICAL_PATH.reset(token)


def current_mutation_path() -> str | None:
    """Return the canonical path bound to the active mutation-ready callback."""
    return _CANONICAL_PATH.get()


def mutation_evidence(raw: bytes) -> tuple[bytes | None, str | None]:
    """Decode the digest and canonical path from the child mutation event."""
    for line in raw.splitlines():
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("kind") != "mutation_ready":
            continue
        digest = payload.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            continue
        try:
            decoded = bytes.fromhex(digest)
        except ValueError:
            continue
        if len(decoded) != 32:
            continue
        path = payload.get("path")
        if "path" in payload and not isinstance(path, str):
            path = ""
        return decoded, path
    return None, None


def mutation_warning(detail: str) -> None:
    """Publish a post-commit warning without relying on model-visible text."""
    reporter = _WARNING_REPORTER.get()
    if reporter is not None:
        reporter(detail)
