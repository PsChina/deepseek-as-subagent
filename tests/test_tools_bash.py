from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deepseek_mcp.tools import DEFAULT_BASH_TIMEOUT, _execute_bash


class ExecuteBashTests(unittest.TestCase):
    def test_subprocess_stdin_is_isolated_from_mcp_stdio(self) -> None:
        completed = subprocess.CompletedProcess(
            args="echo ok",
            returncode=0,
            stdout=b"ok\n",
            stderr=b"",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            with patch("deepseek_mcp.tools.subprocess.run", return_value=completed) as run:
                result = _execute_bash({"command": "echo ok"}, workspace)

        run.assert_called_once_with(
            "echo ok",
            shell=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=False,
            timeout=DEFAULT_BASH_TIMEOUT,
            cwd=str(workspace),
        )
        self.assertEqual(result, "[exit 0]\n--- stdout ---\nok\n")

    def test_stdout_stderr_and_exit_code_behavior_is_unchanged(self) -> None:
        completed = subprocess.CompletedProcess(
            args="failing-command",
            returncode=3,
            stdout=b"stdout text",
            stderr=b"stderr text",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            with patch("deepseek_mcp.tools.subprocess.run", return_value=completed):
                result = _execute_bash({"command": "failing-command"}, workspace)

        self.assertEqual(
            result,
            "[exit 3]\n--- stdout ---\nstdout text\n--- stderr ---\nstderr text",
        )

    def test_timeout_behavior_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            with patch(
                "deepseek_mcp.tools.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="slow-command", timeout=7),
            ) as run:
                result = _execute_bash({"command": "slow-command", "timeout": 7}, workspace)

        self.assertEqual(result, "ERROR: command timed out after 7s")
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
