"""Run untrusted shell commands in a locked-down, managed OCI container."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO
from urllib.parse import urlparse

from .container_process import (
    MAX_STREAM_BYTES,
    CapturedStream,
    ContainerSandboxError,
    container_name_absent as _container_name_absent,
    drain as _drain,
    join_readers as _join_readers,
    lease_pass_fds as _lease_pass_fds,
    run_control as _run_control,
    stop_process as _stop_process,
    wait_process as _wait,
)
from .container_watchdog import (
    WatchdogError,
    WatchdogHandle,
    start_watchdog,
    stop_watchdog,
)
from .execution_lock import (
    WorkspaceExecutionLease,
    WorkspaceLockError,
    acquire_workspace_lease,
)
from .workspace_snapshot import (
    WorkspaceSnapshotError,
    cleanup_stale_snapshots,
    cleanup_workspace_snapshot,
    create_workspace_snapshot,
)
from .execution_lock import workspace_identity
from .workspace_guard import expected_identity, require_workspace_identity

if TYPE_CHECKING:
    from .config import Config

CONTAINER_WORKSPACE = "/workspace"
CONTAINER_HOME = "/home/deepseek"
CONTAINER_TMP = "/tmp"
CONTAINER_PATH = "/usr/local/bin:/usr/bin:/bin"
ENGINE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
MAX_STALE_CONTAINERS = 64
LABEL_MANAGED = "io.deepseek-mcp.managed"
LABEL_WORKSPACE = "io.deepseek-mcp.workspace"
LABEL_OWNER = "io.deepseek-mcp.owner"
_MANAGED_NAME = re.compile(r"deepseek-mcp-[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class ContainerResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_total: int
    stderr_total: int
    timed_out: bool


@dataclass(frozen=True)
class _CleanupResult:
    process_stopped: bool
    container_removed: bool
    readers_stopped: bool

    @property
    def ok(self) -> bool:
        return self.process_stopped and self.container_removed and self.readers_stopped


def _local_socket_endpoint(runtime: str) -> tuple[str, str]:
    is_docker = Path(runtime).name == "docker"
    host_variable = "DOCKER_HOST" if is_docker else "CONTAINER_HOST"
    default_socket = (
        "/var/run/docker.sock"
        if is_docker
        else f"/run/user/{os.getuid()}/podman/podman.sock"
    )
    if not is_docker and not os.environ.get(host_variable):
        raise ContainerSandboxError(
            "Podman requires CONTAINER_HOST=unix:///local/socket backed by "
            "podman system service"
        )
    endpoint = os.environ.get(host_variable) or f"unix://{default_socket}"
    parsed = urlparse(endpoint)
    if parsed.scheme != "unix" or parsed.netloc or not parsed.path.startswith("/"):
        raise ContainerSandboxError(
            f"{host_variable} must identify a local unix:// socket"
        )
    return host_variable, endpoint


def _runtime_environment(runtime: str) -> dict[str, str]:
    context = os.environ.get("DOCKER_CONTEXT")
    if context not in (None, "", "default"):
        raise ContainerSandboxError("DOCKER_CONTEXT must be unset or 'default'")
    for variable in ("DOCKER_HOST", "CONTAINER_HOST"):
        endpoint = os.environ.get(variable)
        if endpoint and urlparse(endpoint).scheme != "unix":
            raise ContainerSandboxError(f"{variable} cannot target a remote daemon")
    host_variable, endpoint = _local_socket_endpoint(runtime)
    env = {"PATH": ENGINE_PATH, "HOME": "/nonexistent"}
    env[host_variable] = endpoint
    if Path(runtime).name == "docker":
        env["DOCKER_CONFIG"] = "/nonexistent"
    return env


def _workspace_mount(runtime: str, snapshot: Path) -> tuple[str, str]:
    resolved = snapshot.resolve()
    if not resolved.is_dir():
        raise ContainerSandboxError(f"Workspace is not a directory: {resolved}")
    path = str(resolved)
    if any(char in path for char in "\n\r"):
        raise ContainerSandboxError("Workspace path contains unsupported mount characters")
    if Path(runtime).name == "docker":
        if ":" in path:
            raise ContainerSandboxError(
                "Workspace path contains unsupported Docker volume characters"
            )
        return "--volume", f"{path}:{CONTAINER_WORKSPACE}:ro,Z"
    if "," in path:
        raise ContainerSandboxError(
            "Workspace path contains unsupported Podman mount characters"
        )
    return (
        "--mount",
        f"type=bind,source={path},target={CONTAINER_WORKSPACE},"
        "readonly,relabel=private",
    )


def _workspace_label(source: Config | Path) -> str:
    if isinstance(source, Path):
        require_workspace_identity(source)
        identity = workspace_identity(source)
    else:
        assert source.expected_workspace_identity is not None
        identity = expected_identity(source.expected_workspace_identity)
    return hashlib.sha256(identity).hexdigest()


def build_container_argv(
    runtime: str,
    config: Config,
    snapshot: Path,
    command: str,
    container_name: str,
) -> list[str]:
    """Build argv without a host shell; command parsing happens only in-container."""
    uid = os.getuid()
    gid = os.getgid()
    home_opts = f"rw,noexec,nosuid,nodev,size=64m,mode=0700,uid={uid},gid={gid}"
    labels = [
        f"{LABEL_MANAGED}=true",
        f"{LABEL_WORKSPACE}={_workspace_label(config)}",
        f"{LABEL_OWNER}={container_name}",
    ]
    argv = [
        runtime, "run", "--rm", f"--name={container_name}",
        "--pull=never", "--log-driver=none",
    ]
    mount_flag, mount_value = _workspace_mount(runtime, snapshot)
    for label in labels:
        argv.extend(["--label", label])
    argv.extend([
        "--network=none", "--read-only", mount_flag, mount_value,
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        f"--tmpfs={CONTAINER_HOME}:{home_opts}", f"--workdir={CONTAINER_WORKSPACE}",
        f"--user={uid}:{gid}", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        f"--pids-limit={int(config.bash_pids_limit)}", f"--memory={config.bash_memory}",
        f"--cpus={float(config.bash_cpus):g}", "--init", "--entrypoint=/usr/bin/env",
        "--ulimit=core=0:0",
        config.bash_image or "", "-i", f"HOME={CONTAINER_HOME}", f"TMPDIR={CONTAINER_TMP}",
        f"PATH={CONTAINER_PATH}", "LANG=C.UTF-8", "LC_ALL=C.UTF-8",
        "GIT_OPTIONAL_LOCKS=0", "GIT_CONFIG_COUNT=1",
        "GIT_CONFIG_KEY_0=safe.directory", f"GIT_CONFIG_VALUE_0={CONTAINER_WORKSPACE}",
        "/bin/sh", "-c", command,
    ])
    return argv


def _remove_container_confirmed(
    runtime: str,
    name: str,
    env: dict[str, str],
) -> bool:
    try:
        removed = _run_control(runtime, ["rm", "-f", name], env)
        if removed.returncode == 0:
            return True
        inspected = _run_control(runtime, ["container", "inspect", name], env)
        if inspected.returncode == 0:
            return False
        return _container_name_absent(runtime, name, env)
    except ContainerSandboxError:
        return False


def _list_stale_containers(
    runtime: str,
    config: Config,
    env: dict[str, str],
) -> list[str]:
    filters = [
        "ps", "-a", "--filter", f"label={LABEL_MANAGED}=true",
        "--filter", f"label={LABEL_WORKSPACE}={_workspace_label(config)}",
        "--format", "{{.Names}}",
    ]
    result = _run_control(runtime, filters, env, capture=True)
    if result.returncode != 0 or result.stdout_total > MAX_STREAM_BYTES:
        raise ContainerSandboxError("Could not confirm the managed-container inventory")
    try:
        names = result.stdout.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ContainerSandboxError("Managed-container inventory was not valid ASCII") from exc
    if len(names) > MAX_STALE_CONTAINERS:
        raise ContainerSandboxError("Too many managed containers to clean safely")
    if any(not _MANAGED_NAME.fullmatch(name) for name in names):
        raise ContainerSandboxError("Managed-container inventory contained an unsafe name")
    return names


def cleanup_stale_containers(config: Config, lease_fd: int) -> None:
    """Remove only this workspace's labeled containers while its lease is held."""
    _lease_pass_fds(lease_fd)
    try:
        runtime = config.resolve_bash_runtime()
    except RuntimeError as exc:
        raise ContainerSandboxError(str(exc)) from exc
    env = _runtime_environment(runtime)
    for name in _list_stale_containers(runtime, config, env):
        if not _remove_container_confirmed(runtime, name, env):
            raise ContainerSandboxError(f"Failed to remove stale container {name}")
    if _list_stale_containers(runtime, config, env):
        raise ContainerSandboxError("Stale container cleanup could not be confirmed")


