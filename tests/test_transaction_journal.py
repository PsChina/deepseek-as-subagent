from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp.config import Config
from deepseek_mcp import transaction_journal as journal
from deepseek_mcp.transaction_journal import TransactionJournalError


def _sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


class TransactionJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        if os.name == "posix":
            self.root.chmod(0o700)
        self.journal = self.root / "journal"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config = Config("", self.workspace, allowed_tools=[])
        self.journal_patch = patch.object(
            journal, "JOURNAL_DIRECTORY", self.journal,
        )
        self.journal_patch.start()
        self.addCleanup(self.journal_patch.stop)

    def _record(
        self,
        transaction_id: str,
        *,
        path: str = "target.txt",
        content: str = "replacement",
        tool: str = "Write",
        create_target: bool = True,
    ) -> dict[str, object]:
        if create_target:
            target = self.workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return journal.record_intent(
            self.config,
            transaction_id,
            tool,
            {"path": path},
            _sha256(content),
        )

    def _scope_directory(self, config: Config | None = None) -> Path:
        token = (config or self.config).expected_workspace_identity
        assert token is not None
        return self.journal / hashlib.sha256(bytes.fromhex(token)).hexdigest()

    def _record_path(self, transaction_id: str) -> Path:
        return self._scope_directory() / f"{transaction_id}.json"

    def _temp_path(self, transaction_id: str) -> Path:
        return self._scope_directory() / f".{transaction_id}.json.tmp"

    def test_record_survives_reload_and_is_private(self) -> None:
        transaction_id = "a" * 32
        created = self._record(transaction_id, path="source/code.py")

        pending = journal.pending_records(self.config)

        self.assertEqual(created["status"], "pending")
        self.assertEqual(pending[0]["status"], "committed")
        self.assertEqual(pending[0]["path"], "source/code.py")
        record_path = self._record_path(transaction_id)
        self.assertTrue(record_path.is_file())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.journal.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self._scope_directory().stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o600)
            self.assertEqual(record_path.stat().st_nlink, 1)

    def test_record_intent_is_idempotent_but_rejects_id_reuse(self) -> None:
        transaction_id = "b" * 32
        first = self._record(transaction_id)
        second = journal.record_intent(
            self.config,
            transaction_id,
            "Write",
            {"path": "target.txt"},
            _sha256("replacement"),
        )

        self.assertEqual(second, first)
        with self.assertRaisesRegex(TransactionJournalError, "different intent"):
            journal.record_intent(
                self.config,
                transaction_id,
                "Edit",
                {"path": "target.txt"},
                _sha256("replacement"),
            )

    def test_classifies_match_as_committed_and_every_other_outcome_uncertain(self) -> None:
        self._record("c" * 32, path="committed.txt", content="committed")
        self._record("d" * 32, path="mismatch.txt", content="before")
        (self.workspace / "mismatch.txt").write_text("after", encoding="utf-8")
        self._record("e" * 32, path="missing.txt", create_target=False)
        self._record("f" * 32, path="read-failure.txt", content="before")
        (self.workspace / "read-failure.txt").unlink()
        (self.workspace / "read-failure.txt").mkdir()

        statuses = {
            value["transaction_id"]: value["status"]
            for value in journal.pending_records(self.config)
        }

        self.assertEqual(statuses["c" * 32], "committed")
        self.assertEqual(statuses["d" * 32], "uncertain")
        self.assertEqual(statuses["e" * 32], "uncertain")
        self.assertEqual(statuses["f" * 32], "uncertain")

    @unittest.skipIf(os.name == "nt", "deterministic POSIX symlink replacement")
    def test_query_classifies_target_and_ancestor_symlink_escape_as_uncertain(self) -> None:
        outside = self.root / "outside-query"
        outside.mkdir()
        (outside / "target.txt").write_text("outside", encoding="utf-8")
        direct = self.workspace / "direct.txt"
        direct.write_text("inside", encoding="utf-8")
        self._record("01" * 16, path="direct.txt", content="inside")
        direct.unlink()
        direct.symlink_to(outside / "target.txt")

        safe = self.workspace / "safe"
        safe.mkdir()
        (safe / "target.txt").write_text("inside", encoding="utf-8")
        self._record("02" * 16, path="safe/target.txt", content="inside")
        safe.rename(self.workspace / "original-safe")
        safe.symlink_to(outside, target_is_directory=True)

        statuses = {
            value["transaction_id"]: value["status"]
            for value in journal.pending_records(self.config)
        }
        self.assertEqual(statuses["01" * 16], "uncertain")
        self.assertEqual(statuses["02" * 16], "uncertain")
        self.assertEqual((outside / "target.txt").read_text(encoding="utf-8"), "outside")

    def test_warning_append_is_durable_and_ordered(self) -> None:
        transaction_id = "1" * 32
        self._record(transaction_id)

        journal.append_warning(self.config, transaction_id, "first warning")
        updated = journal.append_warning(self.config, transaction_id, "二次警告")
        pending = journal.pending_records(self.config)

        self.assertEqual(updated["warnings"], ["first warning", "二次警告"])
        self.assertEqual(pending[0]["warnings"], ["first warning", "二次警告"])
        self.assertLessEqual(
            self._record_path(transaction_id).stat().st_size,
            journal.MAX_RECORD_BYTES,
        )

    @unittest.skipIf(os.name == "nt", "POSIX atomic replacement injection")
    def test_failed_warning_replacement_preserves_previous_record(self) -> None:
        transaction_id = "2" * 32
        self._record(transaction_id)
        journal.append_warning(self.config, transaction_id, "preserved")

        with (
            patch.object(journal.os, "replace", side_effect=OSError("injected")),
            self.assertRaises(TransactionJournalError),
        ):
            journal.append_warning(self.config, transaction_id, "not committed")

        pending = journal.pending_records(self.config)
        self.assertEqual(pending[0]["warnings"], ["preserved"])

    @unittest.skipIf(os.name == "nt", "POSIX directory fsync injection")
    def test_post_publish_fsync_failure_is_distinct_and_recoverable(self) -> None:
        real_fsync = os.fsync
        self.journal.mkdir(mode=0o700)
        self._scope_directory().mkdir(mode=0o700)
        scope_info = self._scope_directory().stat()

        def fail_directory_fsync(descriptor: int) -> None:
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) == (scope_info.st_dev, scope_info.st_ino):
                raise OSError("injected directory fsync failure")
            real_fsync(descriptor)

        transaction_id = "21" * 16
        target = self.workspace / "target.txt"
        target.write_text("replacement", encoding="utf-8")
        with (
            patch.object(journal.os, "fsync", side_effect=fail_directory_fsync),
            self.assertRaises(journal.JournalUpdatePublishedWarning),
        ):
            journal.record_intent(
                self.config, transaction_id, "Write",
                {"path": "target.txt"}, _sha256("replacement"),
            )
        self.assertEqual(journal.pending_records(self.config)[0]["status"], "committed")

        with (
            patch.object(journal.os, "fsync", side_effect=fail_directory_fsync),
            self.assertRaises(journal.JournalUpdatePublishedWarning),
        ):
            journal.append_warning(self.config, transaction_id, "published warning")
        self.assertEqual(
            journal.pending_records(self.config)[0]["warnings"],
            ["published warning"],
        )

    def test_acknowledgement_is_exact_idempotent_and_workspace_scoped(self) -> None:
        own_id, foreign_id = "3" * 32, "4" * 32
        self._record(own_id)
        other_workspace = self.root / "other-workspace"
        other_workspace.mkdir()
        other_config = Config("", other_workspace, allowed_tools=[])
        (other_workspace / "foreign.txt").write_text("foreign", encoding="utf-8")
        journal.record_intent(
            other_config,
            foreign_id,
            "Edit",
            {"path": "foreign.txt"},
            _sha256("foreign"),
        )

        removed = journal.acknowledge(
            self.config, [foreign_id, own_id, own_id, "9" * 32],
        )

        self.assertEqual(removed, [own_id])
        self.assertEqual(journal.acknowledge(self.config, [own_id]), [])
        self.assertEqual(journal.pending_records(self.config), [])
        self.assertEqual(
            [value["transaction_id"] for value in journal.pending_records(other_config)],
            [foreign_id],
        )

    def test_invalid_acknowledgement_does_not_delete_anything(self) -> None:
        transaction_id = "5" * 32
        self._record(transaction_id)

        with self.assertRaises(TransactionJournalError):
            journal.acknowledge(self.config, [transaction_id, "../not-an-id"])

        with patch.object(journal, "MAX_RECORDS", 2):
            with self.assertRaisesRegex(TransactionJournalError, "too many"):
                journal.acknowledge(
                    self.config, [transaction_id, transaction_id, transaction_id],
                )

        self.assertEqual(len(journal.pending_records(self.config)), 1)

    def test_capacity_is_bounded(self) -> None:
        with patch.object(journal, "MAX_RECORDS", 2):
            self._record("6" * 32, path="one.txt")
            self._record("7" * 32, path="two.txt")
            with self.assertRaisesRegex(TransactionJournalError, "capacity"):
                self._record("8" * 32, path="three.txt")

    def test_foreign_workspace_records_do_not_consume_current_capacity(self) -> None:
        other_workspace = self.root / "capacity-other"
        other_workspace.mkdir()
        other_config = Config("", other_workspace, allowed_tools=[])
        (other_workspace / "other.txt").write_text("other", encoding="utf-8")
        with patch.object(journal, "MAX_RECORDS", 1):
            journal.record_intent(
                other_config, "61" * 16, "Write",
                {"path": "other.txt"}, _sha256("other"),
            )
            self._record("62" * 16, path="current.txt")
        self.assertNotEqual(
            self._scope_directory(), self._scope_directory(other_config),
        )
        self.assertEqual(len(journal.pending_records(self.config)), 1)
        self.assertEqual(len(journal.pending_records(other_config)), 1)

    def test_crash_orphan_is_privately_removed_before_capacity_check(self) -> None:
        seed = "63" * 16
        self._record(seed)
        journal.acknowledge(self.config, [seed])
        orphan_id = "64" * 16
        orphan = self._temp_path(orphan_id)
        orphan.write_bytes(b'{"partial":true}')
        if os.name == "posix":
            orphan.chmod(0o600)
        unknown = self._scope_directory() / ".not-a-transaction.json.tmp"
        unknown.write_bytes(b"leave-owned-unknown-name-alone")

        with patch.object(journal, "MAX_RECORDS", 1):
            self._record("65" * 16, path="after-crash.txt")

        self.assertFalse(orphan.exists())
        self.assertTrue(unknown.exists())
        self.assertEqual(len(journal.pending_records(self.config)), 1)

    @unittest.skipIf(os.name == "nt", "POSIX hostile-link semantics")
    def test_hostile_named_temp_fails_closed_without_deleting_target(self) -> None:
        seed = "66" * 16
        self._record(seed)
        journal.acknowledge(self.config, [seed])
        outside = self.root / "outside-temp"
        outside.write_bytes(b"unchanged")
        symlink = self._temp_path("67" * 16)
        symlink.symlink_to(outside)

        with self.assertRaises(TransactionJournalError):
            journal.pending_records(self.config)
        self.assertEqual(outside.read_bytes(), b"unchanged")
        self.assertTrue(symlink.is_symlink())

        symlink.unlink()
        hardlink = self._temp_path("68" * 16)
        os.link(outside, hardlink)
        with self.assertRaisesRegex(TransactionJournalError, "uniquely linked"):
            journal.pending_records(self.config)
        self.assertEqual(outside.read_bytes(), b"unchanged")
        self.assertTrue(hardlink.exists())

    def test_windows_names_routes_only_strict_temp_names_through_safe_delete(self) -> None:
        path = self.root / "windows-store-abstraction"
        path.mkdir()
        strict_temp = f".{('69' * 16)}.json.tmp"
        record_name = f"{('6a' * 16)}.json"
        (path / strict_temp).write_bytes(b"orphan")
        (path / record_name).write_bytes(b"record")
        (path / ".almost.json.tmp").write_bytes(b"unknown")
        store = object.__new__(journal._WindowsStore)
        store.path = path
        baseline = object()

        with (
            patch.object(journal.windows_file_io, "validate_private_path"),
            patch.object(store, "read", return_value=(b"", baseline)) as read,
            patch.object(store, "delete") as delete,
        ):
            names = store.names()

        self.assertEqual(names, [record_name])
        read.assert_called_once_with(strict_temp)
        delete.assert_called_once_with(strict_temp, baseline)

    def test_warning_and_record_size_are_bounded(self) -> None:
        transaction_id = "9" * 32
        self._record(transaction_id)

        with self.assertRaisesRegex(TransactionJournalError, "4096"):
            journal.append_warning(self.config, transaction_id, "x" * 4097)
        with (
            patch.object(journal, "MAX_RECORD_BYTES", 512),
            self.assertRaisesRegex(TransactionJournalError, "64 KiB"),
        ):
            journal.append_warning(self.config, transaction_id, "y" * 400)

    @unittest.skipIf(os.name == "nt", "POSIX symlink and mode semantics")
    def test_symlinked_journal_directory_is_rejected_without_target_write(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        self.journal.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(TransactionJournalError):
            self._record("0" * 32)

        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_symlinked_record_is_rejected_without_following_target(self) -> None:
        transaction_id = "a1" * 16
        self._record(transaction_id)
        record_path = self._record_path(transaction_id)
        outside = self.root / "outside.json"
        outside.write_text("unchanged", encoding="utf-8")
        record_path.unlink()
        record_path.symlink_to(outside)

        with self.assertRaises(TransactionJournalError):
            journal.pending_records(self.config)

        self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged")

    @unittest.skipIf(os.name == "nt", "POSIX owner and mode semantics")
    def test_unsafe_directory_and_file_permissions_fail_closed(self) -> None:
        transaction_id = "b1" * 16
        self._record(transaction_id)
        record_path = self._record_path(transaction_id)
        record_path.chmod(0o644)
        with self.assertRaisesRegex(TransactionJournalError, "private"):
            journal.pending_records(self.config)
        record_path.chmod(0o600)
        self.journal.chmod(0o755)
        with self.assertRaisesRegex(TransactionJournalError, "0700"):
            journal.pending_records(self.config)
        self.journal.chmod(0o700)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor lifecycle")
    def test_unsafe_lock_repeatedly_closes_lock_and_directory_descriptors(self) -> None:
        scope = self._scope_directory()
        self.journal.mkdir(mode=0o700)
        scope.mkdir(mode=0o700)
        lock_path = scope / ".lock"
        lock_path.write_bytes(b"lock")
        lock_path.chmod(0o644)
        token = self.config.expected_workspace_identity
        assert token is not None

        for _ in range(8):
            store = journal._PosixStore(token)
            directory_fd = store.directory
            with self.assertRaises(TransactionJournalError):
                store.__enter__()
            with self.assertRaises(OSError):
                os.fstat(directory_fd)
            with self.assertRaises(OSError):
                os.fstat(store.lock)

        lock_path.chmod(0o600)
        with journal._PosixStore(token) as recovered:
            self.assertGreaterEqual(recovered.lock, 0)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor lifecycle")
    def test_invalid_journal_directory_closes_opened_descriptor(self) -> None:
        self.journal.mkdir(mode=0o755)
        captured: list[int] = []
        real_open = os.open

        def capture_open(path, *args, **kwargs):
            descriptor = real_open(path, *args, **kwargs)
            if path == self.journal.name and kwargs.get("dir_fd") is not None:
                captured.append(descriptor)
            return descriptor

        token = self.config.expected_workspace_identity
        assert token is not None
        with (
            patch.object(journal.os, "open", side_effect=capture_open),
            self.assertRaises(TransactionJournalError),
        ):
            journal._PosixStore(token)
        self.assertTrue(captured)
        with self.assertRaises(OSError):
            os.fstat(captured[-1])
        self.journal.chmod(0o700)

    @unittest.skipIf(os.name == "nt", "POSIX hard-link semantics")
    def test_hard_linked_record_fails_closed(self) -> None:
        transaction_id = "c1" * 16
        self._record(transaction_id)
        record_path = self._record_path(transaction_id)
        os.link(record_path, self._scope_directory() / "attacker-link")

        with self.assertRaisesRegex(TransactionJournalError, "uniquely linked"):
            journal.pending_records(self.config)

    @unittest.skipIf(os.name == "nt", "direct POSIX corruption fixture")
    def test_oversized_record_and_strict_json_are_rejected(self) -> None:
        cases = (
            b"x" * (journal.MAX_RECORD_BYTES + 1),
            b'{"version":1,"version":1}',
            b"\xff",
            b'{"version":NaN}',
        )
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                transaction_id = f"{index + 10:032x}"
                self._record(transaction_id, path=f"case-{index}.txt")
                path = self._record_path(transaction_id)
                path.write_bytes(payload)
                path.chmod(0o600)
                with self.assertRaises(TransactionJournalError):
                    journal.pending_records(self.config)

    def test_rejects_bad_tool_path_digest_and_warning_unicode(self) -> None:
        cases = (
            ("Bash", {"path": "target.txt"}, _sha256("x")),
            ("Write", {"path": "../outside.txt"}, _sha256("x")),
            ("Write", {"path": "target.txt"}, b"short"),
        )
        for tool, arguments, digest in cases:
            with self.subTest(tool=tool, arguments=arguments):
                with self.assertRaises(TransactionJournalError):
                    journal.record_intent(
                        self.config, "d1" * 16, tool, arguments, digest,
                    )
        self._record("e1" * 16)
        with self.assertRaisesRegex(TransactionJournalError, "Unicode"):
            journal.append_warning(self.config, "e1" * 16, "\ud800")

    @unittest.skipUnless(os.name == "nt", "real Windows handle semantics")
    def test_windows_publish_survives_parent_close_failure(self) -> None:
        transaction_id = "f1" * 16
        target = self.workspace / "windows.txt"
        target.write_text("windows", encoding="utf-8")
        real_rename = journal.windows_atomic_commit.rename
        real_close = journal.windows_file_io._close
        renamed = False
        failed_handles: list[int] = []

        def rename_then_mark(*args, **kwargs):
            nonlocal renamed
            result = real_rename(*args, **kwargs)
            renamed = True
            return result

        def fail_after_rename(handle: int) -> None:
            if renamed:
                failed_handles.append(handle)
                raise OSError("injected parent close failure")
            real_close(handle)

        with (
            patch.object(journal.windows_atomic_commit, "rename", side_effect=rename_then_mark),
            patch.object(journal.windows_file_io, "_close", side_effect=fail_after_rename),
            self.assertRaises(journal.JournalUpdatePublishedWarning),
        ):
            journal.record_intent(
                self.config, transaction_id, "Write",
                {"path": "windows.txt"}, _sha256("windows"),
            )

        for handle in failed_handles:
            real_close(handle)
        self.assertTrue(self._record_path(transaction_id).is_file())
        self.assertEqual(journal.pending_records(self.config)[0]["status"], "committed")


if __name__ == "__main__":
    unittest.main()
