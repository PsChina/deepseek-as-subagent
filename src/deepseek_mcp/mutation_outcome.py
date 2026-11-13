"""Structured records for workspace mutations that must survive later failures."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_MUTATION_RECORDS = 128
MAX_WARNING_CHARS = 4_096
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class MutationRecord:
    transaction_id: str
    tool: str
    status: str
    warning: str | None = None
    path: str | None = None
    sha256: str | None = None

    def as_dict(self) -> dict[str, str]:
        payload = {
            "transaction_id": self.transaction_id,
            "tool": self.tool,
            "status": self.status,
        }
        if self.warning:
            payload["warning"] = self.warning
        if self.path:
            payload["path"] = self.path
        if self.sha256:
            payload["sha256"] = self.sha256
        return payload

    def summary(self) -> str:
        label = "update committed" if self.status == "committed" else "outcome uncertain"
        target = f" at {self.path}" if self.path else ""
        detail = f" ({self.warning})" if self.warning else ""
        return f"{label}{target}{detail}; transaction {self.transaction_id}"


@dataclass
class MutationAccumulator:
    records: list[MutationRecord] = field(default_factory=list)

    def add(self, record: MutationRecord) -> None:
        if len(self.records) >= MAX_MUTATION_RECORDS:
            raise RuntimeError("mutation record capacity exceeded")
        self.records.append(record)

    def payload(self) -> list[dict[str, str]]:
        return [record.as_dict() for record in self.records]

    def warning_notice(self) -> str | None:
        warnings = [record for record in self.records if record.warning]
        if not warnings:
            return None
        return "[deepseek-mcp transaction safety] " + _summaries(warnings)

    def recovery_notice(self) -> str | None:
        if not self.records:
            return None
        identifiers = ", ".join(record.transaction_id for record in self.records)
        return (
            "[deepseek-mcp recovery required] Workspace mutations were journaled "
            f"({identifiers}). Call get_deepseek_recovery, verify every reported "
            "file, then call acknowledge_deepseek_mutations before delegating again."
        )


def mutation_record(
    transaction_id: str,
    tool: str,
    status: str,
    warning: str | None = None,
    path: str | None = None,
    sha256: str | None = None,
) -> MutationRecord:
    if status not in {"committed", "uncertain"}:
        raise ValueError("invalid mutation status")
    if path is not None and (
        not isinstance(path, str) or not path or path.startswith("/")
        or "\\" in path or (len(path) > 1 and path[0].isalpha() and path[1] == ":")
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError("invalid mutation path")
    if sha256 is not None and (not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None):
        raise ValueError("invalid mutation SHA-256")
    safe_warning = warning[:MAX_WARNING_CHARS] if warning else None
    return MutationRecord(transaction_id, tool, status, safe_warning, path, sha256)


def mutation_failure_message(
    records: list[MutationRecord], failure: BaseException | str,
) -> str:
    """Keep the review handshake separate from the reason the run stopped."""
    reason = str(failure) or "unknown failure"
    return (
        "workspace mutation requires review: "
        f"{_summaries(records)}; DO NOT RETRY the delegation until you call "
        "get_deepseek_recovery, verify the files, then call "
        "acknowledge_deepseek_mutations.\n"
        f"execution stopped before acknowledgement: {reason}"
    )


def records_from_result(result: object) -> list[MutationRecord]:
    if not isinstance(result, dict) or not isinstance(result.get("mutations"), list):
        return []
    records: list[MutationRecord] = []
    for value in result["mutations"]:
        if not isinstance(value, dict):
            continue
        transaction_id, tool, status = (
            value.get("transaction_id"), value.get("tool"), value.get("status")
        )
        warning, path, digest = (
            value.get("warning"), value.get("path"), value.get("sha256")
        )
        if not all(isinstance(item, str) for item in (transaction_id, tool, status)):
            continue
        if any(item is not None and not isinstance(item, str) for item in (warning, path, digest)):
            continue
        try:
            records.append(mutation_record(transaction_id, tool, status, warning, path, digest))
        except ValueError:
            continue
    return records


def _summaries(records: list[MutationRecord]) -> str:
    return "; ".join(record.summary() for record in records)
