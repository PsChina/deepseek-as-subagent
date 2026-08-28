from __future__ import annotations

import tempfile
import unittest
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from deepseek_mcp.bash_tool import (
    _create_windows_job,
    _host_argv,
    _host_environment,
    _stop_host_process,
    execute_bash,
)
from deepseek_mcp.config import Config
from deepseek_mcp.tools import execute_tool

def _trusted_config(workspace: Path) -> Config:
    return Config(
        "sk-" + "test",
        workspace,
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
    )


class ExecuteBashTests(unittest.TestCase):
    def test_trusted_host_runs_in_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            command = "cd" if os.name == "nt" else "pwd"
            output = execute_bash({"command": command}, _trusted_config(workspace))

        self.assertIn(str(workspace), output)

    @unittest.skipIf(os.name == "nt", "POSIX shell command")
    def test_trusted_host_timeout_terminates_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = execute_bash(
                {"command": "sleep 30", "timeout": 1},
                _trusted_config(Path(tmpdir)),
            )

        self.assertEqual(
            output,
            "ERROR: command timed out after 1s; trusted-host process was terminated",
        )

    @unittest.skipIf(os.name == "nt", "POSIX process group")
    def test_trusted_host_completion_reaps_background_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            pid_file = workspace / "background.pid"
            output = execute_bash(
                {"command": "sleep 30 & echo $! > background.pid"},
                _trusted_config(workspace),
            )
            process_id = int(pid_file.read_text(encoding="utf-8"))
            status = subprocess.run(
                ["ps", "-p", str(process_id), "-o", "stat="],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertIn("[exit 0]", output)
        self.assertFalse(status.returncode == 0 and status.stdout.strip())

    def test_trusted_host_environment_excludes_provider_credentials(self) -> None:
        provider_key = "DEEPSEEK_" + "API_KEY"
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            "os.environ",
            {"PATH": "/bin", "HOME": "/private", provider_key: "not-passed"},
            clear=True,
        ):
            environment = _host_environment(Path(tmpdir))

        self.assertEqual(environment, {"PATH": "/bin", "HOME": tmpdir})

    @unittest.skipIf(os.name == "nt", "POSIX shell command")
    def test_trusted_host_bounds_stdout_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = execute_bash(
                {"command": "yes output | head -c 30000; yes error | head -c 30000 >&2"},
                _trusted_config(Path(tmpdir)),
            )

        self.assertIn("truncated, captured 25000 of 30000 bytes", output)

    def test_host_argv_selects_windows_command_processor(self) -> None:
        with patch("deepseek_mcp.bash_tool.os.name", "nt"), patch.dict(
            "os.environ", {"COMSPEC": r"C:\Windows\System32\cmd.exe"}, clear=True
        ):
            argv = _host_argv("echo ready")

        self.assertEqual(argv, [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c", "echo ready"])

    def test_host_argv_selects_posix_shell(self) -> None:
        with patch("deepseek_mcp.bash_tool.os.name", "posix"):
            argv = _host_argv("printf ready")

        self.assertEqual(argv, ["/bin/sh", "-c", "printf ready"])

    def test_windows_process_cleanup_kills_host_process(self) -> None:
        class Process:
            pid = 42
            returncode = None

            def poll(self):
                return self.returncode

            def kill(self):
                self.returncode = -9

            def wait(self, timeout):
                self.returncode = -9

        process = Process()
        with (
            patch("deepseek_mcp.bash_tool.os.name", "nt"),
            patch("deepseek_mcp.bash_tool.subprocess.run") as taskkill,
        ):
            stopped = _stop_host_process(process)  # type: ignore[arg-type]

        self.assertTrue(stopped)
        self.assertEqual(process.returncode, -9)
        taskkill.assert_called_once()

    def test_windows_host_job_kills_descendants_on_close(self) -> None:
        kernel = MagicMock()
        kernel.CreateJobObjectW.return_value = 42
        kernel.SetInformationJobObject.return_value = True
        kernel.AssignProcessToJobObject.return_value = True
        kernel.CloseHandle.return_value = True
        with (
            patch("deepseek_mcp.bash_tool.os.name", "nt"),
            patch(
                "deepseek_mcp.bash_tool.ctypes.WinDLL",
                return_value=kernel,
                create=True,
            ),
        ):
            job = _create_windows_job(SimpleNamespace(_handle=7))  # type: ignore[arg-type]
            assert job is not None
            self.assertTrue(job.close())

        kernel.AssignProcessToJobObject.assert_called_once_with(42, 7)
        kernel.CloseHandle.assert_called_once_with(42)

    def test_timeout_rejects_coercible_or_out_of_range_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _trusted_config(Path(tmpdir))
            for value in (True, 1.5, "7", 0, 601):
                with self.subTest(value=value), patch(
                    "deepseek_mcp.bash_tool._run_on_trusted_host"
                ) as run:
                    output = execute_bash(
                        {"command": "pwd", "timeout": value}, config
                    )

                self.assertIn("timeout must be", output)
                run.assert_not_called()

    def test_command_policy_remains_defence_in_depth(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _trusted_config(Path(tmpdir))
            with patch("deepseek_mcp.bash_tool._run_on_trusted_host") as run:
                output = execute_bash({"command": "curl https://example.com"}, config)

        self.assertIn("program 'curl' not allowed", output)
        run.assert_not_called()

    def test_execute_tool_denies_bash_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config("sk-test", Path(tmpdir), allowed_tools=["Read", "Glob", "Grep"])
            with patch("deepseek_mcp.tools.execute_bash") as run:
                output = execute_tool("Bash", {"command": "pwd"}, config)

        self.assertEqual(output, "ERROR: tool 'Bash' is not allowed by configuration")
        run.assert_not_called()

    def test_execute_tool_forwards_workspace_lease_to_bash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _trusted_config(Path(tmpdir))
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