def _cleanup_launched(
    process: subprocess.Popen[bytes],
    runtime: str,
    name: str,
    env: dict[str, str],
    readers: list[threading.Thread],
    streams: list[BinaryIO],
) -> _CleanupResult:
    try:
        stopped = _stop_process(process)
    except BaseException:
        stopped = False
    try:
        removed = _remove_container_confirmed(runtime, name, env)
    except BaseException:
        removed = False
    try:
        readers_stopped = _join_readers(readers, streams)
    except BaseException:
        readers_stopped = False
    return _CleanupResult(stopped, removed, readers_stopped)


def _capture_result(
    process: subprocess.Popen[bytes],
    stdout: CapturedStream,
    stderr: CapturedStream,
    timed_out: bool,
) -> ContainerResult:
    for label, capture in (("stdout", stdout), ("stderr", stderr)):
        if capture.error is not None:
            raise ContainerSandboxError(f"Failed to capture container {label}") from capture.error
    return ContainerResult(
        process.returncode, bytes(stdout.data), bytes(stderr.data),
        stdout.total_bytes, stderr.total_bytes, timed_out,
    )


def _raise_lifecycle_error(
    cause: BaseException | None,
    cleanup: _CleanupResult,
    watchdog_ok: bool,
) -> None:
    failures = []
    if not cleanup.process_stopped:
        failures.append("runtime process")
    if not cleanup.container_removed:
        failures.append("container removal")
    if not cleanup.readers_stopped:
        failures.append("output readers")
    if not watchdog_ok:
        failures.append("watchdog")
    detail = f"; cleanup failed: {', '.join(failures)}" if failures else ""
    reason = str(cause) if cause is not None else "container lifecycle failed"
    raise ContainerSandboxError(f"{reason}{detail}") from cause


