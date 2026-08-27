from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp.workspace_snapshot import (
    WorkspaceSnapshotError,
    _ensure_private_directory,
    _write_all,
    cleanup_stale_snapshots,
    cleanup_workspace_snapshot,
    create_workspace_snapshot,
)

LABEL = "a" * 64
GIT = shutil.which("git")


def _git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    return subprocess.run(
        [GIT or "git", "-C", str(root), *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=environment,
        check=check,
    )


def _initialize_repository(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "--quiet")
    _git(path, "config", "user.name", "Snapshot Test")
    _git(path, "config", "user.email", "snapshot@example.invalid")


@unittest.skipUnless(os.name == "posix", "container snapshots require POSIX")
class WorkspaceSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.staging = self.root / "staging"
        self.staging.mkdir(mode=0o700)
        patcher = patch(
            "deepseek_mcp.workspace_snapshot._staging_root",
            return_value=self.staging,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_snapshot_contains_only_regular_files_and_real_directories(self) -> None:
        (self.workspace / "plain.txt").write_text("plain", encoding="utf-8")
        nested = self.workspace / "nested"
        nested.mkdir()
        (nested / "code.py").write_text("print('ok')\n", encoding="utf-8")
        external = self.root / "external.txt"
        external.write_text("outside", encoding="utf-8")
        (self.workspace / "external-link").symlink_to(external)
        os.mkfifo(self.workspace / "named-pipe")
        unix_socket = socket.socket(socket.AF_UNIX)
        self.addCleanup(unix_socket.close)
        unix_socket.bind(str(self.workspace / "service.sock"))

        snapshot = create_workspace_snapshot(self.workspace, LABEL)

        self.assertEqual((snapshot / "plain.txt").read_text(encoding="utf-8"), "plain")
        self.assertEqual(
            (snapshot / "nested" / "code.py").read_text(encoding="utf-8"),
            "print('ok')\n",
        )
        self.assertFalse((snapshot / "external-link").exists())
        self.assertFalse((snapshot / "named-pipe").exists())
        self.assertFalse((snapshot / "service.sock").exists())
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((snapshot / "nested").stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((snapshot / "plain.txt").stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(self.staging.stat().st_mode), 0o700)

    def test_snapshot_excludes_all_nested_vcs_control_directories(self) -> None:
        for name in (".git", ".GIT", ".hg", ".HG", ".svn", ".SVN"):
            control = self.workspace / "nested" / name
            control.mkdir(parents=True, exist_ok=True)
            (control / "private-state").write_text("secret", encoding="utf-8")
        (self.workspace / "nested" / "visible.txt").write_text(
            "visible", encoding="utf-8"
        )

        snapshot = create_workspace_snapshot(self.workspace, LABEL)

        for name in (".git", ".GIT", ".hg", ".HG", ".svn", ".SVN"):
            self.assertFalse((snapshot / "nested" / name).exists())
        self.assertEqual(
            (snapshot / "nested" / "visible.txt").read_text(encoding="utf-8"),
            "visible",
        )

    def test_snapshot_excludes_project_agent_control_files_and_directories(self) -> None:
        for label in (
            ".codex/config.toml",
            ".claude/settings.json",
            ".mcp.json",
            "AGENTS.md",
            "CLAUDE.md",
        ):
            target = self.workspace / label
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("control", encoding="utf-8")
        (self.workspace / "source.py").write_text("visible", encoding="utf-8")

        snapshot = create_workspace_snapshot(self.workspace, LABEL)

        for label in (
            ".codex/config.toml",
            ".claude/settings.json",
            ".mcp.json",
            "AGENTS.md",
            "CLAUDE.md",
        ):
            self.assertFalse((snapshot / label).exists())
        self.assertTrue((snapshot / "source.py").is_file())

    def test_snapshot_rejects_agent_control_workspace_before_allocation(self) -> None:
        protected = self.root / ".codex" / "workspace"
        protected.mkdir(parents=True)

        with (
            patch("deepseek_mcp.safety.Path.home", return_value=self.root),
            self.assertRaisesRegex(WorkspaceSnapshotError, "protected"),
        ):
            create_workspace_snapshot(protected, LABEL)

        self.assertEqual(list(self.staging.iterdir()), [])

    def test_snapshot_grants_container_read_access_but_preserves_execute_bits(self) -> None:
        private = self.workspace / "private"
        private.mkdir(mode=0o700)
        data = private / "data.txt"
        data.write_text("data", encoding="utf-8")
        data.chmod(0o600)
        executable = private / "tool.sh"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o700)

        snapshot = create_workspace_snapshot(self.workspace, LABEL)

        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((snapshot / "private").stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((snapshot / "private/data.txt").stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE((snapshot / "private/tool.sh").stat().st_mode), 0o555)
        cleanup_workspace_snapshot(snapshot, self.staging)
        self.assertFalse(snapshot.exists())

    def test_size_limit_removes_partial_snapshot(self) -> None:
        (self.workspace / "large.bin").write_bytes(b"1234")
        with (
            patch("deepseek_mcp.workspace_snapshot.MAX_SNAPSHOT_BYTES", 3),
            self.assertRaisesRegex(WorkspaceSnapshotError, "too large"),
        ):
            create_workspace_snapshot(self.workspace, LABEL)

        self.assertEqual(list(self.staging.iterdir()), [])

    @unittest.skipUnless(GIT, "Git is required")
    def test_git_metadata_is_rejected_before_exceeding_aggregate_disk_budget(self) -> None:
        repository = self.root / "disk-budget"
        _initialize_repository(repository)
        (repository / "tiny.txt").write_text("x", encoding="utf-8")
        with (
            patch(
                "deepseek_mcp.workspace_snapshot.MAX_SNAPSHOT_BYTES",
                4 * 1024 * 1024,
            ),
            self.assertRaisesRegex(WorkspaceSnapshotError, "aggregate disk budget"),
        ):
            create_workspace_snapshot(repository, LABEL)

        self.assertEqual(list(self.staging.iterdir()), [])

    def test_snapshot_rejects_workspace_that_contains_staging_root(self) -> None:
        with self.assertRaisesRegex(WorkspaceSnapshotError, "overlap"):
            create_workspace_snapshot(self.root, LABEL)
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_zero_length_write_fails_instead_of_spinning(self) -> None:
        with (
            patch("deepseek_mcp.workspace_snapshot.os.write", return_value=0),
            self.assertRaisesRegex(WorkspaceSnapshotError, "no progress"),
        ):
            _write_all(99, b"content")

    def test_in_place_file_change_fails_the_snapshot(self) -> None:
        source = self.workspace / "changing.txt"
        source.write_text("before", encoding="utf-8")
        from deepseek_mcp import workspace_snapshot

        original_write = workspace_snapshot._write_all
        changed = False

        def mutate_after_copy(target: int, chunk: bytes) -> None:
            nonlocal changed
            original_write(target, chunk)
            if not changed:
                changed = True
                source.write_text("after!", encoding="utf-8")

        with (
            patch.object(workspace_snapshot, "_write_all", side_effect=mutate_after_copy),
            self.assertRaisesRegex(WorkspaceSnapshotError, "file changed"),
        ):
            create_workspace_snapshot(self.workspace, LABEL)
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_directory_namespace_change_fails_the_snapshot(self) -> None:
        (self.workspace / "existing.txt").write_text("existing", encoding="utf-8")
        from deepseek_mcp import workspace_snapshot

        original_copy = workspace_snapshot._copy_entry
        changed = False

        def mutate_after_entry(*arguments) -> None:
            nonlocal changed
            original_copy(*arguments)
            if not changed and arguments[2] == 1:
                changed = True
                (self.workspace / "added.txt").write_text("added", encoding="utf-8")

        with (
            patch.object(workspace_snapshot, "_copy_entry", side_effect=mutate_after_entry),
            self.assertRaisesRegex(WorkspaceSnapshotError, "directory changed"),
        ):
            create_workspace_snapshot(self.workspace, LABEL)
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_cleanup_rejects_paths_outside_managed_root(self) -> None:
        outside = self.root / "do-not-remove"
        outside.mkdir()

        with self.assertRaisesRegex(WorkspaceSnapshotError, "unmanaged"):
            cleanup_workspace_snapshot(outside)

        self.assertTrue(outside.exists())

    def test_explicit_staging_root_does_not_depend_on_process_home(self) -> None:
        snapshot = create_workspace_snapshot(self.workspace, LABEL)
        with patch.object(Path, "home", return_value=Path("/nonexistent")):
            cleanup_workspace_snapshot(snapshot, self.staging)
        self.assertFalse(snapshot.exists())

    def test_cleanup_rejects_managed_name_symlink(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        link = self.staging / f"deepseek-mcp-{LABEL[:16]}-{'b' * 32}"
        link.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(WorkspaceSnapshotError, "real directory"):
            cleanup_workspace_snapshot(link)

        self.assertTrue(outside.exists())

    def test_stale_cleanup_is_scoped_to_workspace_label(self) -> None:
        stale = create_workspace_snapshot(self.workspace, LABEL)
        other = create_workspace_snapshot(self.workspace, "c" * 64)

        cleanup_stale_snapshots(LABEL)

        self.assertFalse(stale.exists())
        self.assertTrue(other.exists())

    def test_private_directory_rejects_symlink(self) -> None:
        target = self.root / "target"
        target.mkdir()
        link = self.root / "link"
        link.symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(WorkspaceSnapshotError, "secure"):
            _ensure_private_directory(link)

    @unittest.skipUnless(GIT, "Git is required")
    def test_regular_and_linked_worktrees_baseline_visible_files_only(self) -> None:
        repository = self.root / "repository"
        _initialize_repository(repository)
        marker = "HISTORY_ONLY_PRIVATE_MARKER"
        (repository / "history.txt").write_text(marker, encoding="utf-8")
        (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "--quiet", "-m", "history")
        secret_blob = _git(repository, "hash-object", "history.txt").stdout.strip()
        (repository / "history.txt").unlink()
        _git(repository, "add", "-u")
        _git(repository, "commit", "--quiet", "-m", "current")

        linked = self.root / "linked"
        _git(repository, "worktree", "add", "--quiet", "-b", "linked", str(linked))
        for worktree in (repository, linked):
            (worktree / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            (worktree / "untracked.txt").write_text("new\n", encoding="utf-8")
            snapshot = create_workspace_snapshot(worktree, LABEL)
            status = _git(snapshot, "status", "--short").stdout.splitlines()
            history = _git(snapshot, "log", "-p", "--all").stdout

            self.assertEqual(status, [])
            self.assertEqual(_git(snapshot, "rev-list", "--count", "HEAD").stdout.strip(), "1")
            self.assertEqual(
                (snapshot / "tracked.txt").read_text(encoding="utf-8"), "dirty\n"
            )
            self.assertEqual(
                (snapshot / "untracked.txt").read_text(encoding="utf-8"), "new\n"
            )
            self.assertNotIn(marker, history)
            self.assertNotEqual(
                _git(snapshot, "cat-file", "-e", secret_blob, check=False).returncode,
                0,
            )

    @unittest.skipUnless(GIT, "Git is required")
    def test_unborn_repository_gets_a_clean_visible_file_baseline(self) -> None:
        repository = self.root / "unborn"
        _initialize_repository(repository)
        (repository / "new.txt").write_text("new\n", encoding="utf-8")

        snapshot = create_workspace_snapshot(repository, LABEL)

        self.assertEqual(_git(snapshot, "rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertEqual(_git(snapshot, "status", "--short").stdout, "")
        self.assertEqual((snapshot / "new.txt").read_text(encoding="utf-8"), "new\n")

    @unittest.skipUnless(GIT, "Git is required")
    def test_empty_repository_gets_an_empty_root_commit(self) -> None:
        repository = self.root / "empty"
        _initialize_repository(repository)

        snapshot = create_workspace_snapshot(repository, LABEL)

        self.assertEqual(_git(snapshot, "rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertEqual(_git(snapshot, "status", "--short").stdout, "")

    @unittest.skipUnless(GIT, "Git is required")
    def test_source_object_format_is_not_trusted_or_imported(self) -> None:
        repository = self.root / "sha256"
        repository.mkdir()
        initialized = _git(
            repository,
            "init",
            "--quiet",
            "--object-format=sha256",
            check=False,
        )
        if initialized.returncode != 0:
            self.skipTest("installed Git does not support SHA-256 repositories")
        _git(repository, "config", "user.name", "Snapshot Test")
        _git(repository, "config", "user.email", "snapshot@example.invalid")
        (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "--quiet", "-m", "baseline")
        (repository / "tracked.txt").write_text("dirty\n", encoding="utf-8")

        snapshot = create_workspace_snapshot(repository, LABEL)

        object_format = _git(snapshot, "rev-parse", "--show-object-format").stdout.strip()
        self.assertEqual(object_format, "sha1")
        self.assertEqual(_git(snapshot, "status", "--short").stdout, "")
        self.assertEqual(
            (snapshot / "tracked.txt").read_text(encoding="utf-8"), "dirty\n"
        )

    @unittest.skipUnless(GIT, "Git is required")
    def test_model_controlled_git_metadata_is_never_a_git_process_input(self) -> None:
        from deepseek_mcp import git_snapshot

        repository = self.root / "hostile-repository"
        _initialize_repository(repository)
        outside = self.root / "outside-objects"
        outside.mkdir()
        info = repository / ".git" / "objects" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "alternates").write_text(str(outside), encoding="utf-8")
        (repository / ".git" / "config").write_text(
            "[remote \"origin\"]\n\tpromisor = true\n"
            "[filter \"hostile\"]\n\tclean = /tmp/never-run\n",
            encoding="utf-8",
        )
        (repository / "visible.txt").write_text("visible\n", encoding="utf-8")
        invoked_roots: list[Path] = []
        original_run = git_snapshot._run

        def record_root(git, root, arguments, deadline, **kwargs):
            invoked_roots.append(root)
            return original_run(git, root, arguments, deadline, **kwargs)

        with patch.object(git_snapshot, "_run", side_effect=record_root):
            snapshot = create_workspace_snapshot(repository, LABEL)

        self.assertTrue(invoked_roots)
        self.assertNotIn(repository, invoked_roots)
        self.assertTrue(all(root == snapshot for root in invoked_roots))
        self.assertEqual((snapshot / "visible.txt").read_text(), "visible\n")

    @unittest.skipUnless(GIT, "Git is required")
    def test_large_deleted_history_does_not_inflate_current_snapshot(self) -> None:
        repository = self.root / "long-history"
        _initialize_repository(repository)
        for index in range(8):
            payload = repository / f"deleted-{index}.bin"
            payload.write_bytes(bytes([index]) * 128_000)
            _git(repository, "add", payload.name)
            _git(repository, "commit", "--quiet", "-m", f"add {index}")
            payload.unlink()
            _git(repository, "add", "-u")
            _git(repository, "commit", "--quiet", "-m", f"delete {index}")
        (repository / "current.txt").write_text("small\n", encoding="utf-8")
        _git(repository, "add", "current.txt")
        _git(repository, "commit", "--quiet", "-m", "current")

        snapshot = create_workspace_snapshot(repository, LABEL)
        metadata_bytes = sum(
            entry.stat().st_size for entry in (snapshot / ".git").rglob("*")
            if entry.is_file()
        )

        self.assertLess(metadata_bytes, 256_000)
        self.assertEqual(_git(snapshot, "rev-list", "--count", "HEAD").stdout.strip(), "1")

    @unittest.skipUnless(GIT, "Git is required")
    def test_nested_repository_metadata_and_deleted_history_are_excluded(self) -> None:
        outer = self.root / "outer"
        _initialize_repository(outer)
        (outer / "outer.txt").write_text("outer\n", encoding="utf-8")
        _git(outer, "add", ".")
        _git(outer, "commit", "--quiet", "-m", "outer")
        nested = outer / "nested"
        _initialize_repository(nested)
        marker = "NESTED_HISTORY_PRIVATE_MARKER"
        (nested / "history.txt").write_text(marker, encoding="utf-8")
        (nested / "current.txt").write_text("current\n", encoding="utf-8")
        _git(nested, "add", ".")
        _git(nested, "commit", "--quiet", "-m", "history")
        (nested / "history.txt").unlink()
        _git(nested, "add", "-u")
        _git(nested, "commit", "--quiet", "-m", "delete history")
        metadata = nested / ".git"
        intermediate = nested / ".git-case-swap"
        metadata.rename(intermediate)
        intermediate.rename(nested / ".GIT")

        snapshot = create_workspace_snapshot(outer, LABEL)

        self.assertFalse((snapshot / "nested" / ".git").exists())
        self.assertFalse((snapshot / "nested" / ".GIT").exists())
        self.assertTrue((snapshot / "nested" / "current.txt").is_file())
        nested_git = _git(
            snapshot / "nested",
            "--git-dir",
            str(snapshot / "nested" / ".git"),
            "log",
            "-p",
            "--all",
            check=False,
        )
        self.assertNotEqual(nested_git.returncode, 0)
        self.assertNotIn(marker, nested_git.stdout + nested_git.stderr)

    def test_git_subprocess_timeout_kills_and_reaps_the_git_process(self) -> None:
        from deepseek_mcp.git_snapshot import GitSnapshotError, _run

        script = self.root / "slow-git"
        script.write_text(
            "#!/bin/sh\nexec sleep 30\n",
            encoding="utf-8",
        )
        script.chmod(0o700)

        with (
            patch("deepseek_mcp.git_snapshot._kill_process", wraps=lambda process: (
                process.kill(), process.communicate()
            )) as kill_process,
            self.assertRaisesRegex(GitSnapshotError, "time limit"),
        ):
            _run(str(script), self.root, [], time.monotonic() + 0.5)

        kill_process.assert_called_once()
        self.assertIsNotNone(kill_process.call_args.args[0].poll())


if __name__ == "__main__":
    unittest.main()
