"""Strict schema validation and serialization for mutation journal records."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

MAX_WARNING_BYTES = 4096
_ID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_IDENTITY = re.compile(r"(?:[0-9a-f]{2}){1,4096}")
_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
_KEYS_V1 = frozenset({
    "version", "transaction_id", "workspace_identity", "tool",
    "path", "sha256", "warnings",
})
_KEYS_V2 = frozenset({*_KEYS_V1, "baseline_state", "baseline_sha256"})


class TransactionJournalError(RuntimeError):
    """A mutation journal record or storage operation is invalid."""


@dataclass(frozen=True)
class _StoredRecord:
    transaction_id: str
    workspace_identity: str
    tool: str
    path: str
    sha256: str
    warnings: tuple[str, ...] = ()
    version: int = 2
    baseline_state: str = "unknown"
    baseline_sha256: str | None = None

    def storage_payload(self) -> dict[str, object]:
        payload = {
            "version": self.version,
            "transaction_id": self.transaction_id,
            "workspace_identity": self.workspace_identity,
            "tool": self.tool,
            "path": self.path,
            "sha256": self.sha256,
            "warnings": list(self.warnings),
        }
        if self.version == 2:
            payload["baseline_state"] = self.baseline_state
            payload["baseline_sha256"] = self.baseline_sha256
        return payload

    def public_payload(self, status: str) -> dict[str, object]:
        return {
            "transaction_id": self.transaction_id,
            "tool": self.tool,
            "path": self.path,
            "sha256": self.sha256,
            "status": status,
            "warnings": list(self.warnings),
        }


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise TransactionJournalError("journal record contains a duplicate key")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _validate_warning(value: object) -> str:
    if not isinstance(value, str):
        raise TransactionJournalError("journal warning must be a string")
    try:
        size = len(value.encode("utf-8", "strict"))
    except UnicodeEncodeError:
        raise TransactionJournalError("journal warning must be valid Unicode") from None
    if size > MAX_WARNING_BYTES:
        raise TransactionJournalError("journal warning exceeds 4096 UTF-8 bytes")
    return value


def _matched(value: object, pattern: re.Pattern[str], message: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise TransactionJournalError(message)
    return value


def _target_fields(tool: object, path: object) -> tuple[str, str]:
    if not isinstance(tool, str) or tool not in _TOOLS:
        raise TransactionJournalError("journal mutation target is invalid")
    if not isinstance(path, str) or not path:
        raise TransactionJournalError("journal mutation target is invalid")
    relative = Path(path)
    if (
        not relative.parts or relative.is_absolute() or ".." in relative.parts
        or relative.as_posix() != path
        or any(":" in part or part.rstrip(" .") != part for part in relative.parts)
    ):
        raise TransactionJournalError("journal mutation path is not canonical and relative")
    return tool, path


def _validate_baseline_pair(state: str, digest: str | None) -> None:
    inconsistent = (
        (state == "missing" and digest is not None)
        or (state == "present" and digest is None)
        or (state == "unknown" and digest is not None)
    )
    if inconsistent:
        raise TransactionJournalError("journal baseline fields are inconsistent")


def _baseline_fields(value: dict[str, object], version: int) -> tuple[str, str | None]:
    if version == 1 and set(value) == _KEYS_V1:
        return "unknown", None
    if version != 2 or set(value) != _KEYS_V2:
        raise TransactionJournalError("journal record has an invalid schema")
    state = value.get("baseline_state")
    if not isinstance(state, str) or state not in {"missing", "present", "unknown"}:
        raise TransactionJournalError("journal baseline state is invalid")
    digest = value.get("baseline_sha256")
    if digest is not None:
        digest = _matched(digest, _DIGEST, "journal baseline digest is invalid")
    _validate_baseline_pair(state, digest)
    return state, digest


def _validate_stored(value: object) -> _StoredRecord:
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        raise TransactionJournalError("journal record has an invalid schema")
    version = value["version"]
    baseline_state, baseline_sha256 = _baseline_fields(value, version)
    transaction_id = _matched(
        value.get("transaction_id"), _ID, "journal transaction id is invalid"
    )
    identity = _matched(
        value.get("workspace_identity"), _IDENTITY,
        "journal workspace identity is invalid",
    )
    tool, path = _target_fields(value.get("tool"), value.get("path"))
    digest = _matched(value.get("sha256"), _DIGEST, "journal mutation digest is invalid")
    warnings = value.get("warnings")
    if not isinstance(warnings, list):
        raise TransactionJournalError("journal warnings are invalid")
    checked = tuple(_validate_warning(item) for item in warnings)
    return _StoredRecord(
        transaction_id, identity, tool, path, digest, checked,
        version, baseline_state, baseline_sha256,
    )


def _encode_record(record: _StoredRecord, max_bytes: int) -> bytes:
    try:
        encoded = json.dumps(
            record.storage_payload(),
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise TransactionJournalError("journal record cannot be encoded") from error
    if len(encoded) > max_bytes:
        raise TransactionJournalError("journal record exceeds 64 KiB")
    return encoded


def _decode_record(data: bytes, max_bytes: int) -> _StoredRecord:
    if len(data) > max_bytes:
        raise TransactionJournalError("journal record exceeds 64 KiB")
    try:
        value = json.loads(
            data.decode("utf-8", "strict"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError):
        raise TransactionJournalError("journal record is not strict UTF-8 JSON") from None
    return _validate_stored(value)
