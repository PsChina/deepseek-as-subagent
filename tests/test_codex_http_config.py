"""URL-mode (streamable-HTTP) Codex registration policy tests."""
from __future__ import annotations

import unittest
from pathlib import Path

import tomlkit

from adapters.codex import configure_policy as policy

MANAGED_COMMAND = Path(
    "/home/user/.deepseek-mcp/codex-venvs/generation.abc123/bin/deepseek-mcp"
)
HTTP_URL = "http://127.0.0.1:8765/mcp"


def _entry(document) -> dict:
    return document["mcp_servers"]["deepseek"]


class CodexHttpPolicyTests(unittest.TestCase):
    def test_http_install_writes_url_and_skips_stdio_fields(self) -> None:
        document = tomlkit.document()
        policy.configure_install(
            document,
            None,
            False,
            url=HTTP_URL,
            bearer_token_env="DEEPSEEK_MCP_AUTH_TOKEN",
        )
        entry = _entry(document)
        self.assertEqual(entry["url"], HTTP_URL)
        self.assertNotIn("command", entry)
        self.assertNotIn("env_vars", entry)
        self.assertEqual(entry["bearer_token_env_var"], "DEEPSEEK_MCP_AUTH_TOKEN")
        self.assertIs(entry["enabled"], True)
        self.assertEqual(list(entry["enabled_tools"]), policy.EXPOSED_TOOLS)
        self.assertIn(policy.MANAGED_MARKER, str(entry.trivia.comment))

    def test_command_and_url_are_mutually_exclusive(self) -> None:
        document = tomlkit.document()
        with self.assertRaises(policy.ConfigTransactionError):
            policy.configure_install(document, MANAGED_COMMAND, url=HTTP_URL)
        document = tomlkit.document()
        with self.assertRaises(policy.ConfigTransactionError):
            policy.configure_install(document, None)

    def test_plain_http_url_is_loopback_only(self) -> None:
        document = tomlkit.document()
        for url in ("http://10.0.0.5:8765/mcp", "http://example.com/mcp"):
            with self.subTest(url=url), self.assertRaises(
                policy.ConfigTransactionError
            ):
                policy.configure_install(document, None, url=url)

    def test_https_remote_url_is_allowed(self) -> None:
        document = tomlkit.document()
        policy.configure_install(document, None, url="https://mcp.example.com/mcp")
        self.assertEqual(_entry(document)["url"], "https://mcp.example.com/mcp")

    def test_malformed_urls_are_rejected(self) -> None:
        document = tomlkit.document()
        for url in (
            "ftp://127.0.0.1:8765/mcp",
            "http://127.0.0.1:8765/mcp?token=x",
            "http://user:pass@127.0.0.1:8765/mcp",
            "http://127.0.0.1:8765/mcp path",
            "",
            None,
        ):
            with self.subTest(url=url), self.assertRaises(
                policy.ConfigTransactionError
            ):
                policy.configure_install(document, None, url=url)

    def test_bearer_token_env_requires_http_mode(self) -> None:
        document = tomlkit.document()
        with self.assertRaises(policy.ConfigTransactionError):
            policy.configure_install(
                document, MANAGED_COMMAND, bearer_token_env="DEEPSEEK_MCP_TOKEN"
            )
        document = tomlkit.document()
        with self.assertRaises(policy.ConfigTransactionError):
            policy.configure_install(
                document, None, url=HTTP_URL, bearer_token_env="not an env name"
            )

    def test_transport_switch_cleans_stale_fields(self) -> None:
        document = tomlkit.document()
        policy.configure_install(document, MANAGED_COMMAND)
        entry = _entry(document)
        self.assertIn("env_vars", entry)
        policy.configure_install(document, None, False, url=HTTP_URL)
        entry = _entry(document)
        self.assertEqual(entry["url"], HTTP_URL)
        self.assertNotIn("command", entry)
        self.assertNotIn("env_vars", entry)
        self.assertNotIn("bearer_token_env_var", entry)
        policy.configure_install(document, MANAGED_COMMAND)
        entry = _entry(document)
        self.assertEqual(entry["command"], str(MANAGED_COMMAND))
        self.assertNotIn("url", entry)

    def test_foreign_url_entry_is_not_adopted(self) -> None:
        document = tomlkit.parse(
            '[mcp_servers.deepseek]\nurl = "http://127.0.0.1:9999/mcp"\n'
        )
        with self.assertRaises(policy.OwnershipError):
            policy.configure_install(document, None, url=HTTP_URL)

    def test_uninstall_removes_owned_url_entry(self) -> None:
        document = tomlkit.document()
        policy.configure_install(document, None, url=HTTP_URL)
        policy.configure_uninstall(document)
        self.assertNotIn("deepseek", document["mcp_servers"])

    def test_validate_registration_payload_http_mode(self) -> None:
        payload = {
            "name": "deepseek",
            "enabled": True,
            "transport": {"type": "streamable_http", "url": HTTP_URL},
        }
        policy.validate_registration_payload(
            payload, MANAGED_COMMAND, Path("/home/user/.deepseek-mcp/codex-venvs"),
            HTTP_URL,
        )
        wrong = {
            "name": "deepseek",
            "enabled": True,
            "transport": {"type": "streamable_http", "url": "http://127.0.0.1:1/mcp"},
        }
        with self.assertRaises(policy.ConfigTransactionError):
            policy.validate_registration_payload(
                wrong, MANAGED_COMMAND, Path("/x"), HTTP_URL
            )
        stdio = {
            "name": "deepseek",
            "enabled": True,
            "transport": {"type": "stdio", "command": str(MANAGED_COMMAND)},
        }
        with self.assertRaises(policy.ConfigTransactionError):
            policy.validate_registration_payload(
                stdio, MANAGED_COMMAND, Path("/x"), HTTP_URL
            )
        # stdio mode still rejects HTTP-shaped payloads.
        with self.assertRaises(policy.ConfigTransactionError):
            policy.validate_registration_payload(
                payload, MANAGED_COMMAND, Path("/x"), None
            )


if __name__ == "__main__":
    unittest.main()
