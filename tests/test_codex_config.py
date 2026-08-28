from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tomlkit

from adapters.codex import configure as codex_configure
from adapters.codex import windows_file_io

ROOT = Path(__file__).resolve().parents[1]


class CodexConfigTransactionTests(unittest.TestCase):
    def test_windows_temp_delete_failure_does_not_mask_or_leak(self) -> None:
        parent = windows_file_io._Directory(7, "c:\\config")
        with (
            patch.object(windows_file_io, "_open_parent", return_value=parent),
            patch.object(windows_file_io, "_current_stat", return_value=None),
            patch.object(windows_file_io, "_open_child", return_value=9),
            patch.object(windows_file_io, "_descriptor", return_value=11),
            patch.object(
                windows_file_io, "_write_all", side_effect=OSError("write failed")
            ),
            patch.object(
                windows_file_io, "_mark_delete", side_effect=OSError("cleanup")
            ),
            patch.object(windows_file_io.os, "close") as close_file,
            patch.object(windows_file_io, "_close") as close_parent,
        ):
            with self.assertRaisesRegex(OSError, "write failed"):
                windows_file_io.atomic_write(Path("C:/config/file"), b"x", None)

        close_file.assert_called_once_with(11)
        close_parent.assert_called_once_with(7)

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self.root / "config.toml"
        self.backup = self.root / "backup.toml"
        self.manifest = self.root / "transaction.json"
        self.command = self.root / "generation.123" / "bin" / "deepseek-mcp"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _install(self, force: bool = False) -> dict[str, object]:
        return codex_configure.begin_transaction(
            self.config,
            self.backup,
            self.manifest,
            lambda document: codex_configure.configure_install(
                document, self.command, force
            ),
        )

    def _uninstall(self, force: bool = False) -> dict[str, object]:
        return codex_configure.begin_transaction(
            self.config,
            self.backup,
            self.manifest,
            lambda document: (
                codex_configure.configure_uninstall(document, force) or []
            ),
        )

    def test_fresh_install_writes_safe_defaults_and_can_roll_back(self) -> None:
        result = self._install()
        document = tomlkit.parse(self.config.read_text(encoding="utf-8"))
        server = document["mcp_servers"]["deepseek"]

        self.assertTrue(result["changed"])
        self.assertEqual(server["command"], str(self.command))
        self.assertIn(
            codex_configure.MANAGED_MARKER, server.trivia.comment
        )
        self.assertEqual(server["default_tools_approval_mode"], "writes")
        self.assertEqual(server["startup_timeout_sec"], 20)
        self.assertEqual(server["tool_timeout_sec"], 18060)
        self.assertEqual(list(server["enabled_tools"]), codex_configure.EXPOSED_TOOLS)
        self.assertEqual(
            list(server["env_vars"]), codex_configure.FORWARDED_ENV_VARS
        )
        self.assertEqual(
            server["tools"]["get_deepseek_result"]["approval_mode"], "approve"
        )
        self.assertEqual(
            server["tools"]["acknowledge_deepseek_mutations"]["approval_mode"],
            "approve",
        )
        self.assertEqual(
            server["tools"]["get_deepseek_recovery"]["approval_mode"],
            "approve",
        )

        codex_configure.rollback_transaction(self.manifest)
        self.assertFalse(self.config.exists())

    def test_owned_install_preserves_comments_and_custom_policy(self) -> None:
        original = b'''# keep this comment
[mcp_servers.other]
command = "other-mcp"

[mcp_servers.deepseek] # managed-by: deepseek-as-subagent
command = "/old/deepseek-as-subagent/.venv/bin/deepseek-mcp"
enabled_tools = ["ping"]
tool_timeout_sec = 24000
default_tools_approval_mode = "prompt"

[mcp_servers.deepseek.tools.ping]
approval_mode = "approve"
'''
        self.config.write_bytes(original)

        self._install()
        installed = self.config.read_text(encoding="utf-8")
        document = tomlkit.parse(installed)
        server = document["mcp_servers"]["deepseek"]

        self.assertIn("# keep this comment", installed)
        self.assertNotIn("args", server)
        self.assertNotIn("env", server)
        self.assertEqual(server["enabled_tools"], ["ping"])
        self.assertEqual(server["tool_timeout_sec"], 24000)
        self.assertEqual(server["default_tools_approval_mode"], "prompt")
        self.assertEqual(server["tools"]["ping"]["approval_mode"], "approve")
        self.assertEqual(list(server["env_vars"]), codex_configure.FORWARDED_ENV_VARS)

        codex_configure.rollback_transaction(self.manifest)
        self.assertEqual(self.config.read_bytes(), original)

    def test_owned_upgrade_migrates_legacy_five_hour_timeout(self) -> None:
        self.config.write_text(
            '''[mcp_servers.deepseek] # managed-by: deepseek-as-subagent
command = "/old/deepseek-as-subagent/.venv/bin/deepseek-mcp"
tool_timeout_sec = 18000
''',
            encoding="utf-8",
        )

        self._install()

        document = tomlkit.parse(self.config.read_text(encoding="utf-8"))
        self.assertEqual(
            document["mcp_servers"]["deepseek"]["tool_timeout_sec"], 18060
        )

    def test_owned_upgrade_adds_recovery_tools_to_custom_execution_policy(self) -> None:
        self.config.write_text(
            '''[mcp_servers.deepseek] # managed-by: deepseek-as-subagent
command = "/old/deepseek-as-subagent/.venv/bin/deepseek-mcp"
enabled_tools = ["delegate_to_deepseek"]
''',
            encoding="utf-8",
        )

        self._install()

        enabled = tomlkit.parse(self.config.read_text(encoding="utf-8"))[
            "mcp_servers"
        ]["deepseek"]["enabled_tools"]
        self.assertEqual(
            list(enabled),
            [
                "delegate_to_deepseek",
                "get_deepseek_recovery",
                "acknowledge_deepseek_mutations",
            ],
        )

    def test_owned_upgrade_rejects_disabled_recovery_with_execution(self) -> None:
        original = b'''[mcp_servers.deepseek] # managed-by: deepseek-as-subagent
command = "/old/deepseek-as-subagent/.venv/bin/deepseek-mcp"
enabled_tools = ["delegate_to_deepseek"]
disabled_tools = ["acknowledge_deepseek_mutations"]
'''
        self.config.write_bytes(original)

        with self.assertRaisesRegex(
            codex_configure.ConfigTransactionError, "cannot disable"
        ):
            self._install()

        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.manifest.exists())

    def test_owned_upgrade_rejects_unsafe_custom_timeout_without_writes(self) -> None:
        original = b'''[mcp_servers.deepseek] # managed-by: deepseek-as-subagent
command = "/old/deepseek-as-subagent/.venv/bin/deepseek-mcp"
tool_timeout_sec = 2400
'''
        self.config.write_bytes(original)

        with self.assertRaisesRegex(
            codex_configure.ConfigTransactionError, "at least 18060"
        ):
            self._install()

        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.manifest.exists())

    def test_owned_install_rejects_unapproved_forwarded_environment(self) -> None:
        original = '''[mcp_servers.deepseek] # managed-by: deepseek-as-subagent
command = "/old/deepseek-as-subagent/.venv/bin/deepseek-mcp"
env_vars = ["CUSTOM_ENV", "DEEPSEEK_MODE"]
'''
        self.config.write_text(
            original,
            encoding="utf-8",
        )

        with self.assertRaisesRegex(codex_configure.OwnershipError, "force-replace"):
            self._install()

        self.assertEqual(self.config.read_text(), original)
        self.assertFalse(self.manifest.exists())

    def test_foreign_same_name_entry_is_rejected_without_writes(self) -> None:
        original = b'[mcp_servers.deepseek]\ncommand = "unrelated-agent"\n'
        self.config.write_bytes(original)

        with self.assertRaises(codex_configure.OwnershipError):
            self._install()

        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.manifest.exists())

    def test_basename_alone_does_not_claim_legacy_ownership(self) -> None:
        original = b'[mcp_servers.deepseek]\ncommand = "/foreign/bin/deepseek-mcp"\n'
        self.config.write_bytes(original)

        with self.assertRaises(codex_configure.OwnershipError):
            self._install()

        self.assertEqual(self.config.read_bytes(), original)

    def test_force_replace_rebuilds_a_safe_server_table(self) -> None:
        self.config.write_text(
            '''[mcp_servers.deepseek]
command = "foreign-agent"
url = "https://example.invalid/mcp"
transport = "streamable-http"
bearer_token_env_var = "TOKEN_NAME"
args = ["--foreign"]
cwd = "/foreign"
enabled = false
enabled_tools = ["foreign_tool"]
default_tools_approval_mode = "auto"

[mcp_servers.deepseek.env]
MODE = "foreign"

[mcp_servers.deepseek.tools.ping]
approval_mode = "auto"
''',
            encoding="utf-8",
        )

        self._install(force=True)
        server = tomlkit.parse(self.config.read_text())["mcp_servers"]["deepseek"]

        self.assertEqual(server["command"], str(self.command))
        self.assertTrue(server["enabled"])
        self.assertEqual(list(server["enabled_tools"]), codex_configure.EXPOSED_TOOLS)
        self.assertEqual(server["default_tools_approval_mode"], "writes")
        self.assertEqual(
            server["tools"]["get_deepseek_result"]["approval_mode"], "approve"
        )
        self.assertEqual(
            set(server),
            {
                "command",
                "enabled",
                "enabled_tools",
                "startup_timeout_sec",
                "tool_timeout_sec",
                "default_tools_approval_mode",
                "env_vars",
                "tools",
            },
        )

    def test_invalid_toml_is_rejected_without_writes(self) -> None:
        original = b"[mcp_servers.deepseek\ncommand = 'broken'\n"
        self.config.write_bytes(original)

        with self.assertRaises(codex_configure.ConfigTransactionError):
            self._install()

        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.backup.exists())

    def test_rollback_refuses_to_overwrite_newer_edits(self) -> None:
        self.config.write_text(
            '[mcp_servers.deepseek] # managed-by: deepseek-as-subagent\n'
            'command = "/old/deepseek-as-subagent/.venv/bin/deepseek-mcp"\n',
            encoding="utf-8",
        )
        self._install()
        self.config.write_text(
            self.config.read_text(encoding="utf-8") + "\n# concurrent edit\n",
            encoding="utf-8",
        )

        with self.assertRaises(codex_configure.TransactionConflict):
            codex_configure.rollback_transaction(self.manifest)

        self.assertIn("# concurrent edit", self.config.read_text(encoding="utf-8"))
        self.assertTrue(self.backup.exists())

    @unittest.skipIf(os.name == "nt", "Windows does not preserve POSIX mode bits")
    def test_install_tightens_mode_and_rollback_restores_it(self) -> None:
        original = b'''[mcp_servers.deepseek] # managed-by: deepseek-as-subagent
command = "/managed/deepseek-mcp"
'''
        self.config.write_bytes(original)
        self.config.chmod(0o644)

        self._install()
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

        codex_configure.rollback_transaction(self.manifest)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o644)

    @unittest.skipIf(os.name == "nt", "POSIX dir-fd transaction test")
    def test_posix_atomic_write_rechecks_expected_snapshot(self) -> None:
        self.config.write_bytes(b"original")
        expected = codex_configure._read_snapshot(self.config)
        self.config.write_bytes(b"concurrent")

        with self.assertRaises(codex_configure.TransactionConflict):
            codex_configure._atomic_write(
                self.config, b"replacement", 0o600, expected
            )

        self.assertEqual(self.config.read_bytes(), b"concurrent")

    @unittest.skipIf(os.name == "nt", "POSIX dir-fd transaction test")
    def test_posix_delete_rechecks_expected_snapshot(self) -> None:
        self.config.write_bytes(b"original")
        expected = codex_configure._read_snapshot(self.config)
        self.config.write_bytes(b"concurrent")

        with self.assertRaises(codex_configure.TransactionConflict):
            codex_configure._delete_posix(self.config, expected)

        self.assertEqual(self.config.read_bytes(), b"concurrent")

    @unittest.skipIf(os.name == "nt", "POSIX no-follow transaction test")
    def test_posix_atomic_write_rejects_symlinked_parent(self) -> None:
        real_parent = self.root / "real"
        linked_parent = self.root / "linked"
        real_parent.mkdir()
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        target = linked_parent / "config.toml"

        with self.assertRaises(OSError):
            codex_configure._atomic_write(target, b"unsafe", 0o600)

        self.assertFalse((real_parent / "config.toml").exists())

    def test_optimistic_conflict_preserves_newer_config_and_backup(self) -> None:
        original = b'''[mcp_servers.deepseek] # managed-by: deepseek-as-subagent
command = "/managed/deepseek-mcp"
'''
        newer = original + b"\n# concurrent edit\n"
        self.config.write_bytes(original)
        atomic_write = codex_configure._atomic_write

        def inject_edit(path: Path, data: bytes, mode: int) -> None:
            atomic_write(path, data, mode)
            if path == self.manifest:
                self.config.write_bytes(newer)

        with patch.object(codex_configure, "_atomic_write", side_effect=inject_edit):
            with self.assertRaises(codex_configure.TransactionConflict):
                self._install()

        self.assertEqual(self.config.read_bytes(), newer)
        self.assertEqual(self.backup.read_bytes(), original)
        self.assertTrue(self.manifest.exists())

    def test_identical_bytes_with_new_metadata_are_a_conflict(self) -> None:
        original = b'''[mcp_servers.deepseek] # managed-by: deepseek-as-subagent
command = "/managed/deepseek-mcp"
'''
        self.config.write_bytes(original)
        atomic_write = codex_configure._atomic_write

        def inject_touch(path: Path, data: bytes, mode: int) -> None:
            atomic_write(path, data, mode)
            if path == self.manifest:
                current = self.config.stat()
                os.utime(
                    self.config,
                    ns=(current.st_atime_ns, current.st_mtime_ns + 2_000_000_000),
                )

        with patch.object(codex_configure, "_atomic_write", side_effect=inject_touch):
            with self.assertRaises(codex_configure.TransactionConflict):
                self._install()

        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.backup.read_bytes(), original)

    def test_cross_process_writer_waits_for_advisory_lease(self) -> None:
        child_backup = self.root / "child-backup.toml"
        child_manifest = self.root / "child-manifest.json"
        program = f'''
from pathlib import Path
from adapters.codex import configure as module
print("READY", flush=True)
module.begin_transaction(
    Path({str(self.config)!r}),
    Path({str(child_backup)!r}),
    Path({str(child_manifest)!r}),
    lambda document: module.configure_install(
        document, Path({str(self.command)!r}), False
    ),
)
'''
        with codex_configure._ConfigLease(self.config):
            process = subprocess.Popen(
                [sys.executable, "-c", program],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(process.stdout.readline().strip(), "READY")
            with self.assertRaises(subprocess.TimeoutExpired):
                process.wait(timeout=0.25)

        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        server = tomlkit.parse(self.config.read_text())["mcp_servers"]["deepseek"]
        self.assertEqual(server["command"], str(self.command))

    def test_cross_process_rollback_waits_for_advisory_lease(self) -> None:
        self._install()
        program = f'''
from pathlib import Path
from adapters.codex import configure as module
print("READY", flush=True)
module.rollback_transaction(Path({str(self.manifest)!r}))
'''
        with codex_configure._ConfigLease(self.config):
            process = subprocess.Popen(
                [sys.executable, "-c", program],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(process.stdout.readline().strip(), "READY")
            with self.assertRaises(subprocess.TimeoutExpired):
                process.wait(timeout=0.25)

        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertFalse(self.config.exists())

    def test_uninstall_removes_only_owned_server_and_is_reversible(self) -> None:
        original = b'''[mcp_servers.other]
command = "other-mcp"

[mcp_servers.deepseek] # managed-by: deepseek-as-subagent
command = "/managed/deepseek-mcp"
'''
        self.config.write_bytes(original)

        self._uninstall()
        document = tomlkit.parse(self.config.read_text(encoding="utf-8"))
        self.assertIn("other", document["mcp_servers"])
        self.assertNotIn("deepseek", document["mcp_servers"])

        codex_configure.rollback_transaction(self.manifest)
        self.assertEqual(self.config.read_bytes(), original)

    def test_uninstall_refuses_foreign_same_name_server(self) -> None:
        original = b'[mcp_servers.deepseek]\ncommand = "unrelated-agent"\n'
        self.config.write_bytes(original)

        with self.assertRaises(codex_configure.OwnershipError):
            self._uninstall()

        self.assertEqual(self.config.read_bytes(), original)

    @unittest.skipUnless(shutil.which("codex"), "codex CLI is not installed")
    def test_installed_codex_accepts_generated_configuration(self) -> None:
        self._install()
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.root)

        completed = subprocess.run(
            ["codex", "mcp", "get", "deepseek", "--json"],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout)

        self.assertEqual(payload["name"], "deepseek")
        self.assertEqual(payload["startup_timeout_sec"], 20.0)
        self.assertEqual(payload["tool_timeout_sec"], 18060.0)
        self.assertEqual(payload["enabled_tools"], codex_configure.EXPOSED_TOOLS)
        codex_configure.validate_registration_payload(
            payload, self.command, self.root
        )


if __name__ == "__main__":
    unittest.main()
