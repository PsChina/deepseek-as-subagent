"""Bind every delegated operation to the workspace object it was given."""
from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from .execution_lock import (
    WorkspaceLockError,
    filesystem_identity,
    workspace_identity,
)
from .file_identity import ToolInputError

_TOKEN = re.compile(r"[0-9a-f]{2,8192}")
_EXPECTED: ContextVar[bytes | None] = ContextVar(
    "deepseek_expected_workspace_identity", default=None
)


def _decode_token(token: str) -> bytes:
    if (
        not isinstance(token, str)
        or len(token) % 2
        or _TOKEN.fullmatch(token) is None
    ):
        raise RuntimeError("workspace identity token is invalid")
    return bytes.fromhex(token)


def configure_workspace_identity(path: Path, token: str | None) -> str:
    """Capture an identity once, or verify an inherited internal token."""
    try:
        current = workspace_identity(path)
    except WorkspaceLockError as error:
        raise RuntimeError(str(error)) from None
    if token is None:
        return current.hex()
    if current != _decode_token(token):
        raise RuntimeError("workspace identity changed since delegation started")
    return token


def expected_identity(token: str) -> bytes:
    """Decode a previously validated config token for lease acquisition."""
    return _decode_token(token)


@contextmanager
def bind_workspace_identity(token: str) -> Iterator[None]:
    state = _EXPECTED.set(_decode_token(token))
    try:
        yield
    finally:
        _EXPECTED.reset(state)


def require_workspace_identity(path: Path, token: str | None = None) -> None:
    wanted = _decode_token(token) if token is not None else _EXPECTED.get()
    if wanted is None:
        return
    try:
        actual = workspace_identity(path)
    except WorkspaceLockError as error:
        raise ToolInputError("workspace identity cannot be verified") from error
    if actual != wanted:
        raise ToolInputError("workspace identity changed since delegation started")


def require_workspace_stat(info) -> None:
    wanted = _EXPECTED.get()
    if wanted is None:
        return
    actual = filesystem_identity(info)
    if actual is None or actual != wanted:
        raise ToolInputError("workspace identity changed while opening")
