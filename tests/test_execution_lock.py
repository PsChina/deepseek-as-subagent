from __future__ import annotations

import multiprocessing
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deepseek_mcp import execution_lock, windows_file_io
from deepseek_mcp.execution_lock import (
    WorkspaceLockError,
    WorkspaceLockBusy,
    _same_identity,
    _secure_directory,
    _workspace_identity,
    acquire_workspace_lease,
)
from deepseek_mcp.lease_inheritance import ChildLeaseAnchor


def _hold_workspace_lease(
    workspace: str,
    lock_directory: str,
    ready,
    release,
) -> None:
    lease = acquire_workspace_lease(Path(workspace), Path(lock_directory))
    ready.set()
    release.wait()
    lease.release()


class WorkspaceExecutionLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = Path(self._temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.other_workspace = self.root / "other-workspace"
        self.other_workspace.mkdir()
        self.lock_directory = self.root / "locks"

    def _start_holder(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_workspace_lease,
            args=(
                str(self.workspace),
                str(self.lock_directory),
                ready,
                release,
            ),
        )
        process.start()
        self.assertTrue(ready.wait(5.0), f"holder failed with exit={process.exitcode}")
        return process, release

    def test_same_workspace_is_exclusive_across_processes(self) -> None:
        process, release = self._start_holder()
        try:
            with self.assertRaises(WorkspaceLockBusy):
                acquire_workspace_lease(self.workspace, self.lock_directory)

            other = acquire_workspace_lease(self.other_workspace, self.lock_directory)
            other.release()
        finally:
            release.set()
            process.join(5.0)
            if process.is_alive():
                process.terminate()
                process.join(5.0)

        self.assertEqual(process.exitcode, 0)

    def test_process_death_releases_workspace_lease(self) -> None:
        process, _ = self._start_holder()
        process.terminate()
        process.join(5.0)
        self.assertFalse(process.is_alive())

        replacement = acquire_workspace_lease(self.workspace, self.lock_directory)
        replacement.release()

    def test_workspace_alias_uses_the_same_lease(self) -> None:
        alias = self.root / "workspace-alias"
        try:
            alias.symlink_to(self.workspace, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks unavailable: {error}")

        lease = acquire_workspace_lease(self.workspace, self.lock_directory)
        try:
            with self.assertRaises(WorkspaceLockBusy):
                acquire_workspace_lease(alias, self.lock_directory)
        finally:
            lease.release()

    def test_expected_identity_rejects_replacement_at_same_path(self) -> None:
        identity = execution_lock.workspace_identity(self.workspace)
        moved = self.root / "moved-workspace"
        self.workspace.rename(moved)
        self.workspace.mkdir()

        with self.assertRaisesRegex(WorkspaceLockError, "identity changed"):
            acquire_workspace_lease(
                self.workspace,
                self.lock_directory,
                expected_identity=identity,
            )

        replacement = acquire_workspace_lease(self.workspace, self.lock_directory)
        replacement.release()

    def test_distinct_spellings_with_same_inode_have_same_identity(self) -> None:
        metadata = SimpleNamespace(st_dev=42, st_ino=9001)
        with patch.object(Path, "stat", return_value=metadata):
            upper = _workspace_identity(Path("/Users/example/project"))
            lower = _workspace_identity(Path("/users/example/project"))

        self.assertEqual(upper, lower)
        self.assertEqual(upper, b"filesystem:42:9001")

    def test_zero_inode_uses_case_normalized_real_path_fallback(self) -> None:
        metadata = SimpleNamespace(st_dev=0, st_ino=0)
        with (
            patch.object(Path, "stat", return_value=metadata),
            patch(
                "deepseek_mcp.execution_lock.os.path.realpath",
                side_effect=lambda value: value,
            ),
            patch(
                "deepseek_mcp.execution_lock.os.path.normcase",
                side_effect=lambda value: value.lower(),
            ),
        ):
            upper = _workspace_identity(Path("C:/Users/Example/Project"))
            lower = _workspace_identity(Path("c:/users/example/project"))

        self.assertEqual(upper, lower)

    def test_explicit_release_is_idempotent_and_preserves_lock_file(self) -> None:
        lease = acquire_workspace_lease(self.workspace, self.lock_directory)
        lock_path = lease.path
        self.assertGreaterEqual(lease.fileno(), 0)
        lease.release()
        lease.release()

        with self.assertRaisesRegex(RuntimeError, "already released"):
            lease.fileno()

        self.assertTrue(lock_path.exists())
        replacement = acquire_workspace_lease(self.workspace, self.lock_directory)
        replacement.release()

    def test_lease_descriptor_is_not_inheritable(self) -> None:
        lease = acquire_workspace_lease(self.workspace, self.lock_directory)
        try:
            assert lease._fd is not None
            self.assertFalse(os.get_inheritable(lease._fd))
        finally:
            lease.release()

    @unittest.skipUnless(os.name == "nt", "Windows inherited handle integration")
    def test_windows_child_keeps_exclusive_lease_anchor_alive(self) -> None:
        lease = acquire_workspace_lease(self.workspace, self.lock_directory)
        anchor = ChildLeaseAnchor.create(lease.fileno())
        assert anchor.startupinfo is not None
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            close_fds=True,
            startupinfo=anchor.startupinfo,
        )
        anchor.close_parent_copy()
        lease.release()
        try:
            with self.assertRaises(WorkspaceLockBusy):
                acquire_workspace_lease(self.workspace, self.lock_directory)
        finally:
            process.wait(timeout=5)
        replacement = acquire_workspace_lease(self.workspace, self.lock_directory)
        replacement.release()

    @unittest.skipIf(os.name == "nt", "POSIX no-follow ownership checks")
    def test_precreated_lock_directory_symlink_is_rejected(self) -> None:
        target = self.root / "attacker"
        target.mkdir()
        self.lock_directory.symlink_to(target, target_is_directory=True)

        with self.assertRaises(WorkspaceLockError):
            acquire_workspace_lease(self.workspace, self.lock_directory)

        self.assertEqual(list(target.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "POSIX no-follow ownership checks")
    def test_precreated_lock_file_symlink_is_rejected_without_target_write(self) -> None:
        lease = acquire_workspace_lease(self.workspace, self.lock_directory)
        lock_path = lease.path
        lease.release()
        lock_path.unlink()
        target = self.root / "target.txt"
        target.write_bytes(b"unchanged")
        lock_path.symlink_to(target)

        with self.assertRaises(WorkspaceLockError):
            acquire_workspace_lease(self.workspace, self.lock_directory)

        self.assertEqual(target.read_bytes(), b"unchanged")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_precreated_non_regular_lock_is_rejected_without_blocking(self) -> None:
        lease = acquire_workspace_lease(self.workspace, self.lock_directory)
        lock_path = lease.path
        lease.release()
        lock_path.unlink()
        os.mkfifo(lock_path)

        with self.assertRaises(WorkspaceLockError):
            acquire_workspace_lease(self.workspace, self.lock_directory)

    @unittest.skipIf(os.name == "nt", "POSIX mode semantics")
    def test_existing_lock_directory_and_file_are_tightened(self) -> None:
        self.lock_directory.mkdir(mode=0o777)
        self.lock_directory.chmod(0o777)
        lease = acquire_workspace_lease(self.workspace, self.lock_directory)
        try:
            self.assertEqual(self.lock_directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(lease.path.stat().st_mode & 0o777, 0o600)
        finally:
            lease.release()

    def test_windows_unsafe_directory_acl_is_rejected(self) -> None:
        path = Path("locks")
        with (
            patch("deepseek_mcp.execution_lock.os.name", "nt"),
            patch.object(
                windows_file_io,
                "validate_private_path",
                side_effect=windows_file_io.WindowsPathError("unsafe ACL"),
            ),
            self.assertRaisesRegex(OSError, "not private"),
        ):
            _secure_directory(path)

    def test_windows_directory_uses_private_handle_validation(self) -> None:
        path = Path("C:/Users/alice/.deepseek-mcp/locks")
        with (
            patch("deepseek_mcp.execution_lock.os.name", "nt"),
            patch.object(windows_file_io, "validate_private_path") as validate,
        ):
            _secure_directory(path)

        validate.assert_called_once_with(path, directory=True)

    def test_windows_lock_file_uses_private_descriptor_validation(self) -> None:
        path = Path("C:/Users/alice/.deepseek-mcp/locks/example.lock")
        info = SimpleNamespace(
            st_dev=1, st_ino=2, st_mode=stat.S_IFREG,
            st_size=1, st_mtime_ns=3, st_ctime_ns=4,
            st_file_attributes=0,
        )
        with (
            patch.object(windows_file_io, "validate_private_descriptor") as validate,
            patch.object(execution_lock, "_optional_real_path_info", return_value=info),
        ):
            execution_lock._validate_windows_lock(path, 99, info, info)

        validate.assert_called_once_with(99, path)

    def test_zero_inode_identity_detects_replacement_metadata(self) -> None:
        first = SimpleNamespace(
            st_dev=0, st_ino=0, st_mode=stat.S_IFREG, st_size=1,
            st_mtime_ns=10, st_ctime_ns=20,
        )
        replacement = SimpleNamespace(
            st_dev=0, st_ino=0, st_mode=stat.S_IFREG, st_size=2,
            st_mtime_ns=11, st_ctime_ns=21,
        )
        self.assertFalse(_same_identity(first, replacement))

    @unittest.skipIf(os.name == "nt", "POSIX close-only lease semantics")
    def test_posix_release_does_not_explicitly_unlock_inherited_lease(self) -> None:
        lease = acquire_workspace_lease(self.workspace, self.lock_directory)
        with patch("deepseek_mcp.execution_lock._unlock_fd") as unlock:
            lease.release()

        unlock.assert_not_called()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    unittest.main()
