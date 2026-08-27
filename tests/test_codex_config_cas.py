from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.codex import atomic_commit
from adapters.codex import configure as codex_configure

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "POSIX atomic commit tests")
class CodexConfigCASTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self.root / "config.toml"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _replace_target(self, data: bytes, label: str) -> None:
        candidate = self.root / label
        candidate.write_bytes(data)
        os.replace(candidate, self.config)

    def _recovery_contents(self) -> list[bytes]:
        names = self.root.glob(f".{self.config.name}.*.recovery")
        return [path.read_bytes() for path in names]

    def test_existing_commit_window_race_restores_concurrent_content(self) -> None:
        self.config.write_bytes(b"original")
        expected = codex_configure._read_snapshot(self.config)
        real_exchange = atomic_commit.exchange
        injected = False

        def inject(directory: int, source: str, target: str) -> None:
            nonlocal injected
            if not injected:
                injected = True
                self._replace_target(b"concurrent", "racer")
            real_exchange(directory, source, target)

        with patch.object(atomic_commit, "exchange", side_effect=inject):
            with self.assertRaises(codex_configure.TransactionConflict):
                codex_configure._atomic_write(
                    self.config, b"replacement", 0o600, expected
                )

        self.assertEqual(self.config.read_bytes(), b"concurrent")
        self.assertEqual(self._recovery_contents(), [])

    def test_second_exchange_race_preserves_both_external_versions(self) -> None:
        self.config.write_bytes(b"original")
        expected = codex_configure._read_snapshot(self.config)
        real_exchange = atomic_commit.exchange
        calls = 0

        def inject(directory: int, source: str, target: str) -> None:
            nonlocal calls
            calls += 1
            value = b"first concurrent" if calls == 1 else b"second concurrent"
            self._replace_target(value, f"racer-{calls}")
            real_exchange(directory, source, target)

        with patch.object(atomic_commit, "exchange", side_effect=inject):
            with self.assertRaises(codex_configure.TransactionConflict):
                codex_configure._atomic_write(
                    self.config, b"replacement", 0o600, expected
                )

        self.assertEqual(self.config.read_bytes(), b"first concurrent")
        self.assertEqual(self._recovery_contents(), [b"second concurrent"])

    def test_failed_recovery_publish_keeps_unknown_private_temp(self) -> None:
        self.config.write_bytes(b"original")
        expected = codex_configure._read_snapshot(self.config)
        real_exchange = atomic_commit.exchange
        calls = 0

        def inject(directory: int, source: str, target: str) -> None:
            nonlocal calls
            calls += 1
            value = b"first concurrent" if calls == 1 else b"second concurrent"
            self._replace_target(value, f"racer-{calls}")
            real_exchange(directory, source, target)

        with (
            patch.object(atomic_commit, "exchange", side_effect=inject),
            patch.object(
                atomic_commit, "move_no_clobber", side_effect=FileExistsError()
            ),
        ):
            with self.assertRaisesRegex(
                codex_configure.ConfigTransactionError, "private temporary"
            ):
                codex_configure._atomic_write(
                    self.config, b"replacement", 0o600, expected
                )

        temporary = list(self.root.glob(f".{self.config.name}.*.tmp"))
        self.assertEqual(self.config.read_bytes(), b"first concurrent")
        self.assertEqual([path.read_bytes() for path in temporary], [b"second concurrent"])

    def test_new_file_commit_is_no_clobber(self) -> None:
        expected = codex_configure._read_snapshot(self.config)
        real_link = os.link

        def inject(source: str, target: str, **kwargs: object) -> None:
            self._replace_target(b"concurrent", "racer")
            real_link(source, target, **kwargs)

        with patch.object(codex_configure.os, "link", side_effect=inject):
            with self.assertRaises(codex_configure.TransactionConflict):
                codex_configure._atomic_write(
                    self.config, b"replacement", 0o600, expected
                )

        self.assertEqual(self.config.read_bytes(), b"concurrent")

    def test_delete_window_race_restores_concurrent_content(self) -> None:
        self.config.write_bytes(b"original")
        expected = codex_configure._read_snapshot(self.config)
        real_move = atomic_commit.move_no_clobber
        injected = False

        def inject(directory: int, source: str, target: str) -> None:
            nonlocal injected
            if not injected:
                injected = True
                self._replace_target(b"concurrent", "racer")
            real_move(directory, source, target)

        with patch.object(atomic_commit, "move_no_clobber", side_effect=inject):
            with self.assertRaises(codex_configure.TransactionConflict):
                codex_configure._delete_posix(self.config, expected)

        self.assertEqual(self.config.read_bytes(), b"concurrent")

    def test_delete_recovery_never_discards_another_racing_writer(self) -> None:
        self.config.write_bytes(b"original")
        expected = codex_configure._read_snapshot(self.config)
        real_move = atomic_commit.move_no_clobber
        calls = 0

        def inject(directory: int, source: str, target: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                self._replace_target(b"first concurrent", "racer-1")
                real_move(directory, source, target)
                self.config.write_bytes(b"second concurrent")
                return
            real_move(directory, source, target)

        with patch.object(atomic_commit, "move_no_clobber", side_effect=inject):
            with self.assertRaises(codex_configure.TransactionConflict):
                codex_configure._delete_posix(self.config, expected)

        self.assertEqual(self.config.read_bytes(), b"second concurrent")
        self.assertEqual(self._recovery_contents(), [b"first concurrent"])

    def test_delete_audit_errors_restore_original_target(self) -> None:
        for error_type in (OSError, codex_configure.ConfigTransactionError):
            with self.subTest(error_type=error_type):
                self.config.write_bytes(b"original")
                expected = codex_configure._read_snapshot(self.config)
                error = error_type("injected deletion audit failure")

                with patch.object(
                    codex_configure, "_read_posix_snapshot", side_effect=error
                ):
                    with self.assertRaises(error_type):
                        codex_configure._delete_posix(self.config, expected)

                self.assertEqual(self.config.read_bytes(), b"original")
                self.assertEqual(list(self.root.glob("*.delete")), [])

    def test_delete_audit_failure_names_recovery_when_target_was_recreated(self) -> None:
        self.config.write_bytes(b"original")
        expected = codex_configure._read_snapshot(self.config)

        def fail_after_recreate(_directory: int, _name: str):
            self.config.write_bytes(b"concurrent")
            raise OSError("injected deletion audit failure")

        with patch.object(
            codex_configure, "_read_posix_snapshot", side_effect=fail_after_recreate
        ):
            with self.assertRaisesRegex(
                codex_configure.ConfigTransactionError, "preserved at .*recovery"
            ):
                codex_configure._delete_posix(self.config, expected)

        self.assertEqual(self.config.read_bytes(), b"concurrent")
        self.assertEqual(self._recovery_contents(), [b"original"])

    def test_replacement_fsync_failure_reports_committed_state(self) -> None:
        self.config.write_bytes(b"original")
        expected = codex_configure._read_snapshot(self.config)

        with patch.object(
            codex_configure.os,
            "fsync",
            side_effect=(None, OSError("injected directory sync failure")),
        ):
            with self.assertRaisesRegex(
                codex_configure.ConfigTransactionError, "replacement committed"
            ):
                codex_configure._atomic_write(
                    self.config, b"replacement", 0o600, expected
                )

        self.assertEqual(self.config.read_bytes(), b"replacement")

    def test_deletion_fsync_failure_reports_committed_state(self) -> None:
        self.config.write_bytes(b"original")
        expected = codex_configure._read_snapshot(self.config)

        with patch.object(
            codex_configure.os,
            "fsync",
            side_effect=OSError("injected directory sync failure"),
        ):
            with self.assertRaisesRegex(
                codex_configure.ConfigTransactionError, "deletion committed"
            ):
                codex_configure._delete_posix(self.config, expected)

        self.assertFalse(self.config.exists())
        self.assertEqual(list(self.root.glob("*.delete")), [])

    def test_new_publish_reports_private_link_cleanup_failure(self) -> None:
        expected = codex_configure._read_snapshot(self.config)
        with patch.object(
            atomic_commit,
            "discard",
            side_effect=PermissionError("injected cleanup failure"),
        ):
            with self.assertRaisesRegex(
                codex_configure.ConfigTransactionError, "creation committed"
            ):
                codex_configure._atomic_write(
                    self.config, b"replacement", 0o600, expected
                )

        self.assertEqual(self.config.read_bytes(), b"replacement")
        temporary = list(self.root.glob(f".{self.config.name}.*.tmp"))
        self.assertEqual([path.read_bytes() for path in temporary], [b"replacement"])

    def test_strict_discard_tolerates_only_missing_name(self) -> None:
        with patch.object(atomic_commit.os, "unlink", side_effect=FileNotFoundError()):
            atomic_commit.discard(7, "private")
        with patch.object(atomic_commit.os, "unlink", side_effect=PermissionError()):
            with self.assertRaises(PermissionError):
                atomic_commit.discard(7, "private")

    def test_unsupported_exchange_fails_closed(self) -> None:
        self.config.write_bytes(b"original")
        expected = codex_configure._read_snapshot(self.config)
        error = atomic_commit.UnsupportedAtomicCommit("unsupported")

        with patch.object(atomic_commit, "exchange", side_effect=error):
            with self.assertRaises(codex_configure.ConfigTransactionError):
                codex_configure._atomic_write(
                    self.config, b"replacement", 0o600, expected
                )

        self.assertEqual(self.config.read_bytes(), b"original")
        self.assertEqual(list(self.root.glob("*.tmp")), [])


class CodexManifestCLITests(unittest.TestCase):
    def test_rollback_cli_normalizes_malformed_manifest_structures(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        digest = "0" * 64
        wrong_types = {
            "version": "1",
            "config_path": 7,
            "backup_path": str(root / "backup"),
            "original_exists": "false",
            "original_mode": "384",
            "original_sha256": digest,
            "installed_sha256": digest,
        }
        payloads = ([], {"version": 1}, wrong_types)
        script = ROOT / "adapters" / "codex" / "configure.py"

        for index, payload in enumerate(payloads):
            with self.subTest(payload=payload):
                manifest = root / f"invalid-{index}.json"
                manifest.write_text(json.dumps(payload), encoding="utf-8")
                completed = subprocess.run(
                    [sys.executable, script, "rollback", "--manifest", manifest],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertIn("error: invalid transaction manifest", completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
