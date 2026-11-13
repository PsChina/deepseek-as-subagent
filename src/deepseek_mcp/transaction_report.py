"""One-way mutation intent events from a killable tool child to its parent."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
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


def mutation_ready(digest: bytes, baseline: ExpectedIdentity = None) -> None:
    """Persist the replacement digest and pre-mutation baseline before commit."""
    reporter = _REPORTER.get()
    if reporter is not None:
        reporter(digest, baseline)


def mutation_warning(detail: str) -> None:
    """Publish a post-commit warning without relying on model-visible text."""
    reporter = _WARNING_REPORTER.get()
    if reporter is not None:
        reporter(detail)
