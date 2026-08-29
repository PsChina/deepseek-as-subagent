from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import tomlkit

from adapters.codex import configure as codex_configure


class CodexPolicySecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self.root / "config.toml"
        self.backup = self.root / "backup.toml"
        self.manifest = self.root / "transaction.json"
        self.venv_root = self.root / "codex-venvs"
        self.command = (
            self.venv_root / "generation.new" / "bin" / "deepseek-mcp"
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def _unmarked_config(command: Path) -> str:
        document = tomlkit.document()
        servers = tomlkit.table()
        deepseek = tomlkit.table()
        deepseek["command"] = str(command)
        servers["deepseek"] = deepseek
        document["mcp_servers"] = servers
        return tomlkit.dumps(document)

    def _install(self, force: bool = False) -> dict[str, object]:
        return codex_configure.begin_transaction(
            self.config,
            self.backup,
            self.manifest,
            lambda document: codex_configure.configure_install(
                document, self.command, force
            ),
        )

    def test_unmarked_broad_legacy_path_is_not_owned(self) -> None:
        original = (
            '[mcp_servers.deepseek]\n'
            'command = "/tmp/deepseek-as-subagent/.venv/bin/deepseek-mcp"\n'
        )
        self.config.write_text(original, encoding="utf-8")

        with self.assertRaisesRegex(codex_configure.OwnershipError, "force-replace"):
            self._install()

        self.assertEqual(self.config.read_text(encoding="utf-8"), original)
        self.assertFalse(self.manifest.exists())

    def test_new_command_must_be_a_direct_managed_generation_entrypoint(self) -> None:
        invalid = self.root / "not-a-generation" / "bin" / "deepseek-mcp"
        with self.assertRaisesRegex(
            codex_configure.ConfigTransactionError, "managed-generation"
        ):
            codex_configure.begin_transaction(
                self.config,
                self.backup,
                self.manifest,
                lambda document: codex_configure.configure_install(
                    document, invalid, False
                ),
            )
        self.assertFalse(self.config.exists())
        self.assertFalse(self.manifest.exists())

    def test_unmarked_direct_managed_generation_is_owned(self) -> None:
        previous = (
            self.venv_root / "generation.previous" / "bin" / "deepseek-mcp"
        )
        self.config.write_text(
            self._unmarked_config(previous), encoding="utf-8"
        )

        self._install()

        server = tomlkit.parse(self.config.read_text())["mcp_servers"]["deepseek"]
        self.assertEqual(server["command"], str(self.command))
        self.assertIn(codex_configure.MANAGED_MARKER, server.trivia.comment)

    def test_unmarked_non_direct_generation_paths_are_rejected(self) -> None:
        candidates = [
            self.venv_root / "generation.bad.name" / "bin" / "deepseek-mcp",
            self.venv_root / "generation.good" / "nested" / "bin" / "deepseek-mcp",
            self.venv_root / "generation.good" / "bin" / ".." / "bin" / "deepseek-mcp",
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                original = self._unmarked_config(candidate)
                self.config.write_text(original, encoding="utf-8")
                with self.assertRaises(codex_configure.OwnershipError):
                    self._install()
                self.assertEqual(
                    self.config.read_text(encoding="utf-8"), original
                )
                self.manifest.unlink(missing_ok=True)
                self.backup.unlink(missing_ok=True)

    def test_marked_launch_injection_requires_force_and_force_rebuilds(self) -> None:
        original = '''[mcp_servers.deepseek] # managed-by: deepseek-as-subagent
command = "/old/deepseek-mcp"
args = ["--load-plugin", "/tmp/evil"]
cwd = "/tmp/foreign"
env_vars = ["AWS_SECRET_ACCESS_KEY"]

[mcp_servers.deepseek.env]
TOKEN = "inline-secret"
'''
        self.config.write_text(original, encoding="utf-8")

        with self.assertRaisesRegex(codex_configure.OwnershipError, "force-replace"):
            self._install()
        self.assertEqual(self.config.read_text(), original)

        self._install(force=True)
        server = tomlkit.parse(self.config.read_text())["mcp_servers"]["deepseek"]
        self.assertNotIn("args", server)
        self.assertNotIn("cwd", server)
        self.assertNotIn("env", server)
        self.assertEqual(list(server["env_vars"]), codex_configure.FORWARDED_ENV_VARS)

    def _registration_payload(self) -> dict[str, object]:
        return {
            "name": "deepseek",
            "enabled": True,
            "transport": {
                "type": "stdio",
                "command": str(self.command),
                "args": [],
                "env": None,
                "env_vars": codex_configure.FORWARDED_ENV_VARS.copy(),
                "cwd": None,
            },
        }

    def test_registration_verifier_requires_exact_clean_managed_command(self) -> None:
        codex_configure.validate_registration_payload(
            self._registration_payload(), self.command, self.venv_root
        )
        mutations = {
            "wrong command": ("transport", "command", str(self.command) + "-other"),
            "args": ("transport", "args", ["--inject"]),
            "cwd": ("transport", "cwd", "/tmp"),
            "inline env": ("transport", "env", {"TOKEN": "secret"}),
            "extra env var": (
                "transport", "env_vars",
                codex_configure.FORWARDED_ENV_VARS + ["AWS_SECRET_ACCESS_KEY"],
            ),
        }
        for label, (section, key, value) in mutations.items():
            with self.subTest(label=label):
                payload = copy.deepcopy(self._registration_payload())
                payload[section][key] = value
                with self.assertRaises(codex_configure.ConfigTransactionError):
                    codex_configure.validate_registration_payload(
                        payload, self.command, self.venv_root
                    )

        outside = self.root / "outside" / "generation.fake" / "bin" / "deepseek-mcp"
        payload = self._registration_payload()
        payload["transport"]["command"] = str(outside)
        with self.assertRaisesRegex(
            codex_configure.ConfigTransactionError, "managed generation"
        ):
            codex_configure.validate_registration_payload(
                payload, outside, self.venv_root
            )

    def test_registration_absence_requires_a_valid_list_without_deepseek(self) -> None:
        codex_configure.validate_registration_absent([{"name": "other"}])
        for payload in (
            {"name": "other"},
            [{"name": "deepseek"}],
            [{"name": "other"}, "deepseek"],
            [{}],
            [{"name": 123}],
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(codex_configure.ConfigTransactionError):
                    codex_configure.validate_registration_absent(payload)


if __name__ == "__main__":
    unittest.main()
