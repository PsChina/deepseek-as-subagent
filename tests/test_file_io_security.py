from __future__ import annotations

import json
import os
import signal
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import deepseek_mcp.file_io as file_io
import deepseek_mcp.posix_atomic_commit as posix_atomic_commit
import deepseek_mcp.tool_child as tool_child
import deepseek_mcp.transaction_report as transaction_report
import deepseek_mcp.tools as tools
import deepseek_mcp.workspace_walk as workspace_walk
from deepseek_mcp import windows_file_io
from deepseek_mcp.tools import (
    _execute_edit,
    _execute_glob,
    _execute_grep,
    _execute_notebook_edit,
    _execute_read,
    _execute_write,
)


def _notebook(source: str) -> str:
    value = {
        "cells": [
            {
                "cell_type": "code",
                "id": "cell",
                "source": [source],
                "metadata": {},
                "outputs": [],
                "execution_count": None,
            }
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return json.dumps(value)


class WorkspaceFileRaceTests(unittest.TestCase):
    @unittest.skipUnless(
        os.name == "posix" and hasattr(signal, "pthread_sigmask"),
        "POSIX signal masking is required",
    )
    def test_tool_watchdog_inherits_blocked_sigterm(self) -> None:
        completed = threading.Event()
        inherited: list[set[signal.Signals]] = []
        before = signal.pthread_sigmask(signal.SIG_BLOCK, set())

        def inspect_mask(*_args) -> None:
            inherited.append(signal.pthread_sigmask(signal.SIG_BLOCK, set()))

        with patch.object(tool_child, "_exit_if_stalled", side_effect=inspect_mask):
            watchdog = tool_child._start_watchdog(completed, 1.0, "unused")
            watchdog.join(1.0)

        self.assertFalse(watchdog.is_alive())
        self.assertIn(signal.SIGTERM, inherited[0])
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), before)

    def test_file_tools_cannot_access_vcs_control_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            control = workspace / ".git" / "hooks"
            control.mkdir(parents=True)
            target = control / "pre-commit"
            target.write_text("host-control-marker", encoding="utf-8")
            cases = (
                (_execute_read, {"path": ".git/hooks/pre-commit"}),
                (
                    _execute_write,
                    {"path": ".git/hooks/pre-commit", "content": "changed"},
                ),
                (
                    _execute_edit,
                    {
                        "path": ".git/hooks/pre-commit",
                        "old_string": "host-control-marker",
                        "new_string": "changed",
                    },
                ),
            )
            for execute, arguments in cases:
                with self.subTest(tool=execute.__name__):
                    result = execute(arguments, workspace)
                    self.assertTrue(result.startswith("ERROR:"), result)
            self.assertEqual(target.read_text(), "host-control-marker")

    def test_file_tools_cannot_persist_host_agent_control(self) -> None:
        protected = (
            ".codex/config.toml",
            ".claude/settings.json",
            ".mcp.json",
            "AGENTS.md",
            "CLAUDE.md",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            for label in protected:
                target = workspace / label
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("host-control-marker", encoding="utf-8")
                for execute, arguments in (
                    (_execute_read, {"path": label}),
                    (_execute_write, {"path": label, "content": "changed"}),
                    (
                        _execute_edit,
                        {
                            "path": label,
                            "old_string": "host-control-marker",
                            "new_string": "changed",
                        },
                    ),
                ):
                    with self.subTest(label=label, tool=execute.__name__):
                        result = execute(arguments, workspace)
                        self.assertTrue(result.startswith("ERROR:"), result)
                self.assertEqual(target.read_text(), "host-control-marker")

    def test_recursive_tools_skip_explicit_vcs_control_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / ".git").mkdir()
            (workspace / ".git" / "config").write_text(
                "private-control-marker", encoding="utf-8"
            )

            glob_result = _execute_glob({"pattern": ".git/**"}, workspace)
            grep_result = _execute_grep(
                {"pattern": "private-control-marker", "path": ".git"}, workspace
            )

            self.assertNotIn(".git/config", glob_result)
            self.assertTrue(grep_result.startswith("ERROR:"), grep_result)

    @unittest.skipIf(os.name == "nt", "deterministic symlink swap uses POSIX semantics")
    def test_ancestor_swap_never_reads_or_writes_outside_workspace(self) -> None:
        cases = (
            (_execute_read, "input.txt", "inside", {"path": "safe/input.txt"}),
            (
                _execute_write,
                "input.txt",
                "inside",
                {"path": "safe/input.txt", "content": "agent"},
            ),
            (
                _execute_edit,
                "input.txt",
                "inside",
                {"path": "safe/input.txt", "old_string": "inside", "new_string": "agent"},
            ),
            (
                _execute_notebook_edit,
                "input.ipynb",
                _notebook("inside"),
                {
                    "path": "safe/input.ipynb",
                    "edit_mode": "replace",
                    "cell_id": "cell",
                    "new_source": "agent",
                },
            ),
        )
        for execute, name, initial, args in cases:
            with self.subTest(tool=execute.__name__):
                self._assert_ancestor_swap_is_confined(execute, name, initial, args)

    def _assert_ancestor_swap_is_confined(
        self, execute, name: str, initial: str, args: dict
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, outside = root / "workspace", root / "outside"
            safe = workspace / "safe"
            safe.mkdir(parents=True)
            outside.mkdir()
            (safe / name).write_text(initial, encoding="utf-8")
            outside_target = outside / name
            outside_target.write_text("outside-marker", encoding="utf-8")
            real_resolve = file_io.resolve_safe_path

            def swap_after_resolve(label: str, root_path: Path) -> Path:
                resolved = real_resolve(label, root_path)
                safe.rename(workspace / "original-safe")
                safe.symlink_to(outside, target_is_directory=True)
                return resolved

            with patch.object(file_io, "resolve_safe_path", side_effect=swap_after_resolve):
                result = execute(args, workspace)

            self.assertTrue(result.startswith("ERROR:"), result)
            self.assertNotIn("outside-marker", result)
            self.assertEqual(outside_target.read_text(encoding="utf-8"), "outside-marker")

    @unittest.skipIf(os.name == "nt", "deterministic symlink swap uses POSIX semantics")
    def test_terminal_swap_never_reads_or_writes_symlink_target(self) -> None:
        cases = (
            (_execute_read, "target.txt", "inside", "outside-marker", {"path": "target.txt"}),
            (
                _execute_write,
                "target.txt",
                "inside",
                "outside-marker",
                {"path": "target.txt", "content": "agent"},
            ),
            (
                _execute_edit,
                "target.txt",
                "inside",
                "outside-marker",
                {"path": "target.txt", "old_string": "inside", "new_string": "agent"},
            ),
            (
                _execute_notebook_edit,
                "target.ipynb",
                _notebook("inside"),
                _notebook("outside-marker"),
                {
                    "path": "target.ipynb",
                    "edit_mode": "replace",
                    "cell_id": "cell",
                    "new_source": "agent",
                },
            ),
        )
        for execute, name, initial, outside_content, args in cases:
            with self.subTest(tool=execute.__name__):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    workspace, outside = root / "workspace", root / f"outside-{name}"
                    workspace.mkdir()
                    target = workspace / name
                    target.write_text(initial, encoding="utf-8")
                    outside.write_text(outside_content, encoding="utf-8")
                    real_resolve = file_io.resolve_safe_path

                    def swap_after_resolve(label: str, root_path: Path) -> Path:
                        resolved = real_resolve(label, root_path)
                        target.unlink()
                        target.symlink_to(outside)
                        return resolved

                    with patch.object(
                        file_io, "resolve_safe_path", side_effect=swap_after_resolve
                    ):
                        result = execute(args, workspace)

                    self.assertTrue(result.startswith("ERROR:"), result)
                    self.assertEqual(outside.read_text(encoding="utf-8"), outside_content)

    def test_edit_rejects_replacement_after_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            target.write_text("before", encoding="utf-8")
            real_write = file_io.atomic_write_workspace_text

            def competing_write(*args, **kwargs) -> None:
                replacement = workspace / "replacement.txt"
                replacement.write_text("competitor", encoding="utf-8")
                os.replace(replacement, target)
                real_write(*args, **kwargs)

            with patch.object(tools, "_atomic_write_workspace_text", competing_write):
                result = _execute_edit(
                    {"path": "target.txt", "old_string": "before", "new_string": "after"},
                    workspace,
                )
            saved = target.read_text(encoding="utf-8")

        self.assertIn("changed during edit", result)
        self.assertEqual(saved, "competitor")

    def test_write_never_overwrites_a_file_changed_after_an_earlier_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            target.write_text("before", encoding="utf-8")
            self.assertEqual(_execute_read({"path": "target.txt"}, workspace), "before")
            target.write_text("competitor", encoding="utf-8")
            result = _execute_write(
                {"path": "target.txt", "content": "agent"}, workspace
            )

            self.assertIn("use Edit", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "competitor")

    def test_write_rejects_missing_target_created_during_temp_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            real_write = file_io._write_all

            def write_then_compete(descriptor: int, data: bytes) -> None:
                real_write(descriptor, data)
                target.write_text("competitor", encoding="utf-8")

            with patch.object(file_io, "_write_all", side_effect=write_then_compete):
                result = _execute_write(
                    {"path": "target.txt", "content": "agent"}, workspace
                )

            self.assertIn("file already exists", result)
            self.assertIn("use Edit", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "competitor")

    @unittest.skipIf(os.name == "nt", "POSIX atomic publish primitive")
    def test_write_rejects_file_created_at_atomic_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            real_publish = posix_atomic_commit._publish_missing

            def compete(parent: int, temporary: str, name: str) -> None:
                target.write_text("competitor", encoding="utf-8")
                real_publish(parent, temporary, name)

            with patch.object(
                posix_atomic_commit, "_publish_missing", side_effect=compete
            ):
                result = _execute_write(
                    {"path": "target.txt", "content": "agent"}, workspace
                )

            self.assertIn("file already exists", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "competitor")

    @unittest.skipIf(os.name == "nt", "POSIX atomic exchange primitive")
    def test_edit_restores_competitor_that_wins_commit_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            target.write_text("before", encoding="utf-8")
            real_exchange = posix_atomic_commit._exchange
            calls = 0

            def compete(parent: int, source: str, name: str) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    replacement = workspace / "competitor.txt"
                    replacement.write_text("competitor", encoding="utf-8")
                    os.replace(replacement, target)
                real_exchange(parent, source, name)

            with patch.object(
                posix_atomic_commit, "_exchange", side_effect=compete
            ):
                result = _execute_edit(
                    {
                        "path": "target.txt",
                        "old_string": "before",
                        "new_string": "agent",
                    },
                    workspace,
                )

            self.assertIn("changed during edit", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "competitor")
            self.assertEqual(list(workspace.glob(".deepseek-mcp-*")), [])

    @unittest.skipIf(os.name == "nt", "POSIX atomic exchange primitive")
    def test_edit_detects_same_inode_content_race_with_restored_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            target.write_text("before", encoding="utf-8")
            original = target.stat()
            real_exchange = posix_atomic_commit._exchange
            calls = 0

            def compete(parent: int, source: str, name: str) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    target.write_text("rivals", encoding="utf-8")
                    os.utime(
                        target,
                        ns=(original.st_atime_ns, original.st_mtime_ns),
                    )
                real_exchange(parent, source, name)

            with patch.object(
                posix_atomic_commit, "_exchange", side_effect=compete
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
            self.assertEqual(list(workspace.glob(".deepseek-mcp-*")), [])

    @unittest.skipIf(os.name == "nt", "POSIX atomic exchange primitive")
    def test_edit_restores_original_when_post_exchange_audit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            target.write_text("before", encoding="utf-8")
            with patch.object(
                posix_atomic_commit,
                "_same_version",
                side_effect=OSError("audit unavailable"),
            ):
                result = _execute_edit(
                    {
                        "path": "target.txt",
                        "old_string": "before",
                        "new_string": "agent",
                    },
                    workspace,
                )

            recoveries = list(workspace.glob(".deepseek-mcp-recovery-*"))
            self.assertIn("commit audit failed", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "before")
            self.assertEqual(len(recoveries), 1)
            self.assertEqual(recoveries[0].read_text(encoding="utf-8"), "agent")

    @unittest.skipIf(os.name == "nt", "POSIX atomic exchange primitive")
    def test_park_and_rollback_failure_preserve_displaced_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            target.write_text("before", encoding="utf-8")
            real_exchange = posix_atomic_commit._exchange
            calls = 0

            def fail_rollback(parent: int, source: str, name: str) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("rollback failed")
                real_exchange(parent, source, name)

            with (
                patch.object(
                    posix_atomic_commit,
                    "_park_displaced",
                    side_effect=OSError("park failed"),
                ),
                patch.object(
                    posix_atomic_commit, "_exchange", side_effect=fail_rollback
                ),
            ):
                result = _execute_edit(
                    {
                        "path": "target.txt",
                        "old_string": "before",
                        "new_string": "after",
                    },
                    workspace,
                )

            displaced = list(workspace.glob(".deepseek-mcp-*.tmp"))
            self.assertTrue(result.startswith("OK:"), result)
            self.assertIn("DO NOT RETRY", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "after")
            self.assertEqual(len(displaced), 1)
            self.assertEqual(displaced[0].read_text(encoding="utf-8"), "before")

    @unittest.skipIf(os.name == "nt", "POSIX atomic exchange primitive")
    def test_conflict_restore_failure_preserves_concurrent_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            target.write_text("before", encoding="utf-8")
            real_exchange = posix_atomic_commit._exchange
            calls = 0

            def compete_then_fail(parent: int, source: str, name: str) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    rival = workspace / "rival.txt"
                    rival.write_text("rivals", encoding="utf-8")
                    os.replace(rival, target)
                    real_exchange(parent, source, name)
                    return
                raise OSError("restore failed")

            with patch.object(
                posix_atomic_commit, "_exchange", side_effect=compete_then_fail
            ):
                result = _execute_edit(
                    {
                        "path": "target.txt",
                        "old_string": "before",
                        "new_string": "agents",
                    },
                    workspace,
                )

            recoveries = list(workspace.glob(".deepseek-mcp-recovery-*"))
            self.assertTrue(result.startswith("OK:"), result)
            self.assertIn("DO NOT RETRY", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "agents")
            self.assertEqual(len(recoveries), 1)
            self.assertEqual(recoveries[0].read_text(encoding="utf-8"), "rivals")

    @unittest.skipIf(os.name == "nt", "POSIX atomic exchange primitive")
    def test_audit_restore_failure_preserves_original_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            target.write_text("before", encoding="utf-8")
            real_exchange = posix_atomic_commit._exchange
            calls = 0

            def fail_restore(parent: int, source: str, name: str) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("restore failed")
                real_exchange(parent, source, name)

            with (
                patch.object(
                    posix_atomic_commit,
                    "_same_version",
                    side_effect=OSError("audit failed"),
                ),
                patch.object(
                    posix_atomic_commit, "_exchange", side_effect=fail_restore
                ),
            ):
                result = _execute_edit(
                    {
                        "path": "target.txt",
                        "old_string": "before",
                        "new_string": "after",
                    },
                    workspace,
                )

            recoveries = list(workspace.glob(".deepseek-mcp-recovery-*"))
            self.assertTrue(result.startswith("OK:"), result)
            self.assertIn("DO NOT RETRY", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "after")
            self.assertEqual(len(recoveries), 1)
            self.assertEqual(recoveries[0].read_text(encoding="utf-8"), "before")

    @unittest.skipUnless(
        os.name == "posix" and hasattr(signal, "pthread_sigmask"),
        "POSIX signal masking is required",
    )
    def test_sigterm_after_first_exchange_is_deferred_until_commit_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            target.write_text("before", encoding="utf-8")
            real_exchange = posix_atomic_commit._exchange
            calls = 0

            def signal_after_exchange(parent: int, source: str, name: str) -> None:
                nonlocal calls
                real_exchange(parent, source, name)
                calls += 1
                if calls == 1:
                    os.kill(os.getpid(), signal.SIGTERM)

            previous = signal.signal(signal.SIGTERM, tool_child._terminate_tool)
            try:
                with (
                    patch.object(
                        posix_atomic_commit,
                        "_exchange",
                        side_effect=signal_after_exchange,
                    ),
                    self.assertRaises(tool_child._ToolTermination),
                ):
                    _execute_edit(
                        {
                            "path": "target.txt",
                            "old_string": "before",
                            "new_string": "after",
                        },
                        workspace,
                    )
            finally:
                signal.signal(signal.SIGTERM, previous)

            self.assertEqual(target.read_text(encoding="utf-8"), "after")
            self.assertEqual(list(workspace.glob(".deepseek-mcp-*")), [])

    @unittest.skipUnless(
        os.name == "posix" and hasattr(signal, "pthread_sigmask"),
        "POSIX signal masking is required",
    )
    def test_sigterm_after_park_is_deferred_until_commit_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            target.write_text("before", encoding="utf-8")
            real_park = posix_atomic_commit._park_displaced

            def signal_after_park(parent: int, temporary: str) -> str:
                recovery = real_park(parent, temporary)
                os.kill(os.getpid(), signal.SIGTERM)
                return recovery

            previous = signal.signal(signal.SIGTERM, tool_child._terminate_tool)
            try:
                with (
                    patch.object(
                        posix_atomic_commit,
                        "_park_displaced",
                        side_effect=signal_after_park,
                    ),
                    self.assertRaises(tool_child._ToolTermination),
                ):
                    _execute_edit(
                        {
                            "path": "target.txt",
                            "old_string": "before",
                            "new_string": "after",
                        },
                        workspace,
                    )
            finally:
                signal.signal(signal.SIGTERM, previous)

            self.assertEqual(target.read_text(encoding="utf-8"), "after")
            self.assertEqual(list(workspace.glob(".deepseek-mcp-*")), [])

    @unittest.skipUnless(
        os.name == "posix" and hasattr(signal, "pthread_sigmask"),
        "POSIX signal masking is required",
    )
    def test_pending_sigterm_keeps_post_commit_recovery_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            target.write_text("before", encoding="utf-8")
            real_commit = file_io._commit_posix_file
            warnings: list[str] = []

            def commit_then_warn(*args) -> None:
                real_commit(*args)
                os.kill(os.getpid(), signal.SIGTERM)
                raise file_io.MutationCommittedWarning("recover from retained-copy")

            previous = signal.signal(signal.SIGTERM, tool_child._terminate_tool)
            try:
                with (
                    transaction_report.bind_reporter(lambda _digest: None, warnings.append),
                    patch.object(
                        file_io, "_commit_posix_file", side_effect=commit_then_warn
                    ),
                    self.assertRaises(tool_child._ToolTermination),
                ):
                    _execute_edit(
                        {
                            "path": "target.txt",
                            "old_string": "before",
                            "new_string": "after",
                        },
                        workspace,
                    )
            finally:
                signal.signal(signal.SIGTERM, previous)

            self.assertEqual(target.read_text(encoding="utf-8"), "after")
            self.assertIn("recover from retained-copy", warnings)

    @unittest.skipIf(os.name == "nt", "directory fsync is POSIX-specific")
    def test_edit_reports_committed_when_final_directory_fsync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            target.write_text("before", encoding="utf-8")
            real_fsync = os.fsync

            def fail_directory(descriptor: int) -> None:
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise OSError("directory sync failed")
                real_fsync(descriptor)

            with patch.object(file_io.os, "fsync", side_effect=fail_directory):
                result = _execute_edit(
                    {
                        "path": "target.txt",
                        "old_string": "before",
                        "new_string": "after",
                    },
                    workspace,
                )

            self.assertTrue(result.startswith("OK:"), result)
            self.assertIn("DO NOT RETRY", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "after")

    @unittest.skipIf(os.name == "nt", "POSIX no-replace publish primitive")
    def test_notebook_insert_uses_one_step_no_replace_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "new.ipynb"
            real_move = posix_atomic_commit._move_no_replace
            calls = []

            def record_move(parent: int, temporary: str, name: str) -> None:
                calls.append((temporary, name))
                real_move(parent, temporary, name)

            with patch.object(
                posix_atomic_commit, "_move_no_replace", side_effect=record_move
            ):
                result = _execute_notebook_edit(
                    {"path": "new.ipynb", "edit_mode": "insert", "new_source": "x"},
                    workspace,
                )

            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertTrue(result.startswith("OK:"), result)
            self.assertEqual(calls[0][1], "new.ipynb")
            self.assertEqual(list(workspace.glob(".deepseek-mcp-*")), [])
            self.assertEqual(len(saved["cells"]), 1)
            self.assertEqual(saved["cells"][0]["source"], ["x"])

    @unittest.skipUnless(os.name == "posix" and os.uname().sysname == "Darwin", "Darwin only")
    def test_edit_fails_closed_on_unverified_darwin_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.txt"
            target.write_text("before", encoding="utf-8")
            with patch.object(
                posix_atomic_commit,
                "_darwin_filesystem_name",
                return_value="fskit-test",
            ):
                result = _execute_edit(
                    {
                        "path": "target.txt",
                        "old_string": "before",
                        "new_string": "agent",
                    },
                    workspace,
                )

            self.assertIn("atomic exchange is unsafe", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "before")
            self.assertEqual(list(workspace.glob(".deepseek-mcp-*")), [])

    def test_notebook_insert_rejects_file_created_after_missing_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "new.ipynb"
            real_write = file_io.atomic_write_workspace_text

            def competing_write(*args, **kwargs) -> None:
                target.write_text("competitor", encoding="utf-8")
                real_write(*args, **kwargs)

            with patch.object(tools, "_atomic_write_workspace_text", competing_write):
                result = _execute_notebook_edit(
                    {"path": "new.ipynb", "edit_mode": "insert", "new_source": "x"},
                    workspace,
                )
            saved = target.read_text(encoding="utf-8")

        self.assertIn("appeared during edit", result)
        self.assertEqual(saved, "competitor")

    def test_notebook_replace_rejects_replacement_after_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "target.ipynb"
            target.write_text(_notebook("before"), encoding="utf-8")
            real_write = file_io.atomic_write_workspace_text

            def competing_write(*args, **kwargs) -> None:
                replacement = workspace / "replacement.ipynb"
                replacement.write_text("competitor", encoding="utf-8")
                os.replace(replacement, target)
                real_write(*args, **kwargs)

            with patch.object(tools, "_atomic_write_workspace_text", competing_write):
                result = _execute_notebook_edit(
                    {
                        "path": "target.ipynb",
                        "edit_mode": "replace",
                        "cell_id": "cell",
                        "new_source": "after",
                    },
                    workspace,
                )
            saved = target.read_text(encoding="utf-8")

        self.assertIn("changed during edit", result)
        self.assertEqual(saved, "competitor")


class WindowsHandlePolicyTests(unittest.TestCase):
    def test_regular_read_rejects_early_eof_with_stable_metadata(self) -> None:
        info = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_nlink=1,
            st_size=5,
            st_dev=1,
            st_ino=2,
            st_mtime_ns=3,
        )
        parent = SimpleNamespace(handle=7, expected=r"c:\workspace")
        with (
            patch.object(windows_file_io, "_open_directory", return_value=parent),
            patch.object(windows_file_io, "_open_child", return_value=9),
            patch.object(windows_file_io, "_descriptor", return_value=11),
            patch.object(windows_file_io.os, "fstat", return_value=info),
            patch.object(windows_file_io, "_read_descriptor", return_value=b"x"),
            patch.object(windows_file_io.os, "close") as close_descriptor,
            patch.object(windows_file_io, "_close") as close_handle,
            self.assertRaisesRegex(
                windows_file_io.WindowsPathError, "changed while it was read"
            ),
        ):
            windows_file_io.read_regular(Path(r"C:\workspace\private.log"))

        close_descriptor.assert_called_once_with(11)
        close_handle.assert_called_once_with(7)

    def test_descriptor_setup_failure_closes_crt_descriptor(self) -> None:
        with (
            patch.object(windows_file_io, "msvcrt", create=True) as crt,
            patch.object(
                windows_file_io.os,
                "set_inheritable",
                side_effect=OSError("denied"),
            ),
            patch.object(windows_file_io.os, "close") as close_descriptor,
            patch.object(windows_file_io, "_close") as close_handle,
            self.assertRaisesRegex(OSError, "denied"),
        ):
            crt.open_osfhandle.return_value = 31
            windows_file_io._descriptor(17)

        close_descriptor.assert_called_once_with(31)
        close_handle.assert_not_called()

    def test_descriptor_conversion_failure_retains_handle_ownership(self) -> None:
        with (
            patch.object(windows_file_io, "msvcrt", create=True) as crt,
            patch.object(windows_file_io.os, "close") as close_descriptor,
            patch.object(windows_file_io, "_close") as close_handle,
            self.assertRaisesRegex(OSError, "conversion"),
        ):
            crt.open_osfhandle.side_effect = OSError("conversion")
            windows_file_io._descriptor(17)

        close_descriptor.assert_not_called()
        close_handle.assert_called_once_with(17)

    def test_windows_write_rechecks_implicit_call_local_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = root / "first"
            second = root / "second"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            cases = (
                ([first.stat(), second.stat()], "changed during edit"),
                ([None, second.stat()], "appeared during edit"),
            )
            for observations, message in cases:
                with (
                    self.subTest(message=message),
                    patch.object(file_io, "_win_open_parent", return_value=7),
                    patch.object(
                        file_io, "_win_current_info", side_effect=observations
                    ),
                    patch.object(file_io, "_win_open_child", return_value=9),
                    patch.object(file_io, "_win_fd", return_value=11),
                    patch.object(file_io, "_write_all"),
                    patch.object(file_io.os, "fstat", return_value=first.stat()),
                    patch.object(file_io.os, "fsync"),
                    patch.object(file_io.os, "close"),
                    patch.object(file_io, "_win_mark_delete"),
                    patch.object(file_io, "_win_close"),
                    patch.object(file_io, "_win_rename") as rename,
                    self.assertRaisesRegex(file_io.ToolInputError, message),
                ):
                    file_io._write_windows(
                        Path("C:/root"), Path("target.txt"), b"agent", None
                    )
                rename.assert_not_called()

    def test_failed_temp_delete_still_closes_windows_handles(self) -> None:
        primary = OSError("write failed")
        with (
            patch.object(file_io, "_win_open_parent", return_value=7),
            patch.object(file_io, "_win_current_info", return_value=None),
            patch.object(file_io, "_win_open_child", return_value=9),
            patch.object(file_io, "_win_fd", return_value=11),
            patch.object(file_io, "_write_all", side_effect=primary),
            patch.object(file_io, "_win_mark_delete", side_effect=OSError("cleanup")),
            patch.object(file_io.os, "close") as close_file,
            patch.object(file_io, "_win_close") as close_parent,
        ):
            with self.assertRaisesRegex(OSError, "write failed"):
                file_io._write_windows(Path("C:/root"), Path("file"), b"x", None)

        close_file.assert_called_once_with(11)
        close_parent.assert_called_once_with(7)

    def test_reparse_handle_is_rejected(self) -> None:
        with patch.object(file_io, "_win_attributes", return_value=0x400):
            with self.assertRaisesRegex(file_io.ToolInputError, "reparse point"):
                file_io._win_validate_handle(7, directory=False)

    def test_child_open_rechecks_parent_and_final_identity(self) -> None:
        paths = [r"c:\workspace\safe", r"c:\workspace\safe", r"c:\outside\secret"]
        with (
            patch.object(file_io, "_win_final_path", side_effect=paths),
            patch.object(file_io, "_win_open_path", return_value=19),
            patch.object(file_io, "_win_validate_handle"),
            patch.object(file_io, "_win_close") as close,
        ):
            with self.assertRaisesRegex(file_io.ToolInputError, "identity changed"):
                file_io._win_open_child(7, "secret", 1, 3, directory=False)

        close.assert_called_once_with(19)

    def test_windows_path_rejects_streams_and_ambiguous_suffixes(self) -> None:
        for label in ("name:stream", "name.", "name "):
            with self.subTest(label=label):
                with self.assertRaises(file_io.ToolInputError):
                    file_io._validate_windows_parts(Path(label))


class WorkspaceWalkFallbackRaceTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "deterministic symlink swap uses POSIX semantics")
    def test_glob_fallback_rejects_active_ancestor_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, outside = root / "workspace", root / "outside"
            safe = workspace / "safe"
            safe.mkdir(parents=True)
            outside.mkdir()
            (safe / "outside-marker.txt").symlink_to(workspace / "missing")
            (outside / "outside-marker.txt").write_text("outside", encoding="utf-8")

            result, swapped = self._walk_with_active_swap(
                workspace, safe, outside, _execute_glob, {"pattern": "**/*"}
            )

        self.assertTrue(swapped)
        self.assertNotIn("outside-marker.txt", result)

    @unittest.skipIf(os.name == "nt", "deterministic symlink swap uses POSIX semantics")
    def test_grep_fallback_never_opens_file_after_active_ancestor_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, outside = root / "workspace", root / "outside"
            safe = workspace / "safe"
            safe.mkdir(parents=True)
            outside.mkdir()
            (safe / "same.txt").write_text("inside", encoding="utf-8")
            (outside / "same.txt").write_text("outside-marker", encoding="utf-8")

            result, swapped = self._walk_with_active_swap(
                workspace,
                safe,
                outside,
                _execute_grep,
                {"pattern": "outside-marker"},
            )

        self.assertTrue(swapped)
        self.assertTrue(result.startswith("No matches found"), result)
        self.assertNotIn("same.txt:", result)

    def _walk_with_active_swap(
        self, workspace: Path, safe: Path, outside: Path, execute, args: dict
    ) -> tuple[str, bool]:
        real_next = workspace_walk._next_entry
        safe_display = safe.resolve()
        swapped = False

        def next_then_swap(stack):
            nonlocal swapped
            entry = real_next(stack)
            if (
                entry is not None
                and stack
                and stack[-1].display_path == safe_display
                and not swapped
            ):
                safe.rename(workspace / "original-safe")
                safe.symlink_to(outside, target_is_directory=True)
                swapped = True
            return entry

        with (
            patch.object(workspace_walk, "_HAS_SECURE_DIR_FDS", False),
            patch.object(workspace_walk, "_next_entry", side_effect=next_then_swap),
        ):
            return execute(args, workspace), swapped


if __name__ == "__main__":
    unittest.main()
