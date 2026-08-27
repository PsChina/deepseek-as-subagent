from __future__ import annotations

import subprocess
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from deepseek_mcp.container_sandbox import _runtime_environment
from deepseek_mcp.container_watchdog import (
    CLEANUP_NOW,
    DONE,
    WatchdogError,
    WatchdogHandle,
    _control_action,
    _cleanup_until_confirmed,
    _open_control_pipes,
    _remove_confirmed,
    _run,
    stop_watchdog,
    start_watchdog,
)
from deepseek_mcp.hard_deadline import HardDeadline


class _Process:
    def __init__(self, returncode: int = 0):
        self.pid = 321
        self.returncode = None
        self.result = returncode

    def wait(self, timeout=None):
        self.returncode = self.result
        return self.result

    def kill(self):
        return None


def _managed_snapshot(root: Path) -> Path:
    base = root / ".deepseek-mcp"
    staging = base / "snapshots"
    staging.mkdir(parents=True, mode=0o700)
    base.chmod(0o700)
    staging.chmod(0o700)
    snapshot = staging / f"deepseek-mcp-{'a' * 16}-{'b' * 32}"
    snapshot.mkdir(mode=0o700)
    (snapshot / "input.txt").write_text("safe", encoding="utf-8")
    return snapshot


def _wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not path.exists():
        raise AssertionError(f"timed out waiting for {path.name}")


