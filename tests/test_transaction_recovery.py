from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp import file_io, tool_child, tools, transaction_journal as journal, windows_workspace_write
from deepseek_mcp.config import Config
from deepseek_mcp.file_identity import FileIdentity, MISSING_FILE
from deepseek_mcp.transaction_report import bind_reporter
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
        target.write_text("before", encoding="utf-8")
        baseline = FileIdentity.from_stat(
            target.stat(), hashlib.sha256(b"before").digest()
        )
        target.write_text("committed", encoding="utf-8")
        digest = hashlib.sha256(b"committed").digest()
        journal.record_intent(
            self.config,
            transaction_id,
            "Write",
            {"path": "target.txt"},
            digest,
            baseline,
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

    def test_acknowledgement_refuses_modified_file_and_preserves_journal(self) -> None:
        transaction_id = self._record()
        (self.workspace / "target.txt").write_text("changed", encoding="utf-8")

        with self.assertRaisesRegex(TransactionRecoveryError, "failed safely"):
            acknowledge_with_lease(self.config, [transaction_id], self.locks)

        pending = query_with_lease(self.config, self.locks)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["transaction_id"], transaction_id)
        self.assertEqual(pending[0]["status"], "uncertain")

    def test_acknowledgement_refuses_missing_file_and_preserves_journal(self) -> None:
        transaction_id = self._record()
        (self.workspace / "target.txt").unlink()

        with self.assertRaisesRegex(TransactionRecoveryError, "failed safely"):
            acknowledge_with_lease(self.config, [transaction_id], self.locks)

        pending = query_with_lease(self.config, self.locks)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "uncertain")

    def test_batch_acknowledgement_validates_every_record_before_deleting(self) -> None:
        first = self._record("a" * 32)
        second_path = self.workspace / "other.txt"
        second_path.write_text("committed", encoding="utf-8")
        journal.record_intent(
            self.config, "b" * 32, "Write", {"path": "other.txt"},
            hashlib.sha256(b"committed").digest(),
        )
        second_path.write_text("changed", encoding="utf-8")

        with self.assertRaisesRegex(TransactionRecoveryError, "failed safely"):
            acknowledge_with_lease(self.config, [first, "b" * 32], self.locks)

        pending = query_with_lease(self.config, self.locks)
        self.assertEqual(
            {item["transaction_id"] for item in pending},
            {first, "b" * 32},
        )


    def test_precommit_failure_is_classified_and_acknowledgeable(self) -> None:
        target = self.workspace / "target.txt"
        target.write_text("before", encoding="utf-8")
        transaction_id = "e" * 32

        def persist_intent(digest, baseline):
            journal.record_intent(
                self.config, transaction_id, "Edit", {"path": "target.txt"},
                digest, baseline,
            )

        with bind_reporter(persist_intent):
            if os.name == "nt":
                commit_patch = patch.object(
                    windows_workspace_write, "_commit",
                    side_effect=OSError("injected precommit failure"),
                )
            else:
                commit_patch = patch.object(
                    file_io, "_commit_posix_file",
                    side_effect=OSError("injected precommit failure"),
                )
            with commit_patch:
                result = tools._execute_edit(
                    {"path": "target.txt", "old_string": "before", "new_string": "after"},
                    self.workspace,
                )

        self.assertTrue(result.startswith("ERROR:"))
        self.assertEqual(target.read_text(encoding="utf-8"), "before")
        pending = query_with_lease(self.config, self.locks)
        self.assertEqual(pending[0]["status"], "not_committed")
        removed, remaining = acknowledge_with_lease(
            self.config, [transaction_id], self.locks
        )
        self.assertEqual(removed, [transaction_id])
        self.assertEqual(remaining, [])

    def test_absent_target_after_precommit_write_is_not_committed(self) -> None:
        transaction_id = "f" * 32
        journal.record_intent(
            self.config, transaction_id, "Write", {"path": "new.txt"},
            hashlib.sha256(b"new content").digest(), MISSING_FILE,
        )
        pending = query_with_lease(self.config, self.locks)
        self.assertEqual(pending[0]["status"], "not_committed")
        removed, remaining = acknowledge_with_lease(
            self.config, [transaction_id], self.locks
        )
        self.assertEqual(removed, [transaction_id])
        self.assertEqual(remaining, [])

    def test_diverged_file_remains_uncertain_after_precommit_intent(self) -> None:
        target = self.workspace / "target.txt"
        target.write_text("before", encoding="utf-8")
        baseline = FileIdentity.from_stat(
            target.stat(), hashlib.sha256(b"before").digest()
        )
        journal.record_intent(
            self.config, "1" * 32, "Edit", {"path": "target.txt"},
            hashlib.sha256(b"intended").digest(), baseline,
        )
        target.write_text("third-party", encoding="utf-8")
        pending = query_with_lease(self.config, self.locks)
        self.assertEqual(pending[0]["status"], "uncertain")
        with self.assertRaisesRegex(TransactionRecoveryError, "failed safely"):
            acknowledge_with_lease(self.config, ["1" * 32], self.locks)

    def test_partial_batch_delete_requires_requery_before_retry(self) -> None:
        first, second = self._record("2" * 32), self._record("3" * 32)
        store_type = journal._WindowsStore if os.name == "nt" else journal._PosixStore
        original_delete = store_type.delete
        calls = 0

        def fail_second_delete(store, name, version):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected journal delete failure")
            return original_delete(store, name, version)

        with patch.object(store_type, "delete", side_effect=fail_second_delete):
            with self.assertRaisesRegex(TransactionRecoveryError, "outcome is uncertain"):
                acknowledge_with_lease(self.config, [first, second], self.locks)

        pending = query_with_lease(self.config, self.locks)
        self.assertEqual([item["transaction_id"] for item in pending], [second])

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
                MISSING_FILE,
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
                MISSING_FILE,
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