def _start_output_readers(
    process: subprocess.Popen[bytes], readers: list[threading.Thread]
) -> tuple[CapturedStream, CapturedStream]:
    if process.stdout is None or process.stderr is None:
        raise ContainerSandboxError("Container runtime pipes were not created")
    stdout = CapturedStream()
    stderr = CapturedStream()
    readers.extend([
        threading.Thread(target=_drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, stderr), daemon=True),
    ])
    for reader in readers:
        reader.start()
    return stdout, stderr


def _run_launched_container(
    process: subprocess.Popen[bytes],
    watchdog: WatchdogHandle,
    runtime: str,
    name: str,
    env: dict[str, str],
    timeout: int,
) -> ContainerResult:
    readers: list[threading.Thread] = []
    streams: list[BinaryIO] = []
    stdout: CapturedStream | None = None
    stderr: CapturedStream | None = None
    cause: BaseException | None = None
    timed_out = False
    try:
        streams.extend(
            stream for stream in (process.stdout, process.stderr) if stream is not None
        )
        stdout, stderr = _start_output_readers(process, readers)
        timed_out, stopped = _wait(process, timeout)
        if not stopped:
            raise ContainerSandboxError("Container runtime process did not terminate")
    except BaseException as exc:
        cause = exc
    cleanup = _cleanup_launched(
        process, runtime, name, env, readers, streams
    )
    if cause is not None or not cleanup.ok:
        watchdog_ok = stop_watchdog(
            watchdog, cleanup_now=not cleanup.container_removed
        )
        _raise_lifecycle_error(cause, cleanup, watchdog_ok)
    assert stdout is not None and stderr is not None
    try:
        result = _capture_result(process, stdout, stderr, timed_out)
    except BaseException as exc:
        cleanup = _cleanup_launched(
            process, runtime, name, env, readers, streams
        )
        watchdog_ok = stop_watchdog(
            watchdog, cleanup_now=not cleanup.container_removed
        )
        _raise_lifecycle_error(exc, cleanup, watchdog_ok)
    if not stop_watchdog(watchdog, cleanup_now=False):
        raise ContainerSandboxError("Container watchdog did not terminate cleanly")
    return result


