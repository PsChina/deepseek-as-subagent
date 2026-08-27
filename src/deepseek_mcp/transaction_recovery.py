"""Workspace-leased recovery access for durable mutation intents."""
from __future__ import annotations

from pathlib import Path

from .config import Config, _load_data, _load_workspace
from .execution_lock import (
    WorkspaceExecutionLease,
    WorkspaceLockBusy,
    WorkspaceLockError,
    acquire_workspace_lease,
)
from .transaction_journal import (
    TransactionJournalError,
    acknowledge,
    pending_records,
)


class TransactionRecoveryError(RuntimeError):
    """Recovery state is unavailable or has not been acknowledged."""


class TransactionRecoveryRequired(TransactionRecoveryError):
    """A prior workspace mutation must be reviewed before delegation."""


def load_recovery_config() -> Config:
    """Load only the workspace boundary; provider credentials are unnecessary."""
    data = _load_data()
    return Config(api_key="", workspace=_load_workspace(data), allowed_tools=[])


def require_no_pending(config: Config) -> None:
    """Fail closed while the caller owns the workspace execution lease."""
    records = _pending(config)
    if not records:
        return
    identifiers = ", ".join(str(record["transaction_id"]) for record in records)
    raise TransactionRecoveryRequired(
        "workspace has unacknowledged mutation transactions "
        f"({identifiers}); call get_deepseek_recovery, verify the files, then "
        "call acknowledge_deepseek_mutations; DO NOT RETRY"
    )


def query_with_lease(
    config: Config, lock_directory: Path | None = None,
) -> list[dict[str, object]]:
    lease = _acquire(config, lock_directory)
    try:
        return _pending(config)
    finally:
        _release(lease)


def acknowledge_with_lease(
    config: Config, transaction_ids: list[str], lock_directory: Path | None = None,
) -> tuple[list[str], list[dict[str, object]]]:
    lease = _acquire(config, lock_directory)
    try:
        try:
            removed = acknowledge(config, transaction_ids)
        except TransactionJournalError:
            raise TransactionRecoveryError(
                "transaction acknowledgement failed safely"
            ) from None
        return removed, _pending(config)
    finally:
        _release(lease)


def _pending(config: Config) -> list[dict[str, object]]:
    try:
        return pending_records(config)
    except TransactionJournalError:
        raise TransactionRecoveryError(
            "transaction recovery journal is unavailable; DO NOT RETRY"
        ) from None


def _acquire(
    config: Config, lock_directory: Path | None,
) -> WorkspaceExecutionLease:
    try:
        assert config.expected_workspace_identity is not None
        return acquire_workspace_lease(
            config.workspace,
            lock_directory,
            expected_identity=bytes.fromhex(config.expected_workspace_identity),
        )
    except WorkspaceLockBusy:
        raise TransactionRecoveryError(
            "workspace recovery is busy while a DeepSeek delegation is active"
        ) from None
    except (ValueError, WorkspaceLockError):
        raise TransactionRecoveryError(
            "workspace recovery lease is unavailable"
        ) from None


def _release(lease: WorkspaceExecutionLease) -> None:
    try:
        lease.release()
    except WorkspaceLockError:
        raise TransactionRecoveryError(
            "workspace recovery lease could not be released"
        ) from None
