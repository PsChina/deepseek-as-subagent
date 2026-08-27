from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp import tool_child, transaction_journal as journal
from deepseek_mcp.config import Config
from deepseek_mcp.execution_lock import acquire_workspace_lease
from deepseek_mcp.job_manager import DeepSeekJobManager, JobError
from deepseek_mcp.transaction_recovery import (
    TransactionRecoveryError,
    acknowledge_with_lease,
    load_recovery_config,
    query_with_lease,
)
from deepseek_mcp.transaction_journal import (
    JournalUpdatePublishedWarning,
    TransactionJournalError,
)


def _result() -> dict:
    return {
        "final_message": "done",
        "turns_used": 1,
        "tokens": {"prompt": 1, "completion": 1, "total": 2},
        "tool_calls": 0,
        "duration_seconds": 0.01,
        "mutations": [],
    }


class TransactionRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.locks = self.root / "locks"
        self.config = Config("", self.workspace, allowed_tools=[])
        journal_patch = patch.object(
            journal, "JOURNAL_DIRECTORY", self.root / "journal"
        )
        journal_patch.start()
        self.addCleanup(journal_patch.stop)

    def _record(self, transaction_id: str = "a" * 32) -> str:
        target = self.workspace / "target.txt"
        target.write_text("committed", encoding="utf-8")
        digest = hashlib.sha256(b"committed").digest()
        journal.record_intent(
            self.config,
            transaction_id,
            "Write",
            {"path": "target.txt"},
            digest,
        )
        return transaction_id

    def test_recovery_config_does_not_require_provider_credentials(self) -> None:
        with patch(
            "deepseek_mcp.transaction_recovery._load_data",
            return_value={"workspace": str(self.workspace)},
        ):
            recovered = load_recovery_config()

        self.assertEqual(recovered.api_key, "")
        self.assertEqual(recovered.workspace, self.workspace.resolve())
        self.assertEqual(recovered.allowed_tools, [])

    def test_query_and_acknowledge_are_exact_and_workspace_leased(self) -> None:
        transaction_id = self._record()

        pending = query_with_lease(self.config, self.locks)
        removed, remaining = acknowledge_with_lease(
            self.config, [transaction_id], self.locks
        )

        self.assertEqual(pending[0]["status"], "committed")
        self.assertEqual(removed, [transaction_id])
        self.assertEqual(remaining, [])

    def test_recovery_operations_are_busy_during_active_workspace_lease(self) -> None:
        lease = acquire_workspace_lease(self.workspace, self.locks)
        try:
            with self.assertRaisesRegex(TransactionRecoveryError, "busy"):
                query_with_lease(self.config, self.locks)
            with self.assertRaisesRegex(TransactionRecoveryError, "busy"):
                acknowledge_with_lease(self.config, [], self.locks)
        finally:
            lease.release()

    def test_job_manager_rechecks_pending_after_acquiring_workspace_lease(self) -> None:
        transaction_id = self._record()
        manager = DeepSeekJobManager(lock_directory=self.locks)

        with patch("deepseek_mcp.job_manager.run_agent") as run_agent:
            with self.assertRaisesRegex(JobError, "get_deepseek_recovery"):
                manager.run_sync("must be blocked", self.config)
        run_agent.assert_not_called()

        acknowledge_with_lease(self.config, [transaction_id], self.locks)
        with patch(
            "deepseek_mcp.job_manager.run_agent", return_value=_result()
        ) as run_agent:
            result = manager.run_sync("now safe", self.config)

        self.assertEqual(result["final_message"], "done")
        run_agent.assert_called_once()

    def test_tool_child_persists_intent_before_publishing_commit_event(self) -> None:
        calls: list[str] = []
        with (
            patch.object(
                tool_child,
                "record_intent",
                side_effect=lambda *_args: calls.append("journal"),
            ),
            patch.object(
                tool_child,
                "_write_mutation_ready",
                side_effect=lambda _digest: calls.append("event"),
            ),
        ):
            tool_child._persist_mutation_ready(
                self.config,
                "b" * 32,
                "Write",
                {"path": "target.txt"},
                hashlib.sha256(b"value").digest(),
            )

        self.assertEqual(calls, ["journal", "event"])

    def test_published_journal_warning_still_emits_nonretryable_intent(self) -> None:
        digest = hashlib.sha256(b"value").digest()
        with (
            patch.object(
                tool_child,
                "record_intent",
                side_effect=JournalUpdatePublishedWarning("published"),
            ),
            patch.object(tool_child, "_write_mutation_ready") as publish,
            self.assertRaises(JournalUpdatePublishedWarning),
        ):
            tool_child._persist_mutation_ready(
                self.config,
                "c" * 32,
                "Write",
                {"path": "target.txt"},
                digest,
            )

        publish.assert_called_once_with(digest)

    def test_post_commit_journal_failure_emits_safe_generic_warning(self) -> None:
        with (
            patch.object(
                tool_child,
                "append_warning",
                side_effect=TransactionJournalError("private detail"),
            ),
            patch.object(tool_child, "_write_mutation_warning") as publish,
            self.assertRaises(TransactionJournalError),
        ):
            tool_child._persist_mutation_warning(
                self.config, "d" * 32, "original warning"
            )

        publish.assert_called_once_with(
            "post-commit recovery warning could not be persisted"
        )


if __name__ == "__main__":
    unittest.main()
