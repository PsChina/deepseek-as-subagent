from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp.config import (
    Config,
    DEFAULT_ALLOWED_TOOLS,
    DEFAULT_MAX_RUN_SECONDS,
    HARD_MAX_RUN_SECONDS,
    MAX_CONFIG_BYTES,
    MAX_TURNS,
    _load_api_key,
    _load_data,
    _load_workspace,
)
from deepseek_mcp import windows_file_io

class ConfigTests(unittest.TestCase):
    def test_broad_home_and_credential_workspaces_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir).resolve()
            project = home / "project"
            credential = home / ".ssh"
            project.mkdir()
            credential.mkdir()
            with patch("deepseek_mcp.safety.Path.home", return_value=home):
                for workspace in (home, credential):
                    with (
                        self.subTest(workspace=workspace),
                        self.assertRaisesRegex(RuntimeError, "protected|broad"),
                    ):
                        Config("credential", workspace, allowed_tools=["Read"])
                self.assertEqual(
                    Config(
                        "credential", project, allowed_tools=["Read"]
                    ).workspace,
                    project,
                )

    def test_filesystem_root_is_not_a_delegation_workspace(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "protected|broad"):
            Config("credential", Path("/"), allowed_tools=["Read"])

    def test_legacy_six_positional_arguments_keep_their_public_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            config = Config(
                "credential",
                workspace,
                "deepseek-chat",
                12,
                ["Read", "Glob"],
                "https://example.invalid/v1",
            )

        self.assertEqual(config.model, "deepseek-chat")
        self.assertEqual(config.max_turns, 12)
        self.assertEqual(config.allowed_tools, ["Read", "Glob"])
        self.assertEqual(config.base_url, "https://example.invalid/v1")
        self.assertEqual(config.max_run_seconds, DEFAULT_MAX_RUN_SECONDS)

    def test_config_identity_token_rejects_reused_workspace_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            config = Config("credential", workspace, allowed_tools=["Read"])
            token = config.expected_workspace_identity
            workspace.rename(root / "moved")
            workspace.mkdir()

            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                Config(
                    "credential",
                    workspace,
                    allowed_tools=["Read"],
                    expected_workspace_identity=token,
                )

    def test_windows_config_read_uses_handle_anchored_private_reader(self) -> None:
        path = Path("C:/Users/alice/.deepseek-mcp/config.json")
        with (
            patch("deepseek_mcp.config.CONFIG_PATH", path),
            patch("deepseek_mcp.config.os.name", "nt"),
            patch.object(
                windows_file_io,
                "read_regular",
                return_value=(b'{"model":"deepseek-v4-pro"}', object()),
            ) as read_regular,
        ):
            self.assertEqual(_load_data(), {"model": "deepseek-v4-pro"})

        read_regular.assert_called_once_with(path, max_bytes=MAX_CONFIG_BYTES)

    def test_config_rejects_duplicate_and_unknown_keys(self) -> None:
        cases = (
            ('{"allowed_tools": ["Read"], "allowed_tools": ["Write"]}', "Duplicate"),
            ('{"allowed_tool": ["Read"]}', "Unsupported"),
            ('{"bash_backend": "container"}', "Unsupported"),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir) / "config"
            directory.mkdir(mode=0o700)
            path = directory / "config.json"
            for payload, message in cases:
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    path.chmod(0o600)
                    with (
                        patch("deepseek_mcp.config.CONFIG_PATH", path),
                        self.assertRaisesRegex(RuntimeError, message),
                    ):
                        _load_data()

    def test_config_file_has_a_hard_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir) / "config"
            directory.mkdir(mode=0o700)
            path = directory / "config.json"
            path.write_bytes(b" " * (MAX_CONFIG_BYTES + 1))
            path.chmod(0o600)
            with (
                patch("deepseek_mcp.config.CONFIG_PATH", path),
                self.assertRaisesRegex(RuntimeError, "too large"),
            ):
                _load_data()

    def test_windows_unsafe_config_path_fails_closed(self) -> None:
        with (
            patch("deepseek_mcp.config.os.name", "nt"),
            patch.object(
                windows_file_io,
                "read_regular",
                side_effect=windows_file_io.WindowsPathError("unsafe ACL"),
            ),
            self.assertRaisesRegex(RuntimeError, "Cannot safely read"),
        ):
            _load_data()

    @unittest.skipUnless(os.name == "nt", "Windows handle and ACL integration")
    def test_windows_private_config_handle_integration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir) / ".deepseek-mcp"
            directory.mkdir()
            path = directory / "config.json"
            path.write_text("{}", encoding="utf-8")
            with patch("deepseek_mcp.config.CONFIG_PATH", path):
                self.assertEqual(_load_data(), {})

    def test_wall_clock_limit_is_configurable_with_finite_hard_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            self.assertEqual(Config("credential", workspace).max_run_seconds, 18000)
            self.assertEqual(DEFAULT_MAX_RUN_SECONDS, 18000)
            self.assertEqual(HARD_MAX_RUN_SECONDS, 48 * 60 * 60)
            self.assertEqual(
                Config(
                    "credential", workspace, max_run_seconds=36000
                ).max_run_seconds,
                36000,
            )
            for value in (True, "1800", 0, HARD_MAX_RUN_SECONDS + 1):
                with self.subTest(value=value), self.assertRaisesRegex(
                    RuntimeError, "max_run_seconds"
                ):
                    Config("credential", workspace, max_run_seconds=value)  # type: ignore[arg-type]

    def test_config_repr_never_contains_the_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            marker = "private-marker-value"
            config = Config(marker, Path(tmpdir), allowed_tools=["Read"])

        self.assertNotIn(marker, repr(config))

    def test_max_turns_has_a_strict_hard_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            self.assertEqual(Config("credential", workspace, max_turns=MAX_TURNS).max_turns, MAX_TURNS)
            for value in (True, "50", 1.5, 0, MAX_TURNS + 1):
                with self.subTest(value=value), self.assertRaisesRegex(
                    RuntimeError, "max_turns"
                ):
                    Config("credential", workspace, max_turns=value)  # type: ignore[arg-type]

    @unittest.skipUnless(
        os.name == "posix", "requires POSIX file modes"
    )
    def test_public_config_permissions_are_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o644)
            with patch("deepseek_mcp.config.CONFIG_PATH", path):
                with self.assertRaisesRegex(RuntimeError, "chmod 600"):
                    _load_data()
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    @unittest.skipUnless(
        os.name == "posix", "requires POSIX file modes"
    )
    def test_private_config_read_does_not_mutate_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o400)
            with patch("deepseek_mcp.config.CONFIG_PATH", path):
                self.assertEqual(_load_data(), {})
            self.assertEqual(path.stat().st_mode & 0o777, 0o400)

    @unittest.skipUnless(os.name == "posix", "requires POSIX file modes")
    def test_public_config_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir) / "config"
            directory.mkdir(mode=0o755)
            path = directory / "config.json"
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o600)
            with (
                patch("deepseek_mcp.config.CONFIG_PATH", path),
                self.assertRaisesRegex(RuntimeError, "mode 0700"),
            ):
                _load_data()

    def test_defaults_enable_workspace_file_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            with (
                patch("deepseek_mcp.config._load_data", return_value={}),
                patch("deepseek_mcp.config._load_api_key", return_value="credential"),
                patch("deepseek_mcp.config._load_workspace", return_value=workspace),
            ):
                config = Config.load()

        self.assertEqual(
            config.allowed_tools,
            ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "NotebookEdit"],
        )
        self.assertEqual(config.allowed_tools, DEFAULT_ALLOWED_TOOLS)

    def test_mutation_tools_reject_a_python_runtime_inside_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            runtime = workspace / "runtime"
            runtime.mkdir()
            with (
                patch(
                    "deepseek_mcp.child_runtime.trusted_runtime_paths",
                    return_value=(runtime.resolve(),),
                ),
                self.assertRaisesRegex(RuntimeError, "outside the delegated workspace"),
            ):
                Config(
                    "credential", workspace, allowed_tools=["Read", "Write"]
                )

    def test_windows_rejects_credentials_stored_in_config_files(self) -> None:
        field = "api_" + "key"
        data = {field: "not-a-real-credential"}
        with (
            patch("deepseek_mcp.config.os.name", "nt"),
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(RuntimeError, "cannot store API keys safely"),
        ):
            _load_api_key(data)
        with (
            patch("deepseek_mcp.config.os.name", "nt"),
            patch("deepseek_mcp.config._load_data", return_value=data),
            self.assertRaisesRegex(RuntimeError, "cannot store API keys safely"),
        ):
            Config.validate_runtime_settings()

    def test_runtime_validation_does_not_require_an_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch("deepseek_mcp.config._load_data", return_value={}),
                patch(
                    "deepseek_mcp.config._load_workspace",
                    return_value=Path(tmpdir),
                ),
                patch("deepseek_mcp.config._load_api_key") as load_key,
            ):
                Config.validate_runtime_settings()

        load_key.assert_not_called()

    def test_model_must_be_a_nonempty_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            for value in (None, 42, "", " model\n"):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(RuntimeError, "model"):
                        Config("credential", Path(tmpdir), model=value)  # type: ignore[arg-type]

    def test_base_url_requires_https_for_remote_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            for value in (42, "http://api.deepseek.com", "ftp://api.deepseek.com"):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(RuntimeError, "base_url"):
                        Config("credential", workspace, base_url=value)  # type: ignore[arg-type]

    def test_base_url_accepts_https_and_exact_loopback_http(self) -> None:
        urls = (
            "https://api.deepseek.com",
            "https://example.invalid/v1",
            "http://localhost:8000/v1",
            "http://127.0.0.1:8000",
            "http://[::1]:8000/v1",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            for url in urls:
                with self.subTest(url=url):
                    config = Config("credential", Path(tmpdir), base_url=url)
                    self.assertEqual(config.base_url, url)

    def test_base_url_rejects_lookalike_loopback_and_credentials(self) -> None:
        urls = (
            "http://localhost.example.com",
            "http://127.0.0.2",
            "https://user@example.invalid",
            "https://example.invalid/v1?token=value",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            for url in urls:
                with self.subTest(url=url):
                    with self.assertRaisesRegex(RuntimeError, "base_url"):
                        Config("credential", Path(tmpdir), base_url=url)

    def test_explicit_missing_workspace_fails_instead_of_falling_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing"
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "does not exist"):
                    _load_workspace({"workspace": str(missing)})

    def test_explicit_empty_workspace_fails_instead_of_falling_back(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "non-empty"):
                _load_workspace({"workspace": ""})


if __name__ == "__main__":
    unittest.main()
