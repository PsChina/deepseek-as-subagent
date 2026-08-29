from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import deepseek_mcp.workspace_walk as workspace_walk
from deepseek_mcp import walk_support
from deepseek_mcp import windows_walk
from deepseek_mcp.tools import _execute_grep


class WindowsWalkPolicyTests(unittest.TestCase):
    def test_open_entry_rejects_early_eof_with_stable_metadata(self) -> None:
        info = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_size=5,
            st_dev=1,
            st_ino=2,
            st_mtime_ns=3,
            st_ctime_ns=4,
        )
        with (
            patch.object(walk_support.os, "fstat", return_value=info),
            patch.object(walk_support.os, "lseek"),
            patch.object(walk_support, "read_descriptor", return_value=b"x"),
            patch.object(windows_walk, "descriptor_change_time", return_value=1),
            self.assertRaisesRegex(ValueError, "changed while reading"),
        ):
            walk_support.read_open_entry(7, 100)

    def test_open_entry_rejects_same_inode_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "target.txt"
            target.write_bytes(b"before")
            original = target.stat()
            descriptor = os.open(target, os.O_RDWR)
            real_read = walk_support.read_descriptor

            def read_then_rewrite(fd: int, limit: int) -> bytes:
                data = real_read(fd, limit)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b"rivals")
                os.fsync(fd)
                os.utime(
                    target, ns=(original.st_atime_ns, original.st_mtime_ns)
                )
                return data

            try:
                with (
                    patch.object(
                        walk_support,
                        "read_descriptor",
                        side_effect=read_then_rewrite,
                    ),
                    self.assertRaisesRegex(ValueError, "changed while reading"),
                ):
                    walk_support.read_open_entry(descriptor, 100)
            finally:
                os.close(descriptor)

    def test_windows_alias_and_reserved_components_are_rejected(self) -> None:
        for part in (".. ", "child.", "stream:name", "CON", "com1.txt"):
            with self.subTest(part=part):
                self.assertFalse(windows_walk.safe_part(part))
        self.assertTrue(windows_walk.safe_part("source.py"))

    def test_windows_unsafe_base_is_rejected_before_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            with (
                patch("deepseek_mcp.workspace_walk.os.name", "nt"),
                self.assertRaisesRegex(ValueError, "unsafe Windows"),
            ):
                workspace_walk._resolve_base(".. ", workspace)

    def test_iterator_io_failure_is_not_reported_as_end_of_directory(self) -> None:
        class FailingIterator:
            def __next__(self):
                raise OSError("revoked")

            def close(self) -> None:
                pass

        frames = [SimpleNamespace(
            iterator=FailingIterator(), descriptor=None, guard_handle=None
        )]
        with self.assertRaisesRegex(ValueError, "traversal failed"):
            walk_support.next_entry(frames)
        self.assertEqual(frames, [])

    def test_entry_metadata_io_failure_is_not_silently_skipped(self) -> None:
        parent = SimpleNamespace(descriptor=7)
        with (
            patch.object(workspace_walk.os, "stat", side_effect=PermissionError()),
            self.assertRaisesRegex(ValueError, "traversal failed"),
        ):
            workspace_walk._entry_stat(parent, "private")

    def test_child_scandir_io_failure_is_not_silently_skipped(self) -> None:
        walk = object.__new__(workspace_walk.WorkspaceWalk)
        walk.pattern = SimpleNamespace(can_descend=lambda _states: True)
        walk._directories = 0
        walk.truncated = False
        parent = SimpleNamespace(depth=0, descriptor=None)
        with (
            patch.object(walk, "_new_frame", side_effect=PermissionError()),
            self.assertRaisesRegex(ValueError, "traversal failed"),
        ):
            walk._child_frame(
                Path("private"), ("private",), frozenset({0}), os.stat_result((0,) * 10), parent
            )


@unittest.skipIf(os.name == "nt", "deterministic symlink swap uses POSIX semantics")
class WorkspaceBaseRaceTests(unittest.TestCase):
    def test_early_close_releases_child_opened_for_yielded_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "nested" / "child").mkdir(parents=True)
            captured = []
            original = workspace_walk.WorkspaceWalk._child_frame

            def record_child(walk, *arguments, **kwargs):
                frame = original(walk, *arguments, **kwargs)
                if frame is not None:
                    captured.append(frame)
                return frame

            with patch.object(
                workspace_walk.WorkspaceWalk,
                "_child_frame",
                autospec=True,
                side_effect=record_child,
            ):
                walk = workspace_walk.WorkspaceWalk("", workspace, "**/*")
                next(walk)
                walk.close()

            self.assertEqual(len(captured), 1)
            descriptor = captured[0].descriptor
            self.assertIsNotNone(descriptor)
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_base_chain_swap_during_validation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, safe, outside = self._make_tree(Path(tmpdir))
            real_info = workspace_walk._real_path_info
            safe_display = safe.resolve()
            swapped = False

            def info_then_swap(path: Path):
                nonlocal swapped
                info = real_info(path)
                if Path(path) == safe_display and not swapped:
                    self._swap_ancestor(workspace, safe, outside)
                    swapped = True
                return info

            with (
                patch.object(workspace_walk, "_HAS_SECURE_DIR_FDS", False),
                patch.object(workspace_walk, "_real_path_info", side_effect=info_then_swap),
            ):
                result = self._grep_nested(workspace)

        self.assertTrue(swapped)
        self.assertTrue(result.startswith("ERROR:"), result)
        self.assertNotIn("outside-marker", result)

    def test_base_identity_is_rechecked_when_root_frame_opens(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, safe, outside = self._make_tree(Path(tmpdir))
            real_validate = workspace_walk._validate_base_path

            def validate_then_swap(root: Path, relative: Path):
                identity = real_validate(root, relative)
                self._swap_ancestor(workspace, safe, outside)
                return identity

            with (
                patch.object(workspace_walk, "_HAS_SECURE_DIR_FDS", False),
                patch.object(
                    workspace_walk,
                    "_validate_base_path",
                    side_effect=validate_then_swap,
                ),
            ):
                result = self._grep_nested(workspace)

        self.assertTrue(result.startswith("ERROR:"), result)
        self.assertNotIn("outside-marker", result)

    @staticmethod
    def _make_tree(root: Path) -> tuple[Path, Path, Path]:
        workspace, outside = root / "workspace", root / "outside"
        safe = workspace / "safe"
        (safe / "nested").mkdir(parents=True)
        (outside / "nested").mkdir(parents=True)
        (outside / "nested" / "secret.txt").write_text(
            "outside-marker", encoding="utf-8"
        )
        return workspace, safe, outside

    @staticmethod
    def _swap_ancestor(workspace: Path, safe: Path, outside: Path) -> None:
        safe.rename(workspace / "original-safe")
        safe.symlink_to(outside, target_is_directory=True)

    @staticmethod
    def _grep_nested(workspace: Path) -> str:
        return _execute_grep(
            {"pattern": "outside-marker", "path": "safe/nested"}, workspace
        )


if __name__ == "__main__":
    unittest.main()
