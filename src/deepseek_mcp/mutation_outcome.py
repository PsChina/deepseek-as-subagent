"""Structured records for workspace mutations that must survive later failures."""
from __future__ import annotations

from dataclasses import dataclass, field

MAX_MUTATION_RECORDS = 128
MAX_WARNING_CHARS = 4_096


@dataclass(frozen=True)
class MutationRecord:
    transaction_id: str
    tool: str
    status: str
    warning: str | None = None

    def as_dict(self) -> dict[str, str]:
        payload = {
            "transaction_id": self.transaction_id,
            "tool": self.tool,
            "status": self.status,
        }
        if self.warning:
            payload["warning"] = self.warning
        return payload

    def summary(self) -> str:
        label = "update committed" if self.status == "committed" else "outcome uncertain"
        detail = f" ({self.warning})" if self.warning else ""
        return f"{label}{detail}; transaction {self.transaction_id}"


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
) -> MutationRecord:
    if status not in {"committed", "uncertain"}:
        raise ValueError("invalid mutation status")
    safe_warning = warning[:MAX_WARNING_CHARS] if warning else None
    return MutationRecord(transaction_id, tool, status, safe_warning)


def mutation_failure_message(
    records: list[MutationRecord], failure: BaseException | str,
) -> str:
    reason = str(failure)
    return (
        f"workspace mutation requires review: {_summaries(records)}; "
        "call get_deepseek_recovery, verify the files, then call "
        "acknowledge_deepseek_mutations; DO NOT RETRY; subsequent execution "
        f"stopped: {reason}"
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
        warning = value.get("warning")
        if not all(isinstance(item, str) for item in (transaction_id, tool, status)):
            continue
        if warning is not None and not isinstance(warning, str):
            continue
        try:
            records.append(mutation_record(transaction_id, tool, status, warning))
        except ValueError:
            continue
    return records


def _summaries(records: list[MutationRecord]) -> str:
    return "; ".join(record.summary() for record in records)
