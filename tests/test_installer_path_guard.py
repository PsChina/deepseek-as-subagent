from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "installer_path_guard.py"
sys.path.insert(0, str(ROOT / "scripts"))
import installer_path_guard as path_guard
import installer_asset_guard as asset_guard
from deepseek_mcp import windows_acl, windows_file_io


def _guard(*arguments: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GUARD), *arguments],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class InstallerPathGuardTests(unittest.TestCase):
    @staticmethod
    def _generation(root: Path, name: str, modified_ns: int) -> Path:
        generation = root / name
        (generation / "bin").mkdir(parents=True, mode=0o700)
        (generation / "bin" / "payload").write_bytes(b"installed")
        generation.chmod(0o700)
        os.utime(generation, ns=(modified_ns, modified_ns))
        return generation

    def test_directory_and_file_symlinks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target_dir = root / "target"
            target_dir.mkdir()
            directory_link = root / "config"
            directory_link.symlink_to(target_dir, target_is_directory=True)
            self.assertNotEqual(
                _guard("prepare-dirs", str(directory_link)).returncode, 0
            )

            safe = root / "safe"
            safe.mkdir(mode=0o700)
            target_file = root / "target.json"
            target_file.write_bytes(b"unchanged")
            file_link = safe / "config.json"
            file_link.symlink_to(target_file)
            result = _guard(
                "write-exclusive", str(file_link), input_bytes=b"replacement"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target_file.read_bytes(), b"unchanged")

    def test_failed_exclusive_write_never_removes_a_raced_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            root.chmod(0o700)
            destination = root / "config.json"

            def fail_after_replacement(_descriptor: int, _payload: object) -> int:
                destination.write_bytes(b"competitor")
                raise OSError("injected write failure")

            fake_stdin = SimpleNamespace(buffer=io.BytesIO(b"installer"))
            with (
                patch.object(path_guard.sys, "stdin", fake_stdin),
                patch.object(path_guard.os, "write", side_effect=fail_after_replacement),
                self.assertRaises(OSError),
            ):
                path_guard.write_exclusive(destination)

            self.assertEqual(destination.read_bytes(), b"competitor")
            self.assertEqual(list(root.glob(".config.json.deepseek-mcp.*")), [])

    @unittest.skipIf(os.name == "nt", "directory fsync is POSIX-specific")
    def test_directory_fsync_failure_keeps_committed_config_and_safe_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            root.chmod(0o700)
            destination = root / "config.json"
            warning = io.StringIO()
            first = SimpleNamespace(buffer=io.BytesIO(b"first committed value"))
            with (
                patch.object(path_guard.sys, "stdin", first),
                patch.object(path_guard.sys, "stderr", warning),
                patch.object(
                    path_guard, "_fsync_directory", side_effect=OSError("disk error")
                ),
            ):
                path_guard.write_exclusive(destination)

            self.assertEqual(destination.read_bytes(), b"first committed value")
            self.assertIn("configuration is published", warning.getvalue())
            retry = SimpleNamespace(buffer=io.BytesIO(b"must not overwrite"))
            with (
                patch.object(path_guard.sys, "stdin", retry),
                self.assertRaisesRegex(path_guard.GuardError, "appeared during publication"),
            ):
                path_guard.write_exclusive(destination)
            self.assertEqual(destination.read_bytes(), b"first committed value")
            self.assertEqual(list(root.glob(".config.json.deepseek-mcp.*")), [])

    def test_private_directory_creation_rejects_symlinked_claude_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            outside = Path(tmpdir) / "outside"
            home.mkdir(mode=0o700)
            outside.mkdir(mode=0o700)
            (home / ".claude").symlink_to(outside, target_is_directory=True)

            result = _guard(
                "prepare-private-dirs", str(home), ".claude", ".claude/skills"
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "POSIX mode semantics")
    def test_private_directory_validation_rejects_writable_component(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            claude = home / ".claude"
            claude.mkdir(parents=True, mode=0o700)
            home.chmod(0o700)
            claude.chmod(0o770)

            result = _guard("validate-private-dirs", str(home), ".claude")

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(stat_mode(claude), 0o770)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor semantics")
    def test_helper_regular_read_rejects_early_eof(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            helper = Path(tmpdir) / "helper.md"
            helper.write_bytes(b"complete payload")

            with (
                patch.object(asset_guard.os, "read", return_value=b""),
                self.assertRaisesRegex(asset_guard.AssetGuardError, "validated size"),
            ):
                asset_guard._read_posix_regular(helper)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor semantics")
    def test_helper_directory_enumeration_stops_after_two_entries(self) -> None:
        class TwoEntries:
            def __init__(self) -> None:
                self.calls = 0

            def __enter__(self) -> "TwoEntries":
                return self

            def __exit__(self, *_arguments: object) -> None:
                return None

            def __iter__(self) -> "TwoEntries":
                return self

            def __next__(self) -> object:
                self.calls += 1
                if self.calls <= 2:
                    return SimpleNamespace(name=f"entry-{self.calls}")
                raise AssertionError("enumerated more than two helper entries")

        with tempfile.TemporaryDirectory() as tmpdir:
            skill = Path(tmpdir) / "skill"
            skill.mkdir(mode=0o700)
            entries = TwoEntries()
            with (
                patch.object(asset_guard.os, "scandir", return_value=entries),
                self.assertRaisesRegex(asset_guard.AssetGuardError, "unexpected entries"),
            ):
                asset_guard._skill_file(skill)
            self.assertEqual(entries.calls, 2)

    def test_windows_directory_and_file_contract_uses_handle_acl_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            file = directory / "config.json"
            file.write_text("{}", encoding="utf-8")
            with (
                patch.object(path_guard, "_is_windows", return_value=True),
                patch.object(
                    windows_file_io, "validate_private_path"
                ) as validate,
            ):
                path_guard.secure_directory(directory, create=False)
                path_guard.secure_file(file, harden_mode=False)

        self.assertEqual(
            validate.call_args_list,
            [
                call(directory, directory=True),
                call(file, directory=False),
            ],
        )

    def test_windows_handle_acl_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            with (
                patch.object(path_guard, "_is_windows", return_value=True),
                patch.object(
                    windows_file_io,
                    "validate_private_path",
                    side_effect=windows_file_io.WindowsPathError("unsafe"),
                ),
                self.assertRaisesRegex(path_guard.GuardError, "handle and ACL"),
            ):
                path_guard.secure_directory(directory, create=False)

    def test_windows_drive_type_rejects_mapped_network_drive(self) -> None:
        with patch.object(
            windows_file_io, "_GET_DRIVE_TYPE", return_value=4, create=True
        ):
            with self.assertRaisesRegex(
                windows_file_io.WindowsPathError, "local drive"
            ):
                windows_file_io._require_local_drive("Z:")

    def test_windows_acl_uses_current_object_effective_aces(self) -> None:
        self.assertTrue(windows_acl._ace_applies_to_object(0))
        self.assertFalse(windows_acl._ace_applies_to_object(0x8))

    def test_windows_walk_checks_component_handles_and_only_final_acl(self) -> None:
        expected = "c:\\users\\alice\\.deepseek-mcp"
        with (
            patch.object(windows_file_io, "_absolute_local", return_value=expected),
            patch.object(
                windows_file_io, "_open", side_effect=[10, 11, 12, 13]
            ) as opened,
            patch.object(windows_file_io, "_validate_handle") as validated,
            patch.object(windows_file_io, "_validate_acl") as validated_acl,
            patch.object(windows_file_io, "_close"),
        ):
            result = windows_file_io._open_directory(Path(expected))

        self.assertEqual(result.handle, 13)
        self.assertEqual(opened.call_count, 4)
        self.assertEqual(validated.call_count, 4)
        validated_acl.assert_called_once_with(13)

    @unittest.skipUnless(os.name == "nt", "Windows handle and ACL integration")
    def test_windows_installer_path_guard_integration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir) / ".deepseek-mcp"
            directory.mkdir()
            config = directory / "config.json"
            config.write_text("{}", encoding="utf-8")

            path_guard.secure_directory(directory, create=False)
            path_guard.secure_file(config, harden_mode=False)

    @unittest.skipIf(os.name == "nt", "POSIX executable mode semantics")
    def test_venv_candidate_rejects_writable_external_target_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            generation = root / "generation.safe"
            binary = generation / "bin"
            binary.mkdir(parents=True, mode=0o700)
            generation.chmod(0o700)
            binary.chmod(0o700)
            marker = root / "executed"
            target = root / "attacker-python"
            target.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
            target.chmod(0o777)
            candidate = binary / "python"
            candidate.symlink_to(target)

            result = _guard("validate-venv", str(generation), str(candidate))

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())

    @unittest.skipIf(os.name == "nt", "POSIX ownership and mode semantics")
    def test_valid_private_venv_symlink_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "trusted-python"
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o500)
            generation = root / "generation.safe"
            binary = generation / "bin"
            binary.mkdir(parents=True, mode=0o700)
            generation.chmod(0o700)
            binary.chmod(0o700)
            candidate = binary / "python"
            candidate.symlink_to(target)

            result = _guard("validate-venv", str(generation), str(candidate))

        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_installers_route_sensitive_paths_through_guard(self) -> None:
        codex_install = (ROOT / "adapters/codex/install.sh").read_text(encoding="utf-8")
        codex_uninstall = (ROOT / "adapters/codex/uninstall.sh").read_text(encoding="utf-8")
        root_install = (ROOT / "install.sh").read_text(encoding="utf-8")
        for script in (codex_install, codex_uninstall, root_install):
            self.assertIn("installer_path_guard.py", script)
            self.assertRegex(script, r"(?:secure|validate)-files")
        self.assertIn("validate-venv", codex_uninstall)

    def test_windows_policy_rejects_reparse_candidate_outside_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            generation = root / "generation.safe"
            binary = generation / "Scripts"
            binary.mkdir(parents=True, mode=0o700)
            external = root / "outside.exe"
            external.write_bytes(b"not executed")
            external.chmod(0o700)
            candidate = binary / "python.exe"
            candidate.symlink_to(external)

            with (
                patch.object(path_guard, "_is_windows", return_value=True),
                patch.object(windows_file_io, "validate_private_path"),
                self.assertRaisesRegex(path_guard.GuardError, "reparse point"),
            ):
                path_guard.validate_venv_python(generation, candidate)

    def test_prune_keeps_current_and_newest_previous_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "venvs"
            root.mkdir(mode=0o755)
            oldest = self._generation(root, "generation.oldest", 1_000)
            newest_previous = self._generation(root, "generation.previous", 3_000)
            middle = self._generation(root, "generation.middle", 2_000)
            current = self._generation(root, "generation.current", 500)
            unrelated = root / "do-not-delete"
            unrelated.mkdir()

            result = _guard("prune-generations", str(root), str(current))

            self.assertEqual(result.returncode, 0, result.stderr.decode())
            if os.name != "nt":
                self.assertEqual(stat_mode(root), 0o700)
            self.assertTrue(current.is_dir())
            self.assertTrue(newest_previous.is_dir())
            self.assertFalse(middle.exists())
            self.assertFalse(oldest.exists())
            self.assertTrue(unrelated.is_dir())

    def test_prune_rejects_generation_symlink_without_deleting_anything(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "venvs"
            root.mkdir(mode=0o700)
            current = self._generation(root, "generation.current", 3_000)
            previous = self._generation(root, "generation.previous", 2_000)
            outside = Path(tmpdir) / "outside"
            outside.mkdir()
            (outside / "marker").write_bytes(b"keep")
            (root / "generation.attacker").symlink_to(
                outside, target_is_directory=True
            )

            result = _guard("prune-generations", str(root), str(current))

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(current.is_dir())
            self.assertTrue(previous.is_dir())
            self.assertEqual((outside / "marker").read_bytes(), b"keep")

    def test_prune_rejects_invalid_generation_name_before_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "venvs"
            root.mkdir(mode=0o700)
            current = self._generation(root, "generation.current", 3_000)
            oldest = self._generation(root, "generation.oldest", 1_000)
            (root / "generation.bad.name").mkdir()

            result = _guard("prune-generations", str(root), str(current))

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(current.is_dir())
            self.assertTrue(oldest.is_dir())

    def test_delete_generation_rejects_bad_name_and_outside_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "venvs"
            root.mkdir(mode=0o700)
            bad_name = root / "generation.bad.name"
            bad_name.mkdir()
            outside = Path(tmpdir) / "generation.outside"
            outside.mkdir()

            bad_result = _guard("delete-generation", str(root), str(bad_name))
            outside_result = _guard("delete-generation", str(root), str(outside))

            self.assertNotEqual(bad_result.returncode, 0)
            self.assertNotEqual(outside_result.returncode, 0)
            self.assertTrue(bad_name.is_dir())
            self.assertTrue(outside.is_dir())

    def test_delete_generation_removes_only_the_validated_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "venvs"
            root.mkdir(mode=0o700)
            target = self._generation(root, "generation.failed", 1_000)
            sibling = self._generation(root, "generation.keep", 2_000)

            result = _guard("delete-generation", str(root), str(target))

            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertFalse(target.exists())
            self.assertTrue(sibling.is_dir())

    def test_codex_installer_prunes_only_after_registration_is_verified(self) -> None:
        install = (ROOT / "adapters/codex/install.sh").read_text()
        verification = install.index("if ! verify_registration; then")
        success = install.index("INSTALL_SUCCEEDED=1", verification)
        pruning = install.index("prune-generations", success)

        self.assertLess(verification, success)
        self.assertLess(success, pruning)
        self.assertIn('mcp get deepseek --json', install)
        self.assertIn("validate_registration_payload", install)
        self.assertIn('"$PYBIN" -I -c', install)
        self.assertIn("if ! \"${PYTHON_CMD[@]}\" \"$PATH_GUARD\" prune-generations", install)
        self.assertIn("current install is usable", install)
        self.assertIn("delete-generation", install)
        self.assertNotIn("rm -rf", install)

    def test_codex_install_and_uninstall_share_a_fail_closed_mkdir_lease(self) -> None:
        install = (ROOT / "adapters/codex/install.sh").read_text()
        uninstall = (ROOT / "adapters/codex/uninstall.sh").read_text()

        for script in (install, uninstall):
            self.assertIn('ADAPTER_LOCK="$CONFIG_DIR/codex-adapter.lock"', script)
            self.assertIn('if ! mkdir "$ADAPTER_LOCK"', script)
            self.assertIn('rmdir "$ADAPTER_LOCK"', script)
            self.assertNotIn('rm -rf -- "$ADAPTER_LOCK"', script)
            self.assertIn("prior SIGKILL", script)
        self.assertLess(install.index('if ! mkdir "$ADAPTER_LOCK"'), install.index("mktemp -d"))
        self.assertLess(
            uninstall.index('if ! mkdir "$ADAPTER_LOCK"'),
            uninstall.index("if ! find_config_python; then"),
        )

    def test_codex_installer_avoids_empty_arrays_under_set_u(self) -> None:
        install = (ROOT / "adapters/codex/install.sh").read_text()
        uninstall = (ROOT / "adapters/codex/uninstall.sh").read_text()

        self.assertNotIn('${CONFIG_ARGS[@]}', install)
        self.assertIn('if [ "$FORCE_REPLACE" -eq 1 ]; then', install)
        self.assertIn("--force-replace", install)
        self.assertNotIn('${REMOVE_ARGS[@]}', uninstall)
        self.assertIn('if [ "$FORCE_REMOVE" -eq 1 ]; then', uninstall)

    def test_codex_rollbacks_mask_reentrant_signals_before_recovery(self) -> None:
        for name in ("install.sh", "uninstall.sh"):
            script = (ROOT / "adapters" / "codex" / name).read_text()
            on_exit = script.index("on_exit() {")
            masked = script.index("trap '' INT TERM HUP", on_exit)
            rollback = script.index('"$CONFIG_HELPER" rollback', on_exit)
            self.assertLess(masked, rollback, name)

    def test_path_guard_stays_within_source_size_limits(self) -> None:
        source = GUARD.read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), 500)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                span = (node.end_lineno or node.lineno) - node.lineno + 1
                self.assertLessEqual(span, 50, f"{node.name}: {span} lines")


if __name__ == "__main__":
    unittest.main()