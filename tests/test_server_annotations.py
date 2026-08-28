from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deepseek_mcp import server
from deepseek_mcp.agent_loop import AgentLoopCancelled, _call_with_retry
from deepseek_mcp.provider_retry import MutationOutcomeError
from deepseek_mcp.config import Config
from deepseek_mcp.job_manager import DeepSeekJobManager, JobBusy
from deepseek_mcp.private_logging import MAX_LOG_BYTES

mcp = server.mcp


def _isolated_home_environment(home: str) -> dict[str, str]:
    """Point both POSIX and Windows home discovery at a disposable profile."""
    environment = os.environ.copy()
    isolated_home = os.path.abspath(home)
    home_drive, home_path = os.path.splitdrive(isolated_home)
    environment.update(
        {
            "HOME": isolated_home,
            "USERPROFILE": isolated_home,
            "HOMEDRIVE": home_drive,
            "HOMEPATH": home_path or os.sep,
        }
    )
    return environment


def _wait_for_paths(paths: list[Path], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not all(path.exists() for path in paths):
        if time.monotonic() >= deadline:
            raise AssertionError("logging stress workers did not become ready")
        time.sleep(0.01)


class ServerAnnotationTests(unittest.TestCase):
    def test_recovery_endpoints_query_and_ack_without_provider_load(self) -> None:
        config = Config("", ROOT, allowed_tools=[])
        record = {
            "transaction_id": "a" * 32,
            "tool": "Write",
            "path": "target.txt",
            "sha256": "b" * 64,
            "status": "committed",
            "warnings": [],
        }
        with (
            patch.object(server, "load_recovery_config", return_value=config),
            patch.object(server, "query_with_lease", return_value=[record]),
            patch.object(
                server,
                "acknowledge_with_lease",
                return_value=(["a" * 32], []),
            ),
            patch.object(server.Config, "load") as provider_load,
        ):
            queried = json.loads(server.get_deepseek_recovery())
            acknowledged = json.loads(
                server.acknowledge_deepseek_mutations(["a" * 32])
            )

        self.assertEqual(queried["pending"], [record])
        self.assertEqual(acknowledged["acknowledged"], ["a" * 32])
        provider_load.assert_not_called()

    def test_first_instruction_window_contains_recovery_contract(self) -> None:
        prefix = server._HOST_INSTRUCTIONS[:512]
        self.assertIn("get_deepseek_recovery", prefix)
        self.assertIn("acknowledge_deepseek_mutations", prefix)

    def test_background_usage_claim_is_released_when_persistence_fails(self) -> None:
        result = {
            "duration_seconds": 1.0,
            "turns_used": 1,
            "tool_calls": 0,
            "tokens": {"total": 2},
        }
        manager = Mock()
        manager.result_with_usage_claim.return_value = (
            {"ready": True, "status": "completed", "result": result},
            (4, result),
        )
        with (
            patch.object(server, "job_manager", manager),
            patch.object(server, "_record_usage", return_value=False),
        ):
            payload = server.get_deepseek_result("job")

        self.assertTrue(payload)
        manager.finish_usage_record.assert_called_once_with("job", False)

    def test_invalid_mode_fails_closed_before_configuration_or_execution(self) -> None:
        with (
            patch.dict(os.environ, {"DEEPSEEK_MODE": "of"}),
            patch.object(server.Config, "load") as load,
        ):
            health = server.ping()
            with self.assertRaisesRegex(server.JobError, "exactly"):
                server._load_config()
            delegated = asyncio.run(server.delegate_to_deepseek("do work"))

        load.assert_not_called()
        self.assertIn("mode=invalid", health)
        self.assertIn("DEEPSEEK_MODE", delegated)

    def test_server_fails_closed_when_core_dumps_cannot_be_disabled(self) -> None:
        with (
            patch.object(server, "disable_core_dumps", side_effect=RuntimeError),
            patch.object(server.mcp, "run") as run,
            self.assertRaises(SystemExit),
        ):
            server.main()

        run.assert_not_called()

    @unittest.skipIf(os.name == "nt", "persistent logs are disabled on Windows")
    def test_runtime_logging_rotates_an_oversized_server_log_on_startup(self) -> None:
        program = '''
import logging
from deepseek_mcp import server
server._ensure_runtime_logging()
logging.getLogger("deepseek_mcp.server").warning("new-record")
'''
        with tempfile.TemporaryDirectory() as home:
            log_dir = Path(home) / ".deepseek-mcp"
            log_dir.mkdir(mode=0o700)
            old = b"x" * (MAX_LOG_BYTES + 1)
            (log_dir / "server.log").write_bytes(old)
            environment = _isolated_home_environment(home)
            environment["PYTHONPATH"] = str(ROOT / "src")
            subprocess.run(
                [sys.executable, "-c", program], cwd=ROOT, env=environment,
                check=True, capture_output=True, text=True,
            )
            current = (log_dir / "server.log").read_text(encoding="utf-8")
            rotated = (log_dir / "server.log.1").read_bytes()

        self.assertIn("new-record", current)
        self.assertEqual(rotated, old)

    @unittest.skipIf(os.name == "nt", "persistent logs are disabled on Windows")
    def test_runtime_logging_is_transactional_across_processes(self) -> None:
        program = '''
import logging
import sys
import time
from pathlib import Path
from deepseek_mcp import server
server._ensure_runtime_logging()
logger = logging.getLogger("deepseek_mcp.stress")
marker = sys.argv[1]
gate = Path(sys.argv[2])
gate.with_name(f"{gate.name}.{marker}").touch()
while not gate.exists():
    time.sleep(0.001)
for index in range(64):
    logger.warning("%s:%03d:%s", marker, index, "x" * 32768)
'''
        with tempfile.TemporaryDirectory() as home:
            log_dir = Path(home) / ".deepseek-mcp"
            log_dir.mkdir(mode=0o700)
            old = b"z" * (MAX_LOG_BYTES + 1)
            (log_dir / "server.log").write_bytes(old)
            environment = _isolated_home_environment(home)
            environment["PYTHONPATH"] = str(ROOT / "src")
            gate = Path(home) / "start"
            ready = [gate.with_name(f"start.worker-{index}") for index in range(6)]
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", program, f"worker-{index}", str(gate)],
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for index in range(6)
            ]
            try:
                _wait_for_paths(ready)
                gate.touch()
                completed = [process.communicate(timeout=30) for process in processes]
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
            current = (log_dir / "server.log").read_bytes()
            rotated = (log_dir / "server.log.1").read_bytes()

        self.assertTrue(
            all(process.returncode == 0 for process in processes), completed
        )
        self.assertGreater(len(current), MAX_LOG_BYTES - 65536)
        self.assertLessEqual(len(current), MAX_LOG_BYTES)
        self.assertTrue(all(line.endswith(b"\n") for line in current.splitlines(True)))
        self.assertTrue(all(b"worker-" in line for line in current.splitlines()))
        self.assertEqual(rotated, old)

    @unittest.skipIf(os.name == "nt", "POSIX no-follow log boundary")
    def test_runtime_logging_rejects_symlinked_server_log(self) -> None:
        program = '''
import logging
from deepseek_mcp import server
server._ensure_runtime_logging()
logging.getLogger("deepseek_mcp.server").warning("must-not-escape")
'''
        with tempfile.TemporaryDirectory() as home:
            log_dir = Path(home) / ".deepseek-mcp"
            log_dir.mkdir(mode=0o700)
            target = Path(home) / "outside.log"
            target.write_text("original", encoding="utf-8")
            (log_dir / "server.log").symlink_to(target)
            environment = _isolated_home_environment(home)
            environment["PYTHONPATH"] = str(ROOT / "src")
            subprocess.run(
                [sys.executable, "-c", program], cwd=ROOT, env=environment,
                check=True, capture_output=True, text=True,
            )

            self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_api_body_is_redacted_across_delegate_result_and_server_log(self) -> None:
        marker = "delegate-provider-body-secret-marker"
        config = Config(
            api_key="sk-test", workspace=ROOT, allowed_tools=["Read"]
        )

        def run_sync(_task: str, active_config: Config, _cancel_signal=None):
            return _call_with_retry(active_config, [], [], 0)

        with (
            patch.object(server.Config, "load", return_value=config),
            patch("deepseek_mcp.config.runtime_is_within_workspace", return_value=False),
            patch.object(server.job_manager, "run_sync", side_effect=run_sync),
            patch(
                "deepseek_mcp.provider_retry.request_in_subprocess",
                return_value=(None, "category=api", False),
            ) as request,
            self.assertLogs("deepseek_mcp.server", level="ERROR") as logs,
        ):
            result = asyncio.run(server.delegate_to_deepseek(marker))

        self.assertEqual(result, "ERROR: DeepSeek agent loop failed")
        self.assertNotIn(marker, result)
        self.assertNotIn(marker, "\n".join(logs.output))
        self.assertEqual(request.call_count, 1)

    def test_sync_delegate_preserves_safe_do_not_retry_mutation_outcome(self) -> None:
        config = Config("sk-test", ROOT, allowed_tools=["Read"])
        message = (
            "tool interrupted after mutation intent; update committed; "
            f"transaction {'a' * 32}; DO NOT RETRY"
        )
        with (
            patch.object(server.Config, "load", return_value=config),
            patch("deepseek_mcp.config.runtime_is_within_workspace", return_value=False),
            patch.object(
                server,
                "_run_sync_cancellable",
                side_effect=MutationOutcomeError(message),
            ),
            self.assertLogs("deepseek_mcp.server", level="ERROR"),
        ):
            result = asyncio.run(server.delegate_to_deepseek("edit safely"))

        self.assertEqual(result, f"ERROR: {message}")

    def test_sync_delegate_keeps_event_loop_live_and_awaits_cancel_cleanup(self) -> None:
        started = threading.Event()
        cancel_seen = threading.Event()
        release_cleanup = threading.Event()
        result = {
            "final_message": "done",
            "turns_used": 1,
            "tokens": {"prompt": 1, "completion": 1, "total": 2},
            "tool_calls": 0,
            "duration_seconds": 0.01,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = Config("sk-test", root)
            manager = DeepSeekJobManager(lock_directory=root / "locks")

            def fake_run_agent(_task, _config, *, cancel_signal=None, **_kwargs):
                if cancel_signal is None:
                    return result
                started.set()
                if not cancel_signal.wait(2.0):
                    raise AssertionError("delegate cancellation was not forwarded")
                cancel_seen.set()
                if not release_cleanup.wait(2.0):
                    raise AssertionError("test did not release worker cleanup")
                raise AgentLoopCancelled("cancelled at safe point")

            async def scenario() -> None:
                delegated = asyncio.create_task(
                    server.delegate_to_deepseek("long task")
                )
                self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
                await asyncio.wait_for(asyncio.sleep(0), timeout=0.2)
                self.assertTrue(server.ping().startswith("pong from"))

                delegated.cancel()
                self.assertTrue(await asyncio.to_thread(cancel_seen.wait, 1.0))
                with self.assertRaises(JobBusy):
                    manager.run_sync("must remain busy", config)
                self.assertFalse(delegated.done())

                release_cleanup.set()
                with self.assertRaises(asyncio.CancelledError):
                    await delegated
                follow_up = await asyncio.to_thread(
                    manager.run_sync, "lease released", config
                )
                self.assertEqual(follow_up["final_message"], "done")

            with (
                patch.object(server, "job_manager", manager),
                patch.object(server.Config, "load", return_value=config),
                patch(
                    "deepseek_mcp.job_manager.run_agent",
                    side_effect=fake_run_agent,
                ),
            ):
                asyncio.run(scenario())

    def test_repeated_sync_cancellation_still_consumes_worker_exception(self) -> None:
        started = threading.Event()
        cancel_seen = threading.Event()
        release = threading.Event()
        completed = threading.Event()

        def run_sync(_task, _config, cancel_signal):
            started.set()
            if not cancel_signal.wait(2.0):
                raise AssertionError("cancellation not forwarded")
            cancel_seen.set()
            if not release.wait(2.0):
                raise AssertionError("worker not released")
            completed.set()
            raise MutationOutcomeError("transaction retained; DO NOT RETRY")

        async def scenario(config: Config) -> None:
            loop_errors = []
            asyncio.get_running_loop().set_exception_handler(
                lambda _loop, context: loop_errors.append(context)
            )
            delegated = asyncio.create_task(
                server._run_sync_cancellable("task", config)
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
            delegated.cancel()
            self.assertTrue(await asyncio.to_thread(cancel_seen.wait, 1.0))
            delegated.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await delegated
            release.set()
            self.assertTrue(await asyncio.to_thread(completed.wait, 1.0))
            await asyncio.sleep(0)
            self.assertEqual(loop_errors, [])

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config("sk-test", Path(tmpdir), allowed_tools=["Read"])
            with patch.object(server.job_manager, "run_sync", side_effect=run_sync):
                asyncio.run(scenario(config))

    def test_logging_setup_failure_blocks_root_and_stderr_fallback(self) -> None:
        marker = "logging-fallback-secret-marker"
        package_logger = server._PACKAGE_LOGGER
        original_handlers = package_logger.handlers[:]
        original_propagate = package_logger.propagate
        original_level = package_logger.level
        original_ready = server._LOG_READY
        root = logging.getLogger()
        root_output = io.StringIO()
        stderr_output = io.StringIO()
        root_handler = logging.StreamHandler(root_output)
        root.addHandler(root_handler)
        try:
            package_logger.handlers = []
            package_logger.propagate = True
            server._LOG_READY = False
            with patch.object(
                server,
                "_prepare_private_log_dir",
                side_effect=OSError("unavailable"),
            ):
                server._ensure_runtime_logging()
            with redirect_stderr(stderr_output):
                logging.getLogger("deepseek_mcp.agent_loop").error(marker)
            fail_closed = not package_logger.propagate
            has_null = any(
                isinstance(handler, logging.NullHandler)
                for handler in package_logger.handlers
            )
        finally:
            root.removeHandler(root_handler)
            package_logger.handlers = original_handlers
            package_logger.propagate = original_propagate
            package_logger.setLevel(original_level)
            server._LOG_READY = original_ready

        self.assertTrue(fail_closed)
        self.assertTrue(has_null)
        self.assertNotIn(marker, root_output.getvalue())
        self.assertNotIn(marker, stderr_output.getvalue())

    def test_windows_logging_disables_files_before_directory_fd_open(self) -> None:
        with (
            patch.object(server.os, "name", "nt"),
            patch.object(server.os, "open") as open_descriptor,
            self.assertRaisesRegex(OSError, "unavailable on Windows"),
        ):
            server._prepare_private_log_dir()

        open_descriptor.assert_not_called()

    def test_isolated_home_environment_covers_windows_profile_variables(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            environment = _isolated_home_environment(home)
            absolute_home = os.path.abspath(home)
            drive, path = os.path.splitdrive(absolute_home)

        self.assertEqual(environment["HOME"], absolute_home)
        self.assertEqual(environment["USERPROFILE"], absolute_home)
        self.assertEqual(environment["HOMEDRIVE"], drive)
        self.assertEqual(environment["HOMEPATH"], path or os.sep)

    def test_import_does_not_create_home_files(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            environment = _isolated_home_environment(home)
            environment["PYTHONPATH"] = str(ROOT / "src")

            subprocess.run(
                [sys.executable, "-c", "import deepseek_mcp.server"],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertFalse((Path(home) / ".deepseek-mcp").exists())

    def test_openai_debug_environment_cannot_emit_provider_payloads(self) -> None:
        marker = "private-provider-request-marker"
        program = f'''
import logging
import os
from deepseek_mcp import server
server._ensure_runtime_logging()
for name in ("openai", "httpx", "httpcore"):
    logging.getLogger(name).debug("{marker}")
    logging.getLogger(name).warning("{marker}")
assert "OPENAI_LOG" not in os.environ
'''
        with tempfile.TemporaryDirectory() as home:
            environment = _isolated_home_environment(home)
            environment["PYTHONPATH"] = str(ROOT / "src")
            environment["OPENAI_LOG"] = "debug"
            completed = subprocess.run(
                [sys.executable, "-c", program],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            server_log = Path(home) / ".deepseek-mcp" / "server.log"
            persisted = server_log.read_text(encoding="utf-8") if server_log.exists() else ""

        self.assertNotIn(marker, completed.stdout)
        self.assertNotIn(marker, completed.stderr)
        self.assertNotIn(marker, persisted)

    def test_lazy_logging_captures_package_logs_without_task_text(self) -> None:
        program = '''
import logging
from deepseek_mcp import server
server._ensure_runtime_logging()
logging.getLogger("deepseek_mcp.agent_loop").warning("package-child-log")
server._record_usage(len("private task text"), {
    "duration_seconds": 1.0,
    "turns_used": 1,
    "tool_calls": 2,
    "tokens": {"total": 3},
})
'''
        with tempfile.TemporaryDirectory() as home:
            environment = _isolated_home_environment(home)
            environment["PYTHONPATH"] = str(ROOT / "src")
            subprocess.run(
                [sys.executable, "-c", program],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

            log_dir = Path(home) / ".deepseek-mcp"
            if os.name == "nt":
                self.assertFalse(log_dir.exists())
                return
            server_log = log_dir / "server.log"
            usage_log = log_dir / "usage.log"
            self.assertIn("package-child-log", server_log.read_text(encoding="utf-8"))
            usage = usage_log.read_text(encoding="utf-8")
            self.assertIn("task_chars=17", usage)
            self.assertNotIn("private task text", usage)
            if os.name != "nt":
                self.assertEqual(server_log.stat().st_mode & 0o777, 0o600)
                self.assertEqual(usage_log.stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX no-follow log boundary")
    def test_logging_rejects_symlinked_runtime_directory(self) -> None:
        program = '''
from deepseek_mcp import server
server._ensure_runtime_logging()
server._record_usage(1, {
    "duration_seconds": 1.0,
    "turns_used": 1,
    "tool_calls": 0,
    "tokens": {"total": 1},
})
'''
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as target:
            (Path(home) / ".deepseek-mcp").symlink_to(target, target_is_directory=True)
            environment = _isolated_home_environment(home)
            environment["PYTHONPATH"] = str(ROOT / "src")
            subprocess.run(
                [sys.executable, "-c", program],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(list(Path(target).iterdir()), [])

    def test_tools_publish_honest_annotations(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
        expected = {
            "ping": (True, False, True, False),
            "delegate_to_deepseek": (False, True, False, True),
            "delegate_to_deepseek_readonly": (True, False, False, True),
            "start_deepseek": (False, True, False, True),
            "start_deepseek_readonly": (True, False, False, True),
            "get_deepseek_status": (True, False, True, False),
            "send_deepseek_message": (False, True, False, True),
            "cancel_deepseek": (False, True, True, False),
            "get_deepseek_result": (False, False, True, False),
            "get_deepseek_recovery": (False, False, True, False),
            "acknowledge_deepseek_mutations": (False, True, True, False),
        }

        self.assertEqual(set(tools), set(expected))
        for name, hints in expected.items():
            annotations = tools[name].annotations
            self.assertIsNotNone(annotations, name)
            assert annotations is not None
            actual = (
                annotations.readOnlyHint,
                annotations.destructiveHint,
                annotations.idempotentHint,
                annotations.openWorldHint,
            )
            self.assertEqual(actual, hints, name)

    def test_legacy_public_tool_schemas_remain_exactly_compatible(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

        self.assertEqual(
            tools["ping"].inputSchema,
            {
                "properties": {},
                "title": "pingArguments",
                "type": "object",
            },
        )
        self.assertEqual(
            tools["delegate_to_deepseek"].inputSchema,
            {
                "properties": {
                    "context": {
                        "default": "",
                        "title": "Context",
                        "type": "string",
                    },
                    "task": {"title": "Task", "type": "string"},
                },
                "required": ["task"],
                "title": "delegate_to_deepseekArguments",
                "type": "object",
            },
        )


if __name__ == "__main__":
    unittest.main()
