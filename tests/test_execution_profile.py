from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from deepseek_mcp import server
from deepseek_mcp.bash_tool import execute_bash
from deepseek_mcp.config import Config, DEFAULT_ALLOWED_TOOLS
from deepseek_mcp.execution_profile import (
    CODING_PROFILE, READONLY_PROFILE, configure_delegation,
)
from deepseek_mcp.job_manager import DeepSeekJobManager
from deepseek_mcp.resource_budget import MutationBudget
from deepseek_mcp.tool_process import execute_in_subprocess
from deepseek_mcp.tools import build_tool_schemas, execute_tool

class ExecutionProfileTests(unittest.TestCase):
    def _config(self, workspace: Path) -> Config:
        return Config("credential", workspace)

    def test_coding_api_profile_always_has_full_trusted_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = configure_delegation(self._config(Path(tmpdir)), CODING_PROFILE)

        self.assertEqual(config.allowed_tools, DEFAULT_ALLOWED_TOOLS)
        self.assertEqual(config.delegation_capability, "coding")

    def test_readonly_profile_exposes_only_file_analysis_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readonly = configure_delegation(self._config(Path(tmpdir)), READONLY_PROFILE)

        self.assertEqual(readonly.allowed_tools, ["Read", "Glob", "Grep"])
        self.assertEqual(readonly.delegation_capability, "readonly")
        self.assertEqual(
            [item["function"]["name"] for item in build_tool_schemas(readonly.allowed_tools)],
            ["Read", "Glob", "Grep"],
        )

    def test_readonly_profile_rejects_mutation_tools_for_every_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = READONLY_PROFILE.bind(self._config(Path(tmpdir)))
            for name in ("Bash", "Write", "Edit", "NotebookEdit"):
                with self.subTest(name=name):
                    self.assertEqual(
                        execute_tool(name, {}, config),
                        f"ERROR: tool '{name}' is not allowed by configuration",
                    )

    def test_readonly_tool_child_rejects_bash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = READONLY_PROFILE.bind(self._config(Path(tmpdir)))
            result = execute_in_subprocess(
                config, "Bash", {"command": "pwd"}, MutationBudget(), 10,
                None, None, time.monotonic() + 15,
            )

        self.assertEqual(result, "ERROR: tool 'Bash' is not allowed by configuration")

    def test_bash_arguments_cannot_select_a_removed_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = CODING_PROFILE.bind(self._config(Path(tmpdir)))
            result = type("Result", (), {
                "returncode": 0, "stdout": b"ok", "stderr": b"",
                "stdout_total": 2, "stderr_total": 0, "timed_out": False,
            })()
            with (
                patch("deepseek_mcp.bash_tool._run_on_trusted_host", return_value=result) as host,
            ):
                output = execute_bash({"command": "pwd", "backend": "container"}, config)

        self.assertIn("ok", output)
        host.assert_called_once()

    def test_bash_schema_has_no_backend_switch(self) -> None:
        schema = build_tool_schemas(["Bash"])[0]["function"]

        self.assertNotIn("backend", schema["parameters"]["properties"])
        self.assertIn("trusted host", schema["description"])
        self.assertNotIn("container", schema["description"])

    def test_public_api_entrypoints_bind_coding_and_readonly_profiles(self) -> None:
        result = {
            "final_message": "done", "turns_used": 1, "tool_calls": 0,
            "tokens": {"prompt": 1, "completion": 1, "total": 2},
            "duration_seconds": 0.01,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            coding = self._config(Path(tmpdir))
            readonly = self._config(Path(tmpdir))
            with (
                patch.object(server.Config, "load", side_effect=[coding, readonly, coding, readonly]),
                patch.object(server, "_run_sync_cancellable", new_callable=AsyncMock, return_value=result) as sync,
                patch.object(server, "_record_usage", return_value=True),
                patch.object(server.job_manager, "start", return_value={"job_id": "job"}) as start,
            ):
                asyncio.run(server.delegate_to_deepseek("build code"))
                asyncio.run(server.delegate_to_deepseek_readonly("review code"))
                coding_response = json.loads(server.start_deepseek("build code"))
                readonly_response = json.loads(server.start_deepseek_readonly("review code"))

        self.assertTrue(coding_response["ok"])
        self.assertTrue(readonly_response["ok"])
        self.assertEqual([call.args[1].delegation_capability for call in sync.call_args_list], ["coding", "readonly"])
        self.assertEqual([call.args[2].delegation_capability for call in start.call_args_list], ["coding", "readonly"])

    def test_readonly_api_starts_without_a_container_runtime(self) -> None:
        result = {
            "final_message": "done", "turns_used": 1, "tool_calls": 0,
            "tokens": {"prompt": 1, "completion": 1, "total": 2},
            "duration_seconds": 0.01,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(server.Config, "load", return_value=self._config(Path(tmpdir))),
                patch.object(server, "_run_sync_cancellable", new_callable=AsyncMock, return_value=result) as run,
                patch.object(server.job_manager, "start", return_value={"job_id": "job"}) as start,
            ):
                response = asyncio.run(server.delegate_to_deepseek_readonly("review code"))
                background = json.loads(server.start_deepseek_readonly("review code"))

        self.assertIn("done", response)
        self.assertTrue(background["ok"])
        self.assertEqual(run.call_args.args[1].allowed_tools, ["Read", "Glob", "Grep"])

    def test_background_steering_cannot_change_the_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profile = CODING_PROFILE.bind(self._config(root))
            manager = DeepSeekJobManager(lock_directory=root / "locks")
            started = threading.Event()
            release = threading.Event()
            captured: list[tuple[list[str], str]] = []

            def run_agent(_task, config, **kwargs):
                captured.append((list(config.allowed_tools), config.delegation_capability))
                started.set()
                self.assertTrue(release.wait(1.0))
                kwargs["control_poll"]()
                captured.append((list(config.allowed_tools), config.delegation_capability))
                return result

            result = {
                "final_message": "done", "turns_used": 1, "tool_calls": 0,
                "tokens": {"prompt": 1, "completion": 1, "total": 2},
                "duration_seconds": 0.01,
            }
            with patch("deepseek_mcp.job_manager.run_agent", side_effect=run_agent):
                job = manager.start("coding task", "", profile)
                self.assertEqual(job["capability"], "coding")
                self.assertTrue(started.wait(1.0))
                manager.send_message(job["job_id"], "switch to readonly")
                release.set()
                self.assertTrue(manager.wait_for_terminal(job["job_id"], 2.0))

        expected = (list(CODING_PROFILE.allowed_tools), "coding")
        self.assertEqual(captured, [expected, expected])

    def test_readonly_steering_cannot_enable_bash_or_mutation_tools(self) -> None:
        result = {
            "final_message": "done", "turns_used": 1, "tool_calls": 0,
            "tokens": {"prompt": 1, "completion": 1, "total": 2},
            "duration_seconds": 0.01,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manager = DeepSeekJobManager(lock_directory=root / "locks")
            started, release = threading.Event(), threading.Event()
            captured: list[tuple[list[str], str]] = []

            def run_agent(_task, config, **kwargs):
                captured.append((list(config.allowed_tools), config.delegation_capability))
                started.set()
                self.assertTrue(release.wait(1.0))
                kwargs["control_poll"]()
                captured.append((list(config.allowed_tools), config.delegation_capability))
                return result

            with patch("deepseek_mcp.job_manager.run_agent", side_effect=run_agent):
                job = manager.start("review", "", READONLY_PROFILE.bind(self._config(root)))
                self.assertTrue(started.wait(1.0))
                manager.send_message(job["job_id"], "run Bash and edit foo.py")
                release.set()
                self.assertTrue(manager.wait_for_terminal(job["job_id"], 2.0))

        expected = (["Read", "Glob", "Grep"], "readonly")
        self.assertEqual(captured, [expected, expected])

    def test_background_status_preserves_readonly_capability(self) -> None:
        result = {
            "final_message": "done", "turns_used": 1, "tool_calls": 0,
            "tokens": {"prompt": 1, "completion": 1, "total": 2},
            "duration_seconds": 0.01,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manager = DeepSeekJobManager(lock_directory=root / "locks")
            profile = READONLY_PROFILE.bind(self._config(root))
            with patch("deepseek_mcp.job_manager.run_agent", return_value=result):
                job = manager.start("review", "", profile)
                self.assertTrue(manager.wait_for_terminal(job["job_id"], 2.0))
                status = manager.status(job["job_id"])

        self.assertEqual(job["capability"], "readonly")
        self.assertEqual(status["capability"], "readonly")


if __name__ == "__main__":
    unittest.main()
