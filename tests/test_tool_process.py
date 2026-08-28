from __future__ import annotations

import json
import hashlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp import transaction_journal
from deepseek_mcp.config import Config
from deepseek_mcp.execution_lock import (
    WorkspaceLockBusy,
    acquire_workspace_lease,
    workspace_identity,
)
from deepseek_mcp.file_identity import ToolInputError
from deepseek_mcp.provider_retry import (
    AgentLoopCancelled,
    AgentLoopError,
    MutationOutcomeError,
)
from deepseek_mcp.job_manager import DeepSeekJobManager, JobError
from deepseek_mcp.resource_budget import MutationBudget
from deepseek_mcp.tool_cleanup import cleanup_tool_artifacts
from deepseek_mcp.tool_process import _encoded_request, _environment, execute_in_subprocess
from deepseek_mcp.transaction_recovery import (
    TransactionRecoveryError,
    acknowledge_with_lease,
    query_with_lease,
)

ROOT = Path(__file__).resolve().parents[1]


def _process_exists(process_id: int) -> bool:
    completed = subprocess.run(
        ["ps", "-p", str(process_id), "-o", "stat="],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def _wait_for_file(path: Path, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not path.exists():
        raise AssertionError(f"timed out waiting for {path.name}")


def _fake_runtime(path: Path, marker: Path) -> None:
    path.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, signal, sys, time\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "args = sys.argv[1:]\n"
        "if args[:1] == ['ps'] or args[:1] == ['info']:\n"
        "    raise SystemExit(0)\n"
        "if args[:2] == ['container', 'inspect']:\n"
        "    raise SystemExit(1)\n"
        "if args[:1] == ['rm']:\n"
        "    raise SystemExit(0)\n"
        "if args[:1] == ['run']:\n"
        "    marker.write_text(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


class ToolProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        profile = os.path.abspath(home.name)
        drive, path = os.path.splitdrive(profile)
        environment = {
            "HOME": profile,
            "USERPROFILE": profile,
            "HOMEDRIVE": drive,
            "HOMEPATH": path or os.sep,
        }
        environment_patch = patch.dict(os.environ, environment)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)

    def test_windows_profile_environment_reaches_isolated_tool_child(self) -> None:
        profile = r"C:\Users\new-user"
        with patch.dict(
            os.environ,
            {
                "USERPROFILE": profile,
                "HOMEDRIVE": "C:",
                "HOMEPATH": r"\Users\new-user",
            },
            clear=True,
        ):
            environment = _environment()

        self.assertEqual(environment["USERPROFILE"], profile)
        self.assertEqual(environment["HOMEDRIVE"], "C:")
        self.assertEqual(environment["HOMEPATH"], r"\Users\new-user")

    def _config(self, workspace: Path) -> Config:
        return Config(
            "credential-must-not-cross-tool-boundary",
            workspace,
            allowed_tools=["Read", "Write"],
        )

    def test_real_write_runs_in_child_and_returns_mutation_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            budget = MutationBudget(limit=100)
            result = execute_in_subprocess(
                self._config(workspace),
                "Write",
                {"path": "probe.txt", "content": "hello"},
                budget,
                10,
                None,
                None,
                time.monotonic() + 5,
            )

            self.assertTrue(result.startswith("OK: wrote"))
            self.assertEqual((workspace / "probe.txt").read_text(), "hello")
            self.assertEqual(budget.used, 5)

    def test_real_child_rejects_lone_surrogate_as_tool_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            result = execute_in_subprocess(
                self._config(workspace),
                "Write",
                {"path": "surrogate.txt", "content": "\ud800"},
                MutationBudget(),
                10,
                None,
                None,
                time.monotonic() + 5,
            )

            self.assertIn("not valid UTF-8", result)
            self.assertFalse((workspace / "surrogate.txt").exists())

    def test_real_child_rejects_lone_surrogate_grep_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            config = Config("credential", workspace, allowed_tools=["Grep"])
            result = execute_in_subprocess(
                config,
                "Grep",
                {"pattern": "\ud800"},
                MutationBudget(),
                10,
                None,
                None,
                time.monotonic() + 5,
            )

            self.assertEqual(result, "ERROR: pattern is not valid Unicode text")

    def test_child_rejects_workspace_path_reused_for_new_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            config = self._config(workspace)
            workspace.rename(root / "moved")
            workspace.mkdir()

            with self.assertRaisesRegex(AgentLoopError, "workspace identity changed"):
                execute_in_subprocess(
                    config,
                    "Write",
                    {"path": "must-not-write.txt", "content": "blocked"},
                    MutationBudget(),
                    10,
                    None,
                    None,
                    time.monotonic() + 5,
                )

            self.assertFalse((workspace / "must-not-write.txt").exists())

    def test_cleanup_never_removes_artifacts_from_reused_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            config = self._config(workspace)
            workspace.rename(workspace.with_name("original"))
            workspace.mkdir()
            transaction_id = "a" * 32
            artifact = workspace / f".deepseek-mcp-{transaction_id}.tmp"
            artifact.write_text("new-root-data", encoding="utf-8")

            with self.assertRaisesRegex(ToolInputError, "identity"):
                cleanup_tool_artifacts(
                    config, "Write", {"path": "target.txt"}, transaction_id
                )

            self.assertEqual(artifact.read_text(encoding="utf-8"), "new-root-data")

    def test_unicode_and_control_output_round_trips_from_real_child(self) -> None:
        cases = {
            "cjk.txt": "汉" * 22_000,
            "emoji.txt": "😀" * 11_000,
            "control.txt": "\x01" * 22_000,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            config = self._config(workspace)
            for label, content in cases.items():
                with self.subTest(label=label):
                    (workspace / label).write_text(content, encoding="utf-8")
                    result = execute_in_subprocess(
                        config,
                        "Read",
                        {"path": label},
                        MutationBudget(),
                        10,
                        None,
                        None,
                        time.monotonic() + 10,
                    )
                    self.assertEqual(result, content)

    def test_large_unicode_and_escape_writes_round_trip_to_real_child(self) -> None:
        cases = {
            "cjk.txt": "汉" * 1_400_000,
            "emoji.txt": "😀" * 1_100_000,
            "escaped.txt": "\x01" * 1_400_000,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            config = self._config(workspace)
            for label, content in cases.items():
                with self.subTest(label=label):
                    result = execute_in_subprocess(
                        config,
                        "Write",
                        {"path": label, "content": content},
                        MutationBudget(),
                        10,
                        None,
                        None,
                        time.monotonic() + 20,
                    )
                    self.assertTrue(result.startswith("OK: wrote"), result)
                    self.assertEqual(
                        (workspace / label).read_text(encoding="utf-8"), content
                    )

    def test_committed_write_stops_for_review_when_cleanup_fails(self) -> None:
        records = []
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            with (
                patch(
                    "deepseek_mcp.tool_process.cleanup_tool_artifacts",
                    side_effect=OSError("cleanup failed"),
                ),
                self.assertRaises(MutationOutcomeError) as raised,
            ):
                execute_in_subprocess(
                    self._config(workspace),
                    "Write",
                    {"path": "probe.txt", "content": "hello"},
                    MutationBudget(limit=100),
                    10,
                    None,
                    None,
                    time.monotonic() + 5,
                    records.append,
                )

            self.assertIn("DO NOT RETRY", str(raised.exception))
            self.assertEqual(records[0].status, "committed")
            self.assertIn("artifact cleanup failed", records[0].warning or "")
            self.assertEqual((workspace / "probe.txt").read_text(), "hello")

    def test_error_result_after_intent_stops_for_committed_and_uncertain_states(self) -> None:
        cases = (("value", "committed"), ("external", "uncertain"))
        for content, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmpdir:
                workspace = Path(tmpdir).resolve()
                target = workspace / "target.txt"
                target.write_text(content, encoding="utf-8")
                digest = hashlib.sha256(b"value").hexdigest()
                script = (
                    "import json; print(json.dumps({\"kind\":\"mutation_ready\","
                    f"\"sha256\":\"{digest}\"}})); "
                    "print(json.dumps({\"kind\":\"ok\",\"result\":"
                    "\"ERROR: recovery failed\",\"mutation_used\":0}))"
                )

                def start_error(_timeout: float, _lease: int | None):
                    return subprocess.Popen(
                        [sys.executable, "-c", script],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        close_fds=True,
                        start_new_session=True,
                    )

                records = []
                with (
                    patch(
                        "deepseek_mcp.tool_process._start_tool",
                        side_effect=start_error,
                    ),
                    self.assertRaises(MutationOutcomeError),
                ):
                    execute_in_subprocess(
                        self._config(workspace),
                        "Write",
                        {"path": "target.txt", "content": "value"},
                        MutationBudget(),
                        10,
                        None,
                        None,
                        time.monotonic() + 5,
                        records.append,
                    )

                self.assertEqual(records[0].status, expected)

    def test_provider_credential_is_not_serialized_to_tool_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(Path(tmpdir).resolve())
            request = _encoded_request(
                config,
                "Read",
                {"path": "README.md"},
                MutationBudget(),
                10,
                "a" * 32,
            )

        self.assertNotIn(b"credential-must-not-cross-tool-boundary", request)
        payload = json.loads(request)
        self.assertIsNone(payload["config"].get("api_key"))
        self.assertEqual(payload["transaction_id"], "a" * 32)

    def test_blocked_tool_child_is_killed_and_reaped_at_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(Path(tmpdir).resolve())
            processes: list[subprocess.Popen[bytes]] = []

            def start_sleeper(_timeout: float, _lease: int | None):
                process = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
                processes.append(process)
                return process

            started = time.monotonic()
            with (
                patch(
                    "deepseek_mcp.tool_process._start_tool",
                    side_effect=start_sleeper,
                ),
                self.assertRaisesRegex(AgentLoopError, "time budget"),
            ):
                execute_in_subprocess(
                    config,
                    "Read",
                    {"path": "blocked"},
                    MutationBudget(),
                    10,
                    None,
                    None,
                    time.monotonic() + 0.2,
                )

            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())

    def test_cancellation_kills_and_reaps_an_in_flight_tool_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(Path(tmpdir).resolve())
            cancel = threading.Event()
            processes: list[subprocess.Popen[bytes]] = []

            def start_sleeper(_timeout: float, _lease: int | None):
                process = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
                processes.append(process)
                return process

            timer = threading.Timer(0.1, cancel.set)
            timer.start()
            try:
                with (
                    patch(
                        "deepseek_mcp.tool_process._start_tool",
                        side_effect=start_sleeper,
                    ),
                    self.assertRaisesRegex(AgentLoopCancelled, "cancelled"),
                ):
                    execute_in_subprocess(
                        config,
                        "Read",
                        {"path": "blocked"},
                        MutationBudget(),
                        10,
                        None,
                        cancel,
                        time.monotonic() + 5,
                    )
            finally:
                timer.cancel()

            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())

    def test_thread_start_failure_still_kills_and_reaps_tool_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(Path(tmpdir).resolve())
            processes: list[subprocess.Popen[bytes]] = []

            def start_sleeper(_timeout: float, _lease: int | None):
                process = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
                processes.append(process)
                return process

            with (
                patch("deepseek_mcp.tool_process._start_tool", side_effect=start_sleeper),
                patch("deepseek_mcp.tool_process.threading.Thread.start", side_effect=RuntimeError),
                self.assertRaisesRegex(AgentLoopError, "communication could not start"),
            ):
                execute_in_subprocess(
                    config,
                    "Read",
                    {"path": "blocked"},
                    MutationBudget(),
                    10,
                    None,
                    None,
                    time.monotonic() + 5,
                )

            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())
            self.assertTrue(processes[0].stdin.closed)
            self.assertTrue(processes[0].stdout.closed)

    @unittest.skipIf(os.name == "nt", "POSIX temp cleanup uses directory descriptors")
    def test_deadline_preserves_ambiguous_posix_exchange_temp(self) -> None:
        transaction_id = "b" * 32
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            config = self._config(workspace)
            temporary = workspace / f".deepseek-mcp-{transaction_id}.tmp"
            target = workspace / "target.txt"

            def start_sleeper(_timeout: float, _lease: int | None):
                temporary.write_text("displaced-original", encoding="utf-8")
                target.write_text("replacement", encoding="utf-8")
                digest = hashlib.sha256(b"replacement").hexdigest()
                return subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import sys,time; "
                        f"sys.stdout.write('{{\"kind\":\"mutation_ready\","
                        f"\"sha256\":\"{digest}\"}}\\n'); "
                        "sys.stdout.flush(); time.sleep(30)",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )

            with (
                patch("deepseek_mcp.tool_process.uuid.uuid4") as transaction,
                patch("deepseek_mcp.tool_process._start_tool", side_effect=start_sleeper),
                self.assertRaisesRegex(AgentLoopError, "DO NOT RETRY"),
            ):
                transaction.return_value.hex = transaction_id
                execute_in_subprocess(
                    config,
                    "Write",
                    {"path": "target.txt", "content": "value"},
                    MutationBudget(),
                    10,
                    None,
                    None,
                    time.monotonic() + 0.2,
                )
            self.assertEqual(temporary.read_text(), "displaced-original")
            self.assertEqual(target.read_text(), "replacement")

    def test_changed_target_and_cleanup_failure_remain_uncertain_with_warning(self) -> None:
        transaction_id = "d" * 32
        records = []
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            config = self._config(workspace)
            target = workspace / "target.txt"
            target.write_text("external-change", encoding="utf-8")
            digest = hashlib.sha256(b"agent-replacement").hexdigest()

            def start_sleeper(_timeout: float, _lease: int | None):
                script = (
                    "import sys,time; "
                    f"sys.stdout.write('{{\"kind\":\"mutation_ready\","
                    f"\"sha256\":\"{digest}\"}}\\n'); "
                    "sys.stdout.write('{\"kind\":\"mutation_warning\","
                    "\"detail\":\"recover from retained-copy\"}\\n'); "
                    "sys.stdout.flush(); time.sleep(30)"
                )
                return subprocess.Popen(
                    [sys.executable, "-c", script],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )

            with (
                patch("deepseek_mcp.tool_process.uuid.uuid4") as transaction,
                patch("deepseek_mcp.tool_process._start_tool", side_effect=start_sleeper),
                patch(
                    "deepseek_mcp.tool_process.cleanup_tool_artifacts",
                    side_effect=ToolInputError("unsafe cleanup"),
                ),
                self.assertRaises(MutationOutcomeError) as raised,
            ):
                transaction.return_value.hex = transaction_id
                execute_in_subprocess(
                    config, "Write", {"path": "target.txt", "content": "value"},
                    MutationBudget(), 10, None, None, time.monotonic() + 0.2,
                    records.append,
                )

        self.assertIn("outcome uncertain", str(raised.exception))
        self.assertIn("recover from retained-copy", str(raised.exception))
        self.assertIn("cleanup failed", str(raised.exception))
        self.assertEqual(records[0].status, "uncertain")

    @unittest.skipIf(os.name != "posix", "container snapshots require POSIX")
    def test_deadline_cleans_transaction_scoped_bash_snapshot(self) -> None:
        transaction_id = "c" * 32
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            workspace, home = root / "workspace", root / "home"
            workspace.mkdir()
            home.mkdir()
            config = self._config(workspace)
            label = hashlib.sha256(workspace_identity(workspace)).hexdigest()[:16]
            staging = home / ".deepseek-mcp" / "snapshots"
            staging.mkdir(parents=True, mode=0o700)
            snapshot = staging / f"deepseek-mcp-{label}-{transaction_id}"

            def start_sleeper(_timeout: float, _lease: int | None):
                snapshot.mkdir(mode=0o700)
                (snapshot / "private.txt").write_text("partial", encoding="utf-8")
                return subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )

            with (
                patch.dict(os.environ, {"HOME": str(home)}),
                patch("deepseek_mcp.tool_process.uuid.uuid4") as transaction,
                patch("deepseek_mcp.tool_process._start_tool", side_effect=start_sleeper),
                self.assertRaisesRegex(AgentLoopError, "time budget"),
            ):
                transaction.return_value.hex = transaction_id
                execute_in_subprocess(
                    config,
                    "Bash",
                    {"command": "sleep 30"},
                    MutationBudget(),
                    10,
                    None,
                    None,
                    time.monotonic() + 0.2,
                )

            self.assertFalse(snapshot.exists())

    def test_cleanup_validation_failure_is_reported_without_name_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(Path(tmpdir).resolve())
            with (
                patch(
                    "deepseek_mcp.tool_process.cleanup_tool_artifacts",
                    side_effect=ToolInputError("unsafe artifact"),
                ),
                self.assertRaisesRegex(AgentLoopError, "artifact cleanup failed"),
            ):
                execute_in_subprocess(
                    config,
                    "Read",
                    {"path": "missing.txt"},
                    MutationBudget(),
                    10,
                    None,
                    None,
                    time.monotonic() + 5,
                )

    @unittest.skipIf(os.name != "posix", "parent liveness pipe is POSIX-specific")
    def test_parent_death_stops_active_tool_and_releases_workspace_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            workspace, home, binaries = (
                root / "workspace",
                root / "home",
                root / "bin",
            )
            lock_directory = home / ".deepseek-mcp" / "locks"
            marker = root / "runtime.pid"
            workspace.mkdir()
            home.mkdir()
            binaries.mkdir()
            (workspace / "source.txt").write_text("safe", encoding="utf-8")
            _fake_runtime(binaries / "docker", marker)
            environment = os.environ.copy()
            environment.update({
                "HOME": str(home),
                "PATH": f"{binaries}{os.pathsep}{environment.get('PATH', os.defpath)}",
                "PYTHONPATH": str(ROOT / "src"),
            })
            parent = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "tests" / "tool_parent_helper.py"),
                    str(workspace),
                    str(lock_directory),
                ],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            assert parent.stdout is not None
            child_pid = int(parent.stdout.readline().strip())
            try:
                _wait_for_file(marker)
            except AssertionError:
                parent.terminate()
                _stdout, stderr = parent.communicate(timeout=3)
                self.fail(f"runtime did not start: {stderr[-1000:]}")
            runtime_pid = int(marker.read_text(encoding="utf-8"))
            try:
                parent.kill()
                parent.wait(timeout=3)
                parent.communicate(timeout=1)
                deadline = time.monotonic() + 10
                while (
                    (_process_exists(child_pid) or _process_exists(runtime_pid))
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                self.assertFalse(_process_exists(child_pid))
                self.assertFalse(_process_exists(runtime_pid))

                lease = None
                while lease is None and time.monotonic() < deadline:
                    try:
                        lease = acquire_workspace_lease(workspace, lock_directory)
                    except WorkspaceLockBusy:
                        time.sleep(0.05)
                self.assertIsNotNone(lease)
                assert lease is not None
                lease.release()
                snapshots = home / ".deepseek-mcp" / "snapshots"
                self.assertFalse(snapshots.exists() and any(snapshots.iterdir()))
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=3)
                parent.communicate(timeout=1)
                for process_id in (child_pid, runtime_pid):
                    if _process_exists(process_id):
                        os.kill(process_id, signal.SIGKILL)

    @unittest.skipIf(os.name != "posix", "parent SIGKILL recovery is POSIX-specific")
    def test_parent_sigkill_leaves_queryable_intent_until_exact_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            workspace, home = root / "workspace", root / "home"
            workspace.mkdir()
            home.mkdir()
            lock_directory = home / ".deepseek-mcp" / "locks"
            journal_root = home / ".deepseek-mcp" / "transactions"
            environment = os.environ.copy()
            environment.update({"HOME": str(home), "PYTHONPATH": str(ROOT / "src")})
            parent = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "tests" / "tool_parent_helper.py"),
                    str(workspace),
                    str(lock_directory),
                    "write",
                ],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            assert parent.stdout is not None
            child_pid = int(parent.stdout.readline().strip())

            def reap_fixture() -> None:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=3)
                if _process_exists(child_pid):
                    os.kill(child_pid, signal.SIGKILL)

            self.addCleanup(reap_fixture)
            self.assertEqual(parent.stdout.readline().strip(), "COMMITTED")
            records = list(journal_root.glob("*/*.json"))
            self.assertEqual(len(records), 1)
            parent.kill()
            parent.wait(timeout=3)
            parent.communicate(timeout=1)

            config = Config("", workspace, allowed_tools=[])
            pending = None
            deadline = time.monotonic() + 15
            with patch.object(
                transaction_journal, "JOURNAL_DIRECTORY", journal_root
            ):
                while pending is None and time.monotonic() < deadline:
                    try:
                        pending = query_with_lease(config, lock_directory)
                    except TransactionRecoveryError as error:
                        if "busy" not in str(error):
                            raise
                        time.sleep(0.02)
                self.assertIsNotNone(pending)
                assert pending is not None
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["status"], "committed")
                target = workspace / "crash.txt"
                self.assertEqual(target.stat().st_size, 1_000_000)
                self.assertEqual(
                    hashlib.sha256(target.read_bytes()).hexdigest(),
                    pending[0]["sha256"],
                )

                manager = DeepSeekJobManager(lock_directory=lock_directory)
                with patch("deepseek_mcp.job_manager.run_agent") as run_agent:
                    with self.assertRaisesRegex(JobError, "unacknowledged"):
                        manager.run_sync("must remain blocked", config)
                run_agent.assert_not_called()

                transaction_id = str(pending[0]["transaction_id"])
                removed, remaining = acknowledge_with_lease(
                    config, [transaction_id], lock_directory
                )
                self.assertEqual(removed, [transaction_id])
                self.assertEqual(remaining, [])
                completed = {
                    "final_message": "safe",
                    "turns_used": 1,
                    "tokens": {"prompt": 1, "completion": 1, "total": 2},
                    "tool_calls": 0,
                    "duration_seconds": 0.01,
                    "mutations": [],
                }
                with patch(
                    "deepseek_mcp.job_manager.run_agent", return_value=completed
                ):
                    result = manager.run_sync("allowed after ack", config)
                self.assertEqual(result["final_message"], "safe")

            if _process_exists(child_pid):
                os.kill(child_pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
