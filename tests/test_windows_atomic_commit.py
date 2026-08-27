from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from deepseek_mcp import windows_atomic_commit, windows_workspace_write
from deepseek_mcp.file_identity import (
    FileIdentity,
    MISSING_FILE,
    MutationCommittedWarning,
    ToolInputError,
)
from deepseek_mcp.tools import _execute_edit, _execute_notebook_edit


def _identity(digest: bytes, *, inode: int = 2) -> FileIdentity:
    return FileIdentity(
        1, inode, stat.S_IFREG | 0o600, 6, 7, 8, digest
    )


class WindowsAtomicCommitModelTests(unittest.TestCase):
    def test_create_publish_is_kernel_no_clobber(self) -> None:
        exists = OSError("already exists")
        exists.winerror = 183
        publish = Mock(side_effect=exists)
        with self.assertRaisesRegex(ToolInputError, "appeared during edit"):
            windows_atomic_commit.commit(
                "temp", "target", MISSING_FILE, _identity(b"agent"),
                publish, Mock(), Mock(),
            )

        publish.assert_called_once_with("temp", "target")

    def test_existing_commit_discards_verified_baseline(self) -> None:
        replace, discard = Mock(), Mock(return_value=True)
        baseline = _identity(b"before")
        windows_atomic_commit.commit(
            "temp", "target", baseline, _identity(b"agent"),
            Mock(), replace, discard,
        )

        displaced = replace.call_args.args[2]
        replace.assert_called_once_with("target", "temp", displaced)
        discard.assert_called_once_with(displaced, baseline)

    def test_commit_window_competitor_is_restored(self) -> None:
        replace, discard = Mock(), Mock(side_effect=[False, True])
        replacement = _identity(b"agent", inode=3)
        with self.assertRaisesRegex(ToolInputError, "changed during edit"):
            windows_atomic_commit.commit(
                "temp", "target", _identity(b"before"), replacement,
                Mock(), replace, discard,
            )

        displaced = replace.call_args_list[0].args[2]
        recovery = replace.call_args_list[1].args[2]
        self.assertEqual(
            replace.call_args_list[1].args[:2], ("target", displaced)
        )
        discard.assert_called_with(recovery, replacement)

    def test_audit_io_failure_restores_original(self) -> None:
        replace = Mock()
        discard = Mock(side_effect=[OSError("audit"), True])
        with self.assertRaisesRegex(OSError, "original restored"):
            windows_atomic_commit.commit(
                "temp", "target", _identity(b"before"), _identity(b"agent"),
                Mock(), replace, discard,
            )

        self.assertEqual(replace.call_count, 2)

    def test_second_competitor_is_preserved_during_restore(self) -> None:
        replace, discard = Mock(), Mock(side_effect=[False, False])
        with self.assertRaisesRegex(ToolInputError, "concurrent data retained"):
            windows_atomic_commit.commit(
                "temp", "target", _identity(b"before"), _identity(b"agent"),
                Mock(), replace, discard,
            )

        recovery = replace.call_args_list[1].args[2]
        self.assertEqual(discard.call_args_list[1].args[0], recovery)

    def test_content_digest_detects_restored_metadata_race(self) -> None:
        baseline = _identity(b"before")
        same_metadata = FileIdentity(
            baseline.device, baseline.inode, baseline.mode, baseline.size,
            baseline.modified_ns, baseline.changed_ns + 1, b"rivals",
        )
        self.assertFalse(
            windows_atomic_commit.same_version(same_metadata, baseline)
        )

    def test_partial_replace_attempts_no_clobber_recovery(self) -> None:
        partial = OSError("partial")
        partial.winerror = 1177
        with (
            patch.object(
                windows_workspace_write.windows_atomic_commit,
                "replace_paths",
                side_effect=partial,
            ),
            patch.object(
                windows_workspace_write, "_publish_name"
            ) as publish,
            patch(
                "deepseek_mcp.file_io._win_final_path",
                return_value=r"c:\workspace",
            ),
            self.assertRaises(OSError),
        ):
            windows_workspace_write._replace_with_backup(
                7, "target", "temp", "backup"
            )

        publish.assert_called_once_with(
            7, "backup", "target", committed_result=False
        )

    def test_partial_nested_rollback_restores_original_not_replacement(self) -> None:
        partial = OSError("partial")
        partial.winerror = 1177
        with (
            patch.object(
                windows_workspace_write.windows_atomic_commit,
                "replace_paths",
                side_effect=partial,
            ),
            patch.object(windows_workspace_write, "_publish_name") as publish,
            patch(
                "deepseek_mcp.file_io._win_final_path",
                return_value=r"c:\workspace",
            ),
        ):
            windows_workspace_write._replace_with_backup(
                7, "target", "displaced-original", "agent-recovery",
                rollback=True,
            )

        publish.assert_called_once_with(
            7, "displaced-original", "target", committed_result=False
        )

    def test_nested_rollback_failure_reports_committed_mutation(self) -> None:
        failure = OSError("unable to replace")
        failure.winerror = 1175
        with (
            patch.object(
                windows_workspace_write.windows_atomic_commit,
                "replace_paths",
                side_effect=failure,
            ),
            patch(
                "deepseek_mcp.file_io._win_final_path",
                return_value=r"c:\workspace",
            ),
            self.assertRaisesRegex(
                MutationCommittedWarning,
                "replacement remains committed.*displaced-original",
            ),
        ):
            windows_workspace_write._replace_with_backup(
                7, "target", "displaced-original", "agent-recovery",
                rollback=True,
            )

    def test_nested_rollback_audit_warning_is_not_user_commit_success(self) -> None:
        rollback = Mock(
            side_effect=windows_atomic_commit.RollbackCompletedWarning(
                "parent audit failed"
            )
        )
        with self.assertRaisesRegex(OSError, "rollback audit is uncertain"):
            windows_atomic_commit.commit(
                "temp",
                "target",
                _identity(b"before"),
                _identity(b"agent"),
                Mock(),
                Mock(),
                Mock(return_value=False),
                rollback=rollback,
            )

    def test_create_close_failure_reports_committed_state(self) -> None:
        with (
            patch("deepseek_mcp.file_io._win_open_child", return_value=9),
            patch("deepseek_mcp.file_io._win_fd", return_value=11),
            patch("deepseek_mcp.file_io._win_rename") as rename,
            patch.object(
                windows_workspace_write.os,
                "close",
                side_effect=OSError("close failed"),
            ),
            self.assertRaisesRegex(
                MutationCommittedWarning, "creation committed"
            ),
        ):
            windows_workspace_write._publish_name(7, "temp", "target")

        rename.assert_called_once_with(11, 7, "target", replace=False)

    def test_replace_parent_audit_failure_reports_committed_state(self) -> None:
        with (
            patch.object(
                windows_workspace_write.windows_atomic_commit,
                "replace_paths",
            ),
            patch(
                "deepseek_mcp.file_io._win_final_path",
                side_effect=[r"c:\workspace", OSError("audit failed")],
            ),
            self.assertRaisesRegex(
                MutationCommittedWarning, "replacement committed"
            ),
        ):
            windows_workspace_write._replace_with_backup(
                7, "target", "temp", "backup"
            )

    def test_notebook_insert_does_not_invite_retry_after_windows_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "new.ipynb"

            def committed_write(_workspace, label, content, **_kwargs) -> None:
                (_workspace / label).write_text(content, encoding="utf-8")
                raise MutationCommittedWarning("Windows handle close failed")

            with patch(
                "deepseek_mcp.tools._atomic_write_workspace_text",
                side_effect=committed_write,
            ):
                result = _execute_notebook_edit(
                    {"path": "new.ipynb", "edit_mode": "insert", "new_source": "x"},
                    workspace,
                )

            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertTrue(result.startswith("OK:"), result)
            self.assertIn("DO NOT RETRY", result)
            self.assertEqual(len(saved["cells"]), 1)

    @unittest.skipUnless(os.name == "nt", "real ReplaceFileW competition")
    def test_real_commit_window_competitor_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            target.write_text("before", encoding="utf-8")
            real_replace = windows_atomic_commit.replace_paths
            calls = 0

            def compete(target_path: str, source: str, backup: str) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    rival = workspace / "rival.txt"
                    rival.write_text("rivals", encoding="utf-8")
                    os.replace(rival, target)
                real_replace(target_path, source, backup)

            with patch.object(
                windows_atomic_commit, "replace_paths", side_effect=compete
            ):
                result = _execute_edit(
                    {
                        "path": "target.txt",
                        "old_string": "before",
                        "new_string": "agents",
                    },
                    workspace,
                )

            self.assertIn("changed during edit", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "rivals")


if __name__ == "__main__":
    unittest.main()