class ContainerWatchdogTests(unittest.TestCase):
    def test_second_pipe_failure_closes_first_pipe(self) -> None:
        with (
            patch(
                "deepseek_mcp.container_watchdog.os.pipe",
                side_effect=[(10, 11), OSError("pipe failed")],
            ),
            patch("deepseek_mcp.container_watchdog._close_fd") as close,
            self.assertRaisesRegex(WatchdogError, "control pipe"),
        ):
            _open_control_pipes()
        self.assertEqual(close.call_args_list, [call(10), call(11)])

    def test_runtime_cleanup_uses_argv_without_inheriting_lease(self) -> None:
        process = _Process()
        with patch(
            "deepseek_mcp.container_watchdog.subprocess.Popen",
            return_value=process,
        ) as popen:
            result = _run("/usr/bin/docker", ["rm", "-f", "managed"])

        self.assertEqual(result, 0)
        call = popen.call_args
        self.assertEqual(call.args[0], ["/usr/bin/docker", "rm", "-f", "managed"])
        self.assertFalse(call.kwargs["shell"])
        self.assertNotIn("pass_fds", call.kwargs)
        self.assertIs(call.kwargs["stdout"], subprocess.DEVNULL)

    @unittest.skipIf(os.name == "nt", "Bash watchdog is POSIX-only")
    def test_workspace_shadow_package_is_never_imported_by_watchdog(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            package = root / "deepseek_mcp"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            marker = root / "host-code-executed"
            (package / "container_watchdog.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed')\n",
                encoding="utf-8",
            )
            runtime = root / "docker"
            runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            runtime.chmod(0o700)
            snapshot = _managed_snapshot(root)
            env = _runtime_environment(str(runtime))
            env["PYTHONPATH"] = str(root)
            previous = Path.cwd()
            try:
                os.chdir(root)
                handle = start_watchdog(
                    str(runtime), "deepseek-mcp-shadow", env, 1, (), snapshot
                )
                self.assertTrue(stop_watchdog(handle, cleanup_now=False))
            finally:
                os.chdir(previous)
            self.assertFalse(marker.exists())
            self.assertFalse(snapshot.exists())

    def test_remove_is_confirmed_when_container_is_already_absent(self) -> None:
        with (
            patch("deepseek_mcp.container_watchdog._run", side_effect=[1, 1]) as run,
            patch(
                "deepseek_mcp.container_watchdog.container_name_absent",
                return_value=True,
            ) as absent,
        ):
            self.assertTrue(_remove_confirmed("docker", "managed"))
        self.assertEqual(run.call_args_list[-1].args[1], ["container", "inspect", "managed"])
        absent.assert_called_once_with("docker", "managed", dict(os.environ))

    def test_existing_container_after_remove_failure_fails_closed(self) -> None:
        with patch(
            "deepseek_mcp.container_watchdog._run", side_effect=[1, 0]
        ):
            self.assertFalse(_remove_confirmed("docker", "managed"))

    def test_cleanup_retries_until_container_and_snapshot_are_removed(self) -> None:
        snapshot = Path("/managed/snapshot")
        with (
            patch(
                "deepseek_mcp.container_watchdog._remove_confirmed",
                side_effect=[False, False, True],
            ) as remove,
            patch(
                "deepseek_mcp.container_watchdog._snapshot_removed",
                return_value=True,
            ) as remove_snapshot,
            patch("deepseek_mcp.container_watchdog.time.sleep") as sleep,
        ):
            _cleanup_until_confirmed(
                "docker", "managed", snapshot, snapshot.parent, True
            )
        self.assertEqual(remove.call_count, 3)
        remove_snapshot.assert_called_once_with(snapshot, snapshot.parent)
        self.assertEqual(sleep.call_args_list, [call(1.0), call(2.0)])

    def test_done_signal_is_event_driven_primary_path(self) -> None:
        with (
            patch("deepseek_mcp.container_watchdog.select.select", return_value=([5], [], [])),
            patch("deepseek_mcp.container_watchdog.os.read", return_value=DONE),
            patch("deepseek_mcp.container_watchdog.time.sleep") as sleep,
        ):
            action = _control_action(5, HardDeadline.after(100.0))
        self.assertEqual(action, DONE)
        sleep.assert_not_called()

    def test_parent_eof_requests_immediate_cleanup(self) -> None:
        with (
            patch("deepseek_mcp.container_watchdog.select.select", return_value=([5], [], [])),
            patch("deepseek_mcp.container_watchdog.os.read", return_value=b""),
            patch("deepseek_mcp.container_watchdog.time.sleep") as sleep,
        ):
            action = _control_action(5, HardDeadline.after(10.0))
        self.assertEqual(action, CLEANUP_NOW)
        sleep.assert_not_called()

    def test_normal_completion_signals_and_reaps_watchdog(self) -> None:
        process = _Process()
        handle = WatchdogHandle(process, 42)
        with (
            patch("deepseek_mcp.container_watchdog.os.write") as write,
            patch("deepseek_mcp.container_watchdog._close_fd") as close,
        ):
            self.assertTrue(stop_watchdog(handle, cleanup_now=False))
        write.assert_called_once_with(42, DONE)
        close.assert_called_once_with(42)
        self.assertEqual(process.returncode, 0)

    def test_emergency_completion_requests_immediate_cleanup(self) -> None:
        process = _Process()
        handle = WatchdogHandle(process, 42)
        with (
            patch("deepseek_mcp.container_watchdog.os.write") as write,
            patch("deepseek_mcp.container_watchdog._close_fd"),
        ):
            self.assertTrue(stop_watchdog(handle, cleanup_now=True))
        write.assert_called_once_with(42, CLEANUP_NOW)

    def test_stop_timeout_leaves_cleanup_watchdog_running(self) -> None:
        process = _Process()
        process.wait = Mock(
            side_effect=subprocess.TimeoutExpired(cmd="watchdog", timeout=50)
        )
        handle = WatchdogHandle(process, 42)
        with (
            patch("deepseek_mcp.container_watchdog.os.write"),
            patch("deepseek_mcp.container_watchdog._close_fd"),
            patch("deepseek_mcp.container_watchdog._kill_process") as kill,
        ):
            self.assertFalse(stop_watchdog(handle, cleanup_now=True))
        kill.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX descriptor inheritance contract")
    def test_parent_loss_keeps_lease_through_real_cleanup_retries(self) -> None:
        import fcntl

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            marker = root / "removed"
            attempted = root / "attempted"
            allow = root / "allow-removal"
            runtime = root / "docker"
            runtime.write_text(
                f"#!{sys.executable}\n"
                "import os, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                "if args[:2] == ['rm', '-f']:\n"
                "    pathlib.Path(os.environ['WATCHDOG_ATTEMPT']).write_text('attempted')\n"
                "    if pathlib.Path(os.environ['WATCHDOG_ALLOW']).exists():\n"
                "        pathlib.Path(os.environ['WATCHDOG_MARKER']).write_text(' '.join(args))\n"
                "        raise SystemExit(0)\n"
                "    raise SystemExit(1)\n"
                "if args[:2] == ['container', 'inspect']:\n"
                "    raise SystemExit(1)\n"
                "if args[:2] == ['ps', '-a']:\n"
                "    print('deepseek-mcp-crash')\n"
                "    raise SystemExit(0)\n"
                "if args == ['info']:\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            runtime.chmod(0o700)
            snapshot = _managed_snapshot(root)
            lease_path = root / "lease"
            lease_fd = os.open(lease_path, os.O_RDWR | os.O_CREAT, 0o600)
            contender = os.open(lease_path, os.O_RDWR)
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.dict(os.environ, {}, clear=True):
                env = _runtime_environment(str(runtime))
            env.update({
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "WATCHDOG_MARKER": str(marker),
                "WATCHDOG_ATTEMPT": str(attempted),
                "WATCHDOG_ALLOW": str(allow),
            })
            self.assertEqual(env["HOME"], "/nonexistent")
            with patch("deepseek_mcp.container_watchdog.GRACE_SECONDS", 0.1):
                handle = start_watchdog(
                    str(runtime), "deepseek-mcp-crash", env, 0,
                    (lease_fd,), snapshot,
                )
            os.close(handle.control_fd)
            os.close(lease_fd)
            try:
                _wait_for_file(attempted)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIsNone(handle.process.poll())
                self.assertTrue(snapshot.exists())
                allow.write_text("allow", encoding="utf-8")
                self.assertEqual(handle.process.wait(timeout=5), 0)
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(marker.read_text(encoding="utf-8"), "rm -f deepseek-mcp-crash")
                self.assertFalse(snapshot.exists())
            finally:
                allow.touch(exist_ok=True)
                if handle.process.poll() is None:
                    handle.process.kill()
                    handle.process.wait(timeout=5)
                os.close(contender)


if __name__ == "__main__":
    unittest.main()
