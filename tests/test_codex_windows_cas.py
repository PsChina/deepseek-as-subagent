from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from adapters.codex import windows_file_io


class WindowsCASModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parent = windows_file_io._Directory(7, "c:\\config")
        self.target = "config.toml"
        self.expected = (1, 2, 8, 9, "expected-digest")
        self.transaction = (1, 3, 11, 10, "transaction-digest")

    def _commit_model(self, races: list[tuple]) -> dict[str, tuple]:
        state = {self.target: self.expected, "temp": self.transaction}
        replacements = 0

        def replace(_parent, target, replacement, backup) -> None:
            nonlocal replacements
            if replacements < len(races):
                state[target] = races[replacements]
            replacements += 1
            state[backup] = state.pop(target)
            state[target] = state.pop(replacement)

        def discard(_parent, name) -> None:
            state.pop(name, None)

        with (
            patch.object(windows_file_io, "_replace_with_backup", side_effect=replace),
            patch.object(
                windows_file_io, "_snapshot_name", side_effect=lambda _p, n: state.get(n)
            ),
            patch.object(windows_file_io, "_discard_name", side_effect=discard),
        ):
            with self.assertRaises(windows_file_io.WindowsPathError):
                windows_file_io._commit_replacement(
                    self.parent, self.target, "temp", self.expected, self.transaction
                )
        return state

    def test_replace_window_race_restores_displaced_writer(self) -> None:
        concurrent = (1, 4, 10, 11, "concurrent-digest")

        state = self._commit_model([concurrent])

        self.assertEqual(state, {self.target: concurrent})

    def test_recovery_window_race_preserves_both_external_versions(self) -> None:
        first = (1, 4, 5, 11, "first-digest")
        second = (1, 5, 6, 12, "second-digest")

        state = self._commit_model([first, second])

        self.assertEqual(state[self.target], first)
        self.assertIn(second, state.values())

    def test_failed_replace_restores_displaced_backup_without_deleting_temp(self) -> None:
        state = {self.target: self.expected, "temp": self.transaction}

        def fail(_parent, target, _replacement, backup) -> None:
            state[backup] = state.pop(target)
            raise OSError("ERROR_UNABLE_TO_MOVE_REPLACEMENT_2")

        def snapshot(_parent, name):
            return state.get(name)

        def move(_parent, source, target) -> None:
            if target in state:
                raise FileExistsError(target)
            state[target] = state.pop(source)

        with (
            patch.object(windows_file_io, "_replace_with_backup", side_effect=fail),
            patch.object(windows_file_io, "_snapshot_name", side_effect=snapshot),
            patch.object(windows_file_io, "_move_name", side_effect=move),
        ):
            with self.assertRaises(windows_file_io.WindowsPathError):
                windows_file_io._commit_replacement(
                    self.parent, self.target, "temp", self.expected, self.transaction
                )

        self.assertEqual(state[self.target], self.expected)
        self.assertEqual(state["temp"], self.transaction)

    def test_create_uses_no_clobber_handle_rename(self) -> None:
        info = SimpleNamespace(st_dev=1, st_ino=3, st_size=1, st_mtime_ns=4)
        with (
            patch.object(windows_file_io, "_open_parent", return_value=self.parent),
            patch.object(windows_file_io, "_current_stat", return_value=None),
            patch.object(windows_file_io, "_open_child", return_value=9),
            patch.object(windows_file_io, "_descriptor", return_value=11),
            patch.object(windows_file_io, "_write_all"),
            patch.object(windows_file_io.os, "fsync"),
            patch.object(windows_file_io.os, "fstat", return_value=info),
            patch.object(windows_file_io, "_identity", return_value=self.transaction),
            patch.object(windows_file_io, "_rename", side_effect=FileExistsError()),
            patch.object(windows_file_io, "_mark_delete") as mark_delete,
            patch.object(windows_file_io.os, "close"),
            patch.object(windows_file_io, "_close"),
        ):
            with self.assertRaises(FileExistsError):
                windows_file_io.atomic_write(Path("C:/config/config.toml"), b"x", None)

        mark_delete.assert_called_once_with(11)

    def test_delete_quarantines_then_verifies_before_marking_delete(self) -> None:
        info = SimpleNamespace(st_dev=1, st_ino=2, st_size=8, st_mtime_ns=9)
        concurrent = (1, 2, 10, 12, "concurrent-digest")
        with (
            patch.object(windows_file_io, "_open_parent", return_value=self.parent),
            patch.object(windows_file_io, "_open_child", return_value=9),
            patch.object(windows_file_io, "_descriptor", return_value=11),
            patch.object(windows_file_io.os, "fstat", return_value=info),
            patch.object(
                windows_file_io, "_read_open_descriptor", return_value=(b"new", info)
            ),
            patch.object(windows_file_io, "_identity", return_value=concurrent),
            patch.object(windows_file_io, "_rename") as rename,
            patch.object(windows_file_io, "_mark_delete") as mark_delete,
            patch.object(windows_file_io.os, "close"),
            patch.object(windows_file_io, "_close"),
        ):
            with self.assertRaises(windows_file_io.WindowsPathError):
                windows_file_io.delete_regular(
                    Path("C:/config/config.toml"), self.expected
                )

        self.assertEqual(rename.call_count, 2)
        self.assertEqual(rename.call_args_list[1], call(11, self.parent, self.target))
        mark_delete.assert_not_called()

    def test_delete_read_audit_failure_restores_target(self) -> None:
        info = SimpleNamespace(st_dev=1, st_ino=2, st_size=8, st_mtime_ns=9)
        with (
            patch.object(windows_file_io, "_open_parent", return_value=self.parent),
            patch.object(windows_file_io, "_open_child", return_value=9),
            patch.object(windows_file_io, "_descriptor", return_value=11),
            patch.object(windows_file_io.os, "fstat", return_value=info),
            patch.object(
                windows_file_io,
                "_read_open_descriptor",
                side_effect=OSError("injected audit read failure"),
            ),
            patch.object(windows_file_io, "_rename") as rename,
            patch.object(windows_file_io, "_mark_delete") as mark_delete,
            patch.object(windows_file_io.os, "close"),
            patch.object(windows_file_io, "_close_parent"),
        ):
            with self.assertRaisesRegex(
                windows_file_io.WindowsPathError, "audit failed; preserved at config.toml"
            ):
                windows_file_io.delete_regular(
                    Path("C:/config/config.toml"), self.expected
                )

        self.assertEqual(rename.call_count, 2)
        self.assertEqual(rename.call_args_list[1], call(11, self.parent, self.target))
        mark_delete.assert_not_called()

    def test_stable_reader_rejects_early_eof(self) -> None:
        info = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_dev=1,
            st_ino=2,
            st_size=4,
            st_mtime_ns=9,
        )
        with (
            patch.object(windows_file_io.os, "lseek"),
            patch.object(windows_file_io.os, "fstat", return_value=info),
            patch.object(windows_file_io, "_read_descriptor", return_value=b"x"),
        ):
            with self.assertRaisesRegex(windows_file_io.WindowsPathError, "validated size"):
                windows_file_io._read_open_descriptor(11)

    def test_stable_reader_rejects_non_regular_handle(self) -> None:
        info = SimpleNamespace(st_mode=stat.S_IFIFO, st_size=0)
        with (
            patch.object(windows_file_io.os, "lseek"),
            patch.object(windows_file_io.os, "fstat", return_value=info),
        ):
            with self.assertRaisesRegex(windows_file_io.WindowsPathError, "regular file"):
                windows_file_io._read_open_descriptor(11)

    def test_failed_existing_commit_does_not_cleanup_private_temp(self) -> None:
        info = SimpleNamespace(st_dev=1, st_ino=2, st_size=8, st_mtime_ns=9)
        with (
            patch.object(windows_file_io, "_open_parent", return_value=self.parent),
            patch.object(windows_file_io, "_current_stat", return_value=info),
            patch.object(windows_file_io, "_open_child", return_value=9),
            patch.object(windows_file_io, "_descriptor", return_value=11),
            patch.object(windows_file_io, "_write_all"),
            patch.object(windows_file_io.os, "fsync"),
            patch.object(windows_file_io.os, "fstat", return_value=info),
            patch.object(windows_file_io, "_identity", return_value=self.transaction),
            patch.object(
                windows_file_io,
                "_commit_replacement",
                side_effect=windows_file_io.WindowsPathError("failed safely"),
            ),
            patch.object(windows_file_io, "_mark_delete") as mark_delete,
            patch.object(windows_file_io.os, "close"),
            patch.object(windows_file_io, "_close"),
        ):
            with self.assertRaises(windows_file_io.WindowsPathError):
                windows_file_io.atomic_write(
                    Path("C:/config/config.toml"), b"replacement", self.expected
                )

        mark_delete.assert_not_called()

    def test_parent_walk_keeps_no_delete_share_handles_for_every_ancestor(self) -> None:
        absolute = "c:\\ancestor\\parent\\config.toml"
        with (
            patch.object(windows_file_io, "_absolute_local", return_value=absolute),
            patch.object(windows_file_io, "_open", side_effect=(10, 11, 12)) as opened,
            patch.object(
                windows_file_io,
                "_validate",
                side_effect=("c:\\", "c:\\ancestor", "c:\\ancestor\\parent"),
            ),
            patch.object(windows_file_io, "_validate_acl"),
            patch.object(windows_file_io, "_close") as close,
        ):
            parent = windows_file_io._open_parent(Path("ignored"))
            windows_file_io._close_parent(parent)

        self.assertEqual(parent.handle, 12)
        self.assertEqual(parent.ancestors, (10, 11))
        self.assertTrue(
            all(item.kwargs["share"] == windows_file_io._SHARE_RW for item in opened.call_args_list)
        )
        self.assertEqual(close.call_args_list, [call(12), call(11), call(10)])

    def test_parent_validation_failure_closes_once_and_preserves_error(self) -> None:
        validation_error = windows_file_io.WindowsPathError("validation failed")
        with (
            patch.object(
                windows_file_io,
                "_absolute_local",
                return_value="c:\\parent\\config.toml",
            ),
            patch.object(windows_file_io, "_open", side_effect=(10, 11)),
            patch.object(
                windows_file_io,
                "_validate",
                side_effect=("c:\\", validation_error),
            ),
            patch.object(
                windows_file_io,
                "_close",
                side_effect=(OSError("child close failed"), OSError("parent close failed")),
            ) as close,
        ):
            with self.assertRaisesRegex(
                windows_file_io.WindowsPathError, "validation failed"
            ):
                windows_file_io._open_parent(Path("ignored"))

        self.assertEqual(close.call_args_list, [call(11), call(10)])

    def test_delete_recreated_name_preserves_original_in_recovery(self) -> None:
        info = SimpleNamespace(st_dev=1, st_ino=2, st_size=8, st_mtime_ns=9)
        rename_effects = (None, FileExistsError(), None)
        with (
            patch.object(windows_file_io, "_open_parent", return_value=self.parent),
            patch.object(windows_file_io, "_open_child", return_value=9),
            patch.object(windows_file_io, "_descriptor", return_value=11),
            patch.object(windows_file_io.os, "fstat", return_value=info),
            patch.object(
                windows_file_io, "_read_open_descriptor", return_value=(b"original", info)
            ),
            patch.object(windows_file_io, "_identity", return_value=self.expected),
            patch.object(windows_file_io, "_current_stat", return_value=info),
            patch.object(
                windows_file_io, "_rename", side_effect=rename_effects
            ) as rename,
            patch.object(windows_file_io, "_mark_delete") as mark_delete,
            patch.object(windows_file_io.os, "close"),
            patch.object(windows_file_io, "_close_parent"),
        ):
            with self.assertRaisesRegex(
                windows_file_io.WindowsPathError, "recreated during deletion"
            ):
                windows_file_io.delete_regular(
                    Path("C:/config/config.toml"), self.expected
                )

        self.assertEqual(rename.call_count, 3)
        self.assertIn(".recovery", rename.call_args_list[-1].args[-1])
        mark_delete.assert_not_called()


@unittest.skipUnless(os.name == "nt", "real Windows ReplaceFileW integration")
class WindowsCASIntegrationTests(unittest.TestCase):
    def test_real_replacefile_race_preserves_concurrent_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = Path(raw) / "config.toml"
            config.write_bytes(b"original")
            data, info = windows_file_io.read_regular(config)
            expected = windows_file_io._identity(data, info)
            real_replace = windows_file_io._replace_with_backup
            injected = False

            def inject(parent, target, replacement, backup) -> None:
                nonlocal injected
                if not injected:
                    injected = True
                    racer = config.with_suffix(".racer")
                    racer.write_bytes(b"concurrent")
                    os.replace(racer, config)
                real_replace(parent, target, replacement, backup)

            with patch.object(
                windows_file_io, "_replace_with_backup", side_effect=inject
            ):
                with self.assertRaises(windows_file_io.WindowsPathError):
                    windows_file_io.atomic_write(config, b"replacement", expected)

            self.assertEqual(config.read_bytes(), b"concurrent")

    def test_real_recovery_race_preserves_both_concurrent_versions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / "config.toml"
            config.write_bytes(b"original")
            data, info = windows_file_io.read_regular(config)
            expected = windows_file_io._identity(data, info)
            real_replace = windows_file_io._replace_with_backup
            calls = 0

            def inject(parent, target, replacement, backup) -> None:
                nonlocal calls
                calls += 1
                value = b"first concurrent" if calls == 1 else b"second concurrent"
                racer = root / f"racer-{calls}"
                racer.write_bytes(value)
                os.replace(racer, config)
                real_replace(parent, target, replacement, backup)

            with patch.object(
                windows_file_io, "_replace_with_backup", side_effect=inject
            ):
                with self.assertRaises(windows_file_io.WindowsPathError):
                    windows_file_io.atomic_write(config, b"replacement", expected)

            recoveries = list(root.glob(".config.toml.*.recovery"))
            self.assertEqual(config.read_bytes(), b"first concurrent")
            self.assertEqual([path.read_bytes() for path in recoveries], [b"second concurrent"])

    def test_real_replace_holds_parent_ancestry_against_rename_and_junction_swap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ancestor = root / "ancestor"
            parent_dir = ancestor / "parent"
            alternate = root / "alternate"
            parent_dir.mkdir(parents=True)
            alternate.mkdir()
            junction = root / "junction"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", junction, alternate],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest("junction creation is unavailable")
            config = parent_dir / "config.toml"
            config.write_bytes(b"original")
            data, info = windows_file_io.read_regular(config)
            expected = windows_file_io._identity(data, info)
            real_replace = windows_file_io._replace_with_backup

            def assert_locked(parent, target, replacement, backup) -> None:
                with self.assertRaises(OSError):
                    os.rename(parent_dir, root / "moved-parent")
                with self.assertRaises(OSError):
                    os.rename(ancestor, root / "moved-ancestor")
                with self.assertRaises(OSError):
                    os.rename(parent_dir, root / "junction-swap-parked")
                self.assertTrue(junction.exists())
                real_replace(parent, target, replacement, backup)

            with patch.object(
                windows_file_io, "_replace_with_backup", side_effect=assert_locked
            ):
                windows_file_io.atomic_write(config, b"replacement", expected)

            self.assertEqual(config.read_bytes(), b"replacement")

    def test_real_delete_recreated_target_is_preserved_with_original_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / "config.toml"
            config.write_bytes(b"original")
            data, info = windows_file_io.read_regular(config)
            expected = windows_file_io._identity(data, info)
            real_current = windows_file_io._current_stat
            injected = False

            def inject(parent, name):
                nonlocal injected
                if not injected:
                    injected = True
                    config.write_bytes(b"concurrent")
                return real_current(parent, name)

            with patch.object(windows_file_io, "_current_stat", side_effect=inject):
                with self.assertRaises(windows_file_io.WindowsPathError):
                    windows_file_io.delete_regular(config, expected)

            recoveries = list(root.glob(".config.toml.*.recovery"))
            self.assertEqual(config.read_bytes(), b"concurrent")
            self.assertEqual([path.read_bytes() for path in recoveries], [b"original"])


if __name__ == "__main__":
    unittest.main()
