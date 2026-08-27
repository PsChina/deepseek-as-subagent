from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp.bash_tool import execute_bash
from deepseek_mcp.config import Config
from deepseek_mcp.container_sandbox import ContainerResult, ContainerSandboxError
from deepseek_mcp.tools import execute_tool

PINNED_IMAGE = "example.invalid/deepseek-shell@sha256:" + ("a" * 64)


def _config(workspace: Path, *, bash: bool = True) -> Config:
    tools = ["Read", "Glob", "Grep", "Bash"] if bash else ["Read", "Glob", "Grep"]
    return Config(
        "sk-" + "test",
        workspace,
        allowed_tools=tools,
        bash_backend="container" if bash else None,
        bash_runtime="docker" if bash else None,
        bash_image=PINNED_IMAGE if bash else None,
    )


class ExecuteBashTests(unittest.TestCase):
    def test_formats_container_result_without_host_execution(self) -> None:
        result = ContainerResult(3, b"stdout", b"stderr", 6, 6, False)
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with patch("deepseek_mcp.bash_tool.run_in_container", return_value=result) as run:
                output = execute_bash({"command": "test -f missing"}, config)

        run.assert_called_once_with(
            "test -f missing", config, 60, lease_fd=None
        )
        self.assertEqual(
            output,
            "[exit 3]\n--- stdout ---\nstdout\n--- stderr ---\nstderr",
        )

    def test_timeout_reports_forced_container_cleanup(self) -> None:
        result = ContainerResult(-9, b"", b"", 0, 0, True)
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with patch("deepseek_mcp.bash_tool.run_in_container", return_value=result):
                output = execute_bash({"command": "sleep 30", "timeout": 7}, config)

        self.assertEqual(
            output,
            "ERROR: command timed out after 7s; container was force-removed",
        )

    def test_timeout_rejects_coercible_or_out_of_range_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            for value in (True, 1.5, "7", 0, 601):
                with self.subTest(value=value), patch(
                    "deepseek_mcp.bash_tool.run_in_container"
                ) as run:
                    output = execute_bash(
                        {"command": "pwd", "timeout": value}, config
                    )

                self.assertIn("timeout must be", output)
                run.assert_not_called()

    def test_missing_backend_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            error = ContainerSandboxError("Container runtime not found: docker")
            with patch("deepseek_mcp.bash_tool.run_in_container", side_effect=error):
                output = execute_bash({"command": "pwd"}, config)

        self.assertIn("container sandbox unavailable", output)

    def test_lease_fd_is_forwarded_to_container_lifecycle(self) -> None:
        result = ContainerResult(0, b"ok", b"", 2, 0, False)
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with patch("deepseek_mcp.bash_tool.run_in_container", return_value=result) as run:
                output = execute_bash({"command": "pwd"}, config, lease_fd=17)

        self.assertIn("ok", output)
        run.assert_called_once_with("pwd", config, 60, lease_fd=17)

    def test_format_failure_is_reported_after_confirmed_lifecycle(self) -> None:
        result = ContainerResult(0, b"ok", b"", 2, 0, False)
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with (
                patch("deepseek_mcp.bash_tool.run_in_container", return_value=result),
                patch(
                    "deepseek_mcp.bash_tool._format_result",
                    side_effect=ValueError("format broke"),
                ),
            ):
                output = execute_bash({"command": "pwd"}, config)

        self.assertEqual(output, "ERROR: failed to format container result: format broke")

    def test_command_policy_remains_defence_in_depth(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with patch("deepseek_mcp.bash_tool.run_in_container") as run:
                output = execute_bash({"command": "curl https://example.com"}, config)

        self.assertIn("program 'curl' not allowed", output)
        run.assert_not_called()

    def test_execute_tool_denies_bash_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir), bash=False)
            with patch("deepseek_mcp.tools.execute_bash") as run:
                output = execute_tool("Bash", {"command": "pwd"}, config)

        self.assertEqual(output, "ERROR: tool 'Bash' is not allowed by configuration")
        run.assert_not_called()

    def test_execute_tool_forwards_workspace_lease_to_bash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with patch("deepseek_mcp.tools.execute_bash", return_value="OK") as run:
                output = execute_tool(
                    "Bash",
                    {"command": "pwd"},
                    config,
                    execution_lease_fd=29,
                )

        self.assertEqual(output, "OK")
        run.assert_called_once_with(
            {"command": "pwd"},
            config,
            lease_fd=29,
        )


if __name__ == "__main__":
    unittest.main()
