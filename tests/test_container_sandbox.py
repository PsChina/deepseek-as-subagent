from __future__ import annotations

import io
import hashlib
import os
import signal
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from deepseek_mcp.config import Config
from deepseek_mcp.container_process import container_name_absent, run_control
from deepseek_mcp.container_sandbox import (
    LABEL_MANAGED,
    LABEL_OWNER,
    LABEL_WORKSPACE,
    MAX_STREAM_BYTES,
    ContainerSandboxError,
    _join_readers,
    _remove_container_confirmed,
    _runtime_environment,
    _workspace_label,
    build_container_argv,
    cleanup_stale_containers,
    run_in_container,
)
from deepseek_mcp.container_watchdog import WatchdogError, start_watchdog
from deepseek_mcp.execution_lock import WorkspaceExecutionLease

PINNED_IMAGE = "example.invalid/deepseek-shell@sha256:" + ("c" * 64)


def _config(workspace: Path) -> Config:
    return Config(
        "test-value",
        workspace,
        allowed_tools=["Read", "Bash"],
        bash_backend="container",
        bash_runtime="docker",
        bash_image=PINNED_IMAGE,
    )


class _FakeProcess:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", waits=None):
        self.pid = 4321
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None
        self._waits = list(waits or [0])
        self.killed = False

    def wait(self, timeout=None):
        outcome = self._waits.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        self.returncode = outcome
        return outcome

    def kill(self) -> None:
        self.killed = True


class _ReaderDouble:
    def __init__(self, start_error: BaseException | None = None):
        self.start_error = start_error
        self.started = False
        self.joined = 0

    def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    def join(self, timeout=None) -> None:
        self.joined += 1

    def is_alive(self) -> bool:
        return False


class _StuckReader:
    def join(self, timeout=None) -> None:
        return None

    def is_alive(self) -> bool:
        return True


def _test_lease() -> tuple[WorkspaceExecutionLease, int]:
    read_fd, write_fd = os.pipe()
    return WorkspaceExecutionLease(Path("test.lock"), read_fd), write_fd


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _run_patches(process: _FakeProcess) -> ExitStack:
    stack = ExitStack()
    lease, write_fd = _test_lease()
    stack.callback(_close_fd, write_fd)
    stack.callback(lease.release)
    stack.enter_context(patch("deepseek_mcp.config.sys.platform", "linux"))
    stack.enter_context(
        patch("deepseek_mcp.config.shutil.which", return_value="/usr/bin/docker")
    )
    stack.enter_context(
        patch(
            "deepseek_mcp.config.validate_trusted_executable",
            return_value=Path("/usr/bin/docker"),
        )
    )
    stack.enter_context(
        patch(
            "deepseek_mcp.container_sandbox.uuid.uuid4",
            return_value=SimpleNamespace(hex="fixed"),
        )
    )
    stack.enter_context(
        patch("deepseek_mcp.container_sandbox.start_watchdog", return_value=Mock())
    )
    stack.enter_context(
        patch("deepseek_mcp.container_sandbox.stop_watchdog", return_value=True)
    )
    stack.enter_context(
        patch("deepseek_mcp.container_sandbox.acquire_workspace_lease", return_value=lease)
    )
    stack.enter_context(patch("deepseek_mcp.container_sandbox.cleanup_stale_containers"))
    stack.enter_context(patch("deepseek_mcp.container_sandbox.cleanup_stale_snapshots"))
    stack.enter_context(
        patch(
            "deepseek_mcp.container_sandbox.create_workspace_snapshot",
            side_effect=lambda workspace, _label: workspace,
        )
    )
    stack.enter_context(patch("deepseek_mcp.container_sandbox.cleanup_workspace_snapshot"))
    stack.enter_context(
        patch(
            "deepseek_mcp.container_sandbox._remove_container_confirmed",
            return_value=True,
        )
    )
    stack.enter_context(
        patch("deepseek_mcp.container_sandbox.subprocess.Popen", return_value=process)
    )
    return stack