def _prepare_snapshot(config: Config) -> Path:
    workspace_label = _workspace_label(config)
    try:
        cleanup_stale_snapshots(workspace_label)
        snapshot = create_workspace_snapshot(config.workspace, workspace_label)
    except WorkspaceSnapshotError as exc:
        raise ContainerSandboxError(str(exc)) from exc
    return snapshot


def _cleanup_snapshot_after_error(snapshot: Path) -> None:
    try:
        cleanup_workspace_snapshot(snapshot)
    except WorkspaceSnapshotError as cleanup_error:
        raise ContainerSandboxError(
            "Workspace snapshot preparation failed and cleanup was not confirmed"
        ) from None


def _run_with_lease(
    command: str,
    config: Config,
    timeout: int,
    lease_fd: int,
    runtime: str,
) -> ContainerResult:
    lease_fds = _lease_pass_fds(lease_fd)
    cleanup_stale_containers(config, lease_fd)
    snapshot = _prepare_snapshot(config)
    name = f"deepseek-mcp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        env = _runtime_environment(runtime)
        argv = build_container_argv(runtime, config, snapshot, command, name)
    except BaseException:
        _cleanup_snapshot_after_error(snapshot)
        raise
    try:
        watchdog = start_watchdog(
            runtime, name, env, timeout, lease_fds, snapshot
        )
    except WatchdogError as exc:
        _cleanup_snapshot_after_error(snapshot)
        raise ContainerSandboxError(str(exc)) from None
    try:
        process = subprocess.Popen(
            argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, close_fds=True,
            start_new_session=True,
        )
    except Exception as exc:
        watchdog_ok = stop_watchdog(watchdog, cleanup_now=False)
        detail = "" if watchdog_ok else "; watchdog cleanup failed"
        raise ContainerSandboxError(
            f"Failed to start container runtime: {exc}{detail}"
        ) from exc
    return _run_launched_container(
        process, watchdog, runtime, name, env, timeout
    )


def _acquire_local_lease(config: Config) -> WorkspaceExecutionLease:
    try:
        assert config.expected_workspace_identity is not None
        return acquire_workspace_lease(
            config.workspace,
            expected_identity=expected_identity(config.expected_workspace_identity),
        )
    except WorkspaceLockError as exc:
        raise ContainerSandboxError(str(exc)) from exc


def run_in_container(
    command: str,
    config: Config,
    timeout: int,
    lease_fd: int | None = None,
) -> ContainerResult:
    try:
        runtime = config.resolve_bash_runtime()
    except RuntimeError as exc:
        raise ContainerSandboxError(str(exc)) from exc
    if lease_fd is not None:
        return _run_with_lease(command, config, timeout, lease_fd, runtime)
    lease = _acquire_local_lease(config)
    try:
        return _run_with_lease(command, config, timeout, lease.fileno(), runtime)
    finally:
        lease.release()