@unittest.skipIf(os.name == "nt", "container execution intentionally fails closed on Windows")
class ContainerSandboxTests(unittest.TestCase):
    def test_workspace_label_is_alias_independent_for_same_inode(self) -> None:
        metadata = SimpleNamespace(st_dev=42, st_ino=9001)
        with patch.object(Path, "stat", return_value=metadata):
            upper = _workspace_label(Path("/Users/example/project"))
            lower = _workspace_label(Path("/users/example/project"))
        self.assertEqual(upper, lower)

    def test_config_workspace_label_uses_captured_identity_after_root_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            config = _config(workspace)
            expected = hashlib.sha256(
                bytes.fromhex(config.expected_workspace_identity or "")
            ).hexdigest()

            workspace.rename(workspace.with_name("original"))
            workspace.mkdir()

            self.assertEqual(_workspace_label(config), expected)

    def test_argv_enforces_boundary_and_traceable_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            snapshot = root / "snapshot"
            workspace.mkdir()
            snapshot.mkdir()
            config = _config(workspace)
            argv = build_container_argv(
                "/usr/bin/docker",
                config,
                snapshot,
                "printf hello",
                "deepseek-mcp-owner",
            )
        required = {
            "--pull=never", "--log-driver=none", "--network=none", "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=512m",
            "--cpus=1", "--entrypoint=/usr/bin/env",
            "--ulimit=core=0:0",
        }
        self.assertTrue(required.issubset(argv))
        self.assertEqual(argv.count("--volume"), 1)
        self.assertEqual(argv.count("--label"), 3)
        labels = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--label"]
        self.assertIn(f"{LABEL_MANAGED}=true", labels)
        self.assertTrue(any(label.startswith(f"{LABEL_WORKSPACE}=") for label in labels))
        self.assertIn(f"{LABEL_OWNER}=deepseek-mcp-owner", labels)
        mount = argv[argv.index("--volume") + 1]
        self.assertEqual(mount, f"{snapshot.resolve()}:/workspace:ro,Z")
        self.assertNotIn(str(workspace.resolve()), mount)
        self.assertEqual(argv[-3:], ["/bin/sh", "-c", "printf hello"])
        self.assertIn("GIT_OPTIONAL_LOCKS=0", argv)

    def test_podman_mount_is_read_only_and_privately_relabelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            snapshot = root / "snapshot"
            workspace.mkdir()
            snapshot.mkdir()
            config = _config(workspace)
            argv = build_container_argv(
                "/usr/bin/podman", config, snapshot, "true", "deepseek-mcp-owner"
            )

        self.assertEqual(argv.count("--mount"), 1)
        mount = argv[argv.index("--mount") + 1]
        self.assertIn(f"source={snapshot.resolve()}", mount)
        self.assertIn("target=/workspace", mount)
        self.assertTrue(mount.endswith(",readonly,relabel=private"))

    def test_docker_mount_rejects_colon_in_snapshot_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            snapshot = root / "snapshot:unsafe"
            workspace.mkdir()
            snapshot.mkdir()
            with self.assertRaisesRegex(ContainerSandboxError, "Docker volume"):
                build_container_argv(
                    "/usr/bin/docker",
                    _config(workspace),
                    snapshot,
                    "true",
                    "deepseek-mcp-owner",
                )

    def test_runtime_environment_drops_secrets_and_proxies(self) -> None:
        values = {
            "PRIVATE_VALUE": "sensitive",
            "HTTP_PROXY": "http://proxy.invalid",
            "PATH": "/usr/bin:/bin",
            "DOCKER_HOST": "unix:///safe.sock",
        }
        with patch.dict(os.environ, values, clear=True):
            env = _runtime_environment("/usr/bin/docker")
        self.assertEqual(
            env,
            {
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": "/nonexistent",
                "DOCKER_HOST": "unix:///safe.sock",
                "DOCKER_CONFIG": "/nonexistent",
            },
        )

    def test_local_socket_policy_and_remote_rejection(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
        ):
            env = _runtime_environment("/usr/local/bin/docker")
        self.assertEqual(env["DOCKER_HOST"], "unix:///var/run/docker.sock")
        for variable, endpoint in (
            ("DOCKER_HOST", "tcp://remote.invalid:2376"),
            ("DOCKER_HOST", "ssh://builder@remote.invalid"),
            ("CONTAINER_HOST", "tcp://remote.invalid:8080"),
        ):
            with self.subTest(variable=variable):
                with patch.dict(os.environ, {variable: endpoint}, clear=True):
                    with self.assertRaisesRegex(ContainerSandboxError, "remote daemon"):
                        _runtime_environment("/usr/bin/docker")

    def test_podman_and_named_context_fail_closed(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ContainerSandboxError, "CONTAINER_HOST"),
        ):
            _runtime_environment("/usr/local/bin/podman")
        with patch.dict(os.environ, {"DOCKER_CONTEXT": "remote"}, clear=True):
            with self.assertRaisesRegex(ContainerSandboxError, "DOCKER_CONTEXT"):
                _runtime_environment("/usr/bin/docker")

    def test_bounded_capture_and_lease_cleanup(self) -> None:
        output = b"x" * (MAX_STREAM_BYTES * 3)
        process = _FakeProcess(stdout=output)
        lease_read, lease_write = os.pipe()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                config = _config(Path(tmpdir))
                with _run_patches(process) as stack:
                    cleanup = stack.enter_context(
                        patch("deepseek_mcp.container_sandbox.cleanup_stale_containers")
                    )
                    result = run_in_container(
                        "printf hello", config, 10, lease_fd=lease_read
                    )
                    cleanup.assert_called_once_with(config, lease_read)
        finally:
            os.close(lease_read)
            os.close(lease_write)
        self.assertEqual(len(result.stdout), MAX_STREAM_BYTES)
        self.assertEqual(result.stdout_total, len(output))
        self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_runtime_popen_does_not_inherit_lease_or_use_host_shell(self) -> None:
        process = _FakeProcess()
        lease_read, lease_write = os.pipe()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                config = _config(Path(tmpdir))
                with _run_patches(process) as stack:
                    stack.enter_context(
                        patch("deepseek_mcp.container_sandbox.cleanup_stale_containers")
                    )
                    with patch(
                        "deepseek_mcp.container_sandbox.subprocess.Popen",
                        return_value=process,
                    ) as popen:
                        run_in_container("pwd", config, 5, lease_fd=lease_read)
        finally:
            os.close(lease_read)
            os.close(lease_write)
        call = popen.call_args
        self.assertFalse(call.kwargs["shell"])
        self.assertIs(call.kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("pass_fds", call.kwargs)
        self.assertTrue(call.kwargs["start_new_session"])

    def test_timeout_kills_group_and_confirms_removal(self) -> None:
        timeout = subprocess.TimeoutExpired(cmd="docker", timeout=7)
        process = _FakeProcess(waits=[timeout, -9])
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with _run_patches(process) as stack:
                killpg = stack.enter_context(
                    patch("deepseek_mcp.container_sandbox.os.killpg")
                )
                remove = stack.enter_context(
                    patch(
                        "deepseek_mcp.container_sandbox._remove_container_confirmed",
                        return_value=True,
                    )
                )
                result = run_in_container("sleep 30", config, 7)
        self.assertTrue(result.timed_out)
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        remove.assert_called_once()

    def test_first_reader_start_failure_uses_unified_cleanup(self) -> None:
        process = _FakeProcess()
        readers = [_ReaderDouble(RuntimeError("first start")), _ReaderDouble()]
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with _run_patches(process) as stack:
                stack.enter_context(
                    patch("deepseek_mcp.container_sandbox.threading.Thread", side_effect=readers)
                )
                remove = stack.enter_context(
                    patch(
                        "deepseek_mcp.container_sandbox._remove_container_confirmed",
                        return_value=True,
                    )
                )
                killpg = stack.enter_context(
                    patch("deepseek_mcp.container_sandbox.os.killpg")
                )
                with self.assertRaisesRegex(ContainerSandboxError, "first start"):
                    run_in_container("pwd", config, 5)
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        remove.assert_called_once()
        self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_second_reader_start_failure_reaps_first_reader(self) -> None:
        process = _FakeProcess()
        first = _ReaderDouble()
        second = _ReaderDouble(RuntimeError("second start"))
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with _run_patches(process) as stack:
                stack.enter_context(
                    patch(
                        "deepseek_mcp.container_sandbox.threading.Thread",
                        side_effect=[first, second],
                    )
                )
                with self.assertRaisesRegex(ContainerSandboxError, "second start"):
                    run_in_container("pwd", config, 5)
        self.assertTrue(first.started)
        self.assertGreater(first.joined, 0)
        self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_wait_exception_uses_unified_cleanup(self) -> None:
        process = _FakeProcess()
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with _run_patches(process) as stack:
                stack.enter_context(
                    patch(
                        "deepseek_mcp.container_sandbox._wait",
                        side_effect=RuntimeError("wait broke"),
                    )
                )
                remove = stack.enter_context(
                    patch(
                        "deepseek_mcp.container_sandbox._remove_container_confirmed",
                        return_value=True,
                    )
                )
                with self.assertRaisesRegex(ContainerSandboxError, "wait broke"):
                    run_in_container("pwd", config, 5)
        remove.assert_called_once()
        self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_capture_exception_repeats_confirmed_cleanup(self) -> None:
        process = _FakeProcess(stdout=b"ok")
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with _run_patches(process) as stack:
                remove = stack.enter_context(
                    patch(
                        "deepseek_mcp.container_sandbox._remove_container_confirmed",
                        return_value=True,
                    )
                )
                stack.enter_context(
                    patch(
                        "deepseek_mcp.container_sandbox._capture_result",
                        side_effect=RuntimeError("capture broke"),
                    )
                )
                with self.assertRaisesRegex(ContainerSandboxError, "capture broke"):
                    run_in_container("pwd", config, 5)
        self.assertEqual(remove.call_count, 2)
        self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_reader_capture_error_is_observed_after_cleanup(self) -> None:
        process = _FakeProcess(stdout=b"output")
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with _run_patches(process) as stack:
                stack.enter_context(
                    patch(
                        "deepseek_mcp.container_sandbox.CapturedStream.append",
                        side_effect=RuntimeError("capture append broke"),
                    )
                )
                with self.assertRaisesRegex(ContainerSandboxError, "capture container"):
                    run_in_container("pwd", config, 5)
        self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_runtime_control_capture_error_fails_closed(self) -> None:
        process = _FakeProcess(stdout=b"inventory")
        with (
            patch("deepseek_mcp.container_process.subprocess.Popen", return_value=process),
            patch(
                "deepseek_mcp.container_process.CapturedStream.append",
                side_effect=RuntimeError("capture broke"),
            ),
            self.assertRaisesRegex(ContainerSandboxError, "control capture"),
        ):
            run_control("/usr/bin/docker", ["ps"], {}, capture=True)
        self.assertTrue(process.stdout.closed)

    def test_name_absence_requires_successful_bounded_inventory(self) -> None:
        present = SimpleNamespace(
            returncode=0,
            stdout=b"deepseek-mcp-target\n",
            stdout_total=len(b"deepseek-mcp-target\n"),
        )
        absent = SimpleNamespace(returncode=0, stdout=b"", stdout_total=0)
        with patch(
            "deepseek_mcp.container_process.run_control",
            side_effect=[present, absent],
        ) as control:
            self.assertFalse(
                container_name_absent("docker", "deepseek-mcp-target", {})
            )
            self.assertTrue(
                container_name_absent("docker", "deepseek-mcp-target", {})
            )

        expected = [
            "ps", "-a", "--filter", "name=deepseek-mcp-target",
            "--format", "{{.Names}}",
        ]
        self.assertEqual(control.call_args_list[0].args[1], expected)
        self.assertTrue(control.call_args_list[0].kwargs["capture"])

    def test_remove_does_not_treat_daemon_health_as_container_absence(self) -> None:
        failed = SimpleNamespace(returncode=1)
        inventory = SimpleNamespace(
            returncode=0,
            stdout=b"deepseek-mcp-target\n",
            stdout_total=len(b"deepseek-mcp-target\n"),
        )
        with (
            patch(
                "deepseek_mcp.container_sandbox._run_control",
                side_effect=[failed, failed],
            ) as control,
            patch(
                "deepseek_mcp.container_process.run_control",
                return_value=inventory,
            ),
        ):
            self.assertFalse(
                _remove_container_confirmed(
                    "docker", "deepseek-mcp-target", {}
                )
            )

        self.assertEqual(len(control.call_args_list), 2)
        self.assertNotIn(["info"], [item.args[1] for item in control.call_args_list])

    def test_cleanup_substep_error_still_closes_and_reaps_readers(self) -> None:
        process = _FakeProcess()
        first = _ReaderDouble()
        second = _ReaderDouble()
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with _run_patches(process) as stack:
                stack.enter_context(
                    patch(
                        "deepseek_mcp.container_sandbox.threading.Thread",
                        side_effect=[first, second],
                    )
                )
                stack.enter_context(
                    patch(
                        "deepseek_mcp.container_sandbox._remove_container_confirmed",
                        side_effect=RuntimeError("remove broke"),
                    )
                )
                with self.assertRaisesRegex(ContainerSandboxError, "container removal"):
                    run_in_container("pwd", config, 5)
        self.assertGreater(first.joined, 0)
        self.assertGreater(second.joined, 0)
        self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_unconfirmed_removal_fails_closed_and_triggers_watchdog(self) -> None:
        process = _FakeProcess()
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with _run_patches(process) as stack:
                stack.enter_context(
                    patch(
                        "deepseek_mcp.container_sandbox._remove_container_confirmed",
                        return_value=False,
                    )
                )
                stop = stack.enter_context(
                    patch("deepseek_mcp.container_sandbox.stop_watchdog", return_value=True)
                )
                with self.assertRaisesRegex(ContainerSandboxError, "container removal"):
                    run_in_container("pwd", config, 5)
        stop.assert_called_once_with(ANY, cleanup_now=True)

    def test_watchdog_start_failure_never_starts_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with _run_patches(_FakeProcess()) as stack:
                stack.enter_context(patch(
                    "deepseek_mcp.container_sandbox.start_watchdog",
                    side_effect=WatchdogError("watchdog failed"),
                ))
                popen = stack.enter_context(
                    patch("deepseek_mcp.container_sandbox.subprocess.Popen")
                )
                with self.assertRaisesRegex(ContainerSandboxError, "watchdog failed"):
                    run_in_container("pwd", config, 5)
        popen.assert_not_called()

    def test_watchdog_handshake_inherits_lease_and_control_fds(self) -> None:
        watchdog_process = _FakeProcess()
        lease_read, lease_write = os.pipe()
        try:
            with (
                patch("deepseek_mcp.container_watchdog.os.pipe", side_effect=[(100, 101), (102, 103)]),
                patch("deepseek_mcp.container_watchdog._close_fd"),
                patch("deepseek_mcp.container_watchdog.select.select", return_value=([100], [], [])),
                patch("deepseek_mcp.container_watchdog.os.read", return_value=b"R"),
                patch(
                    "deepseek_mcp.container_watchdog.subprocess.Popen",
                    return_value=watchdog_process,
                ) as popen,
            ):
                watchdog = start_watchdog(
                    "/usr/bin/docker", "deepseek-mcp-owner", {}, 9,
                    (lease_read,), Path("/tmp/deepseek-mcp-snapshot"),
                )
        finally:
            os.close(lease_read)
            os.close(lease_write)
        self.assertEqual(watchdog.control_fd, 103)
        self.assertEqual(set(popen.call_args.kwargs["pass_fds"]), {lease_read, 101, 102})
        self.assertFalse(popen.call_args.kwargs["shell"])
        argv = popen.call_args.args[0]
        self.assertIn("/tmp/deepseek-mcp-snapshot", argv)
        self.assertEqual(argv[argv.index("--snapshot-root") + 1], "/tmp")

    def test_stale_cleanup_is_scoped_and_reconfirmed(self) -> None:
        lease_read, lease_write = os.pipe()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                config = _config(Path(tmpdir))
                with (
                    patch("deepseek_mcp.config.sys.platform", "linux"),
                    patch("deepseek_mcp.config.shutil.which", return_value="/usr/bin/docker"),
                    patch(
                        "deepseek_mcp.config.validate_trusted_executable",
                        return_value=Path("/usr/bin/docker"),
                    ),
                    patch(
                        "deepseek_mcp.container_sandbox._list_stale_containers",
                        side_effect=[["deepseek-mcp-old"], []],
                    ) as listed,
                    patch(
                        "deepseek_mcp.container_sandbox._remove_container_confirmed",
                        return_value=True,
                    ) as removed,
                ):
                    cleanup_stale_containers(config, lease_read)
        finally:
            os.close(lease_read)
            os.close(lease_write)
        self.assertEqual(listed.call_count, 2)
        removed.assert_called_once()
        self.assertEqual(removed.call_args.args[1], "deepseek-mcp-old")
        self.assertNotIn(lease_read, removed.call_args.args)

    def test_unconfirmed_stale_cleanup_prevents_any_start(self) -> None:
        lease_read, lease_write = os.pipe()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                config = _config(Path(tmpdir))
                with (
                    patch("deepseek_mcp.config.sys.platform", "linux"),
                    patch("deepseek_mcp.config.shutil.which", return_value="/usr/bin/docker"),
                    patch(
                        "deepseek_mcp.config.validate_trusted_executable",
                        return_value=Path("/usr/bin/docker"),
                    ),
                    patch(
                        "deepseek_mcp.container_sandbox.cleanup_stale_containers",
                        side_effect=ContainerSandboxError("inventory unknown"),
                    ),
                    patch("deepseek_mcp.container_sandbox.start_watchdog") as watchdog,
                    patch("deepseek_mcp.container_sandbox.subprocess.Popen") as popen,
                    self.assertRaisesRegex(ContainerSandboxError, "inventory unknown"),
                ):
                    run_in_container("pwd", config, 5, lease_fd=lease_read)
        finally:
            os.close(lease_read)
            os.close(lease_write)
        watchdog.assert_not_called()
        popen.assert_not_called()

    def test_stuck_reader_closes_pipes_and_fails_closed(self) -> None:
        stdout = io.BytesIO(b"output")
        stderr = io.BytesIO()
        self.assertFalse(_join_readers([_StuckReader()], [stdout, stderr]))
        self.assertTrue(stdout.closed and stderr.closed)

    def test_missing_runtime_never_starts_any_host_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(Path(tmpdir))
            with (
                patch("deepseek_mcp.config.sys.platform", "linux"),
                patch("deepseek_mcp.config.shutil.which", return_value=None),
                patch("deepseek_mcp.container_sandbox.subprocess.Popen") as popen,
                self.assertRaisesRegex(ContainerSandboxError, "runtime not found"),
            ):
                run_in_container("pwd", config, 5)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
