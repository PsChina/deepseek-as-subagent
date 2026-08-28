"""Independent last-resort cleanup for one managed container.

The normal lifecycle is controlled by ``container_sandbox`` through a pipe.
Only a lost parent or an explicit emergency signal reaches the bounded timer;
the timer is a crash-safety backstop, never the primary completion path.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import select
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .child_runtime import (
    child_working_directory,
    isolated_child_argv,
    sanitized_python_environment,
)
from .container_process import ContainerSandboxError, container_name_absent
from .workspace_snapshot import WorkspaceSnapshotError, cleanup_workspace_snapshot
from .hard_deadline import Deadline, HardDeadline, remaining

COMMAND_TIMEOUT = 15
GRACE_SECONDS = 30
START_TIMEOUT = 5
STOP_TIMEOUT = (COMMAND_TIMEOUT * 3) + 5
MIN_RETRY_SECONDS = 1.0
MAX_RETRY_SECONDS = 30.0
DONE = b"D"
CLEANUP_NOW = b"C"
_SAFE_NAME = re.compile(r"deepseek-mcp-[A-Za-z0-9_.-]+")


class WatchdogError(RuntimeError):
    """The watchdog process could not be started or stopped safely."""


@dataclass
class WatchdogHandle:
    process: subprocess.Popen[bytes]
    control_fd: int


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _stop_process(process: subprocess.Popen[bytes]) -> bool:
    if process.returncode is not None:
        return True
    _kill_process(process)
    for _ in range(2):
        try:
            process.wait(timeout=COMMAND_TIMEOUT)
            return True
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
        except OSError:
            return False
    return False


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _open_control_pipes() -> tuple[int, int, int, int]:
    ready_read, ready_write = os.pipe()
    try:
        control_read, control_write = os.pipe()
    except OSError as exc:
        _close_fd(ready_read)
        _close_fd(ready_write)
        raise WatchdogError(f"Failed to create watchdog control pipe: {exc}") from exc
    return ready_read, ready_write, control_read, control_write


def _watchdog_argv(
    runtime: str,
    name: str,
    timeout: int,
    ready_fd: int,
    control_fd: int,
    snapshot: Path,
) -> list[str]:
    return isolated_child_argv(
        "deepseek_mcp.container_watchdog",
        "--runtime",
        runtime,
        "--name",
        name,
        "--deadline-seconds",
        str(timeout + GRACE_SECONDS),
        "--ready-fd",
        str(ready_fd),
        "--control-fd",
        str(control_fd),
        "--snapshot",
        str(snapshot),
        "--snapshot-root",
        str(snapshot.parent),
    )


def start_watchdog(
    runtime: str,
    name: str,
    env: dict[str, str],
    timeout: int,
    lease_fds: tuple[int, ...],
    snapshot: Path,
) -> WatchdogHandle:
    ready_read, ready_write, control_read, control_write = _open_control_pipes()
    inherited = (*lease_fds, ready_write, control_read)
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            _watchdog_argv(
                runtime,
                name,
                timeout,
                ready_write,
                control_read,
                snapshot,
            ),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=sanitized_python_environment(env),
            cwd=child_working_directory(),
            close_fds=True,
            pass_fds=inherited,
            start_new_session=True,
        )
        _close_fd(ready_write)
        _close_fd(control_read)
        readable, _, _ = select.select([ready_read], [], [], START_TIMEOUT)
        ready = os.read(ready_read, 1) if readable else b""
        if ready != b"R":
            raise WatchdogError("Container watchdog did not become ready")
        return WatchdogHandle(process, control_write)
    except BaseException as exc:
        _close_fd(control_write)
        if process is not None and not _stop_process(process):
            raise WatchdogError("Container watchdog startup cleanup failed") from exc
        if isinstance(exc, WatchdogError):
            raise
        raise WatchdogError(f"Failed to start container watchdog: {exc}") from exc
    finally:
        _close_fd(ready_read)
        _close_fd(ready_write)
        _close_fd(control_read)


def stop_watchdog(watchdog: WatchdogHandle, *, cleanup_now: bool) -> bool:
    action = CLEANUP_NOW if cleanup_now else DONE
    try:
        os.write(watchdog.control_fd, action)
    except OSError:
        pass
    finally:
        _close_fd(watchdog.control_fd)
    try:
        watchdog.process.wait(timeout=STOP_TIMEOUT)
        return watchdog.process.returncode == 0
    except subprocess.TimeoutExpired:
        # A cleanup watchdog must retain its inherited workspace lease until it
        # confirms both container and snapshot removal. Never kill it merely
        # because the parent-side wait budget expired.
        return False
    except OSError:
        return False


def _run(runtime: str, args: list[str]) -> int | None:
    try:
        process = subprocess.Popen(
            [runtime, *args],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        return process.wait(timeout=COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_process(process)
        try:
            process.wait(timeout=COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired:
            return None
        return None


def _remove_confirmed(runtime: str, name: str) -> bool:
    if _run(runtime, ["rm", "-f", name]) == 0:
        return True
    if _run(runtime, ["container", "inspect", name]) == 0:
        return False
    try:
        return container_name_absent(runtime, name, dict(os.environ))
    except ContainerSandboxError:
        return False


def _snapshot_removed(snapshot: Path, snapshot_root: Path) -> bool:
    try:
        cleanup_workspace_snapshot(snapshot, snapshot_root)
        return True
    except WorkspaceSnapshotError:
        return False


def _cleanup_until_confirmed(
    runtime: str,
    name: str,
    snapshot: Path,
    snapshot_root: Path,
    remove_container: bool,
) -> None:
    delay = MIN_RETRY_SECONDS
    container_removed = not remove_container
    while True:
        if not container_removed:
            container_removed = _remove_confirmed(runtime, name)
        if container_removed and _snapshot_removed(snapshot, snapshot_root):
            return
        time.sleep(delay)
        delay = min(delay * 2, MAX_RETRY_SECONDS)


def _signal_ready(ready_fd: int) -> bool:
    try:
        os.write(ready_fd, b"R")
        return True
    except OSError:
        return False
    finally:
        try:
            os.close(ready_fd)
        except OSError:
            pass


def _control_action(control_fd: int, deadline: Deadline) -> bytes:
    while (left := remaining(deadline)) > 0:
        readable, _, _ = select.select(
            [control_fd], [], [], min(1.0, left)
        )
        if not readable:
            continue
        try:
            action = os.read(control_fd, 1)
        except OSError:
            action = b""
        if action in (DONE, CLEANUP_NOW):
            return action
        # EOF means the supervising tool process exited or crashed.
        return CLEANUP_NOW
    return CLEANUP_NOW


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--deadline-seconds", type=float, required=True)
    parser.add_argument("--ready-fd", type=int, required=True)
    parser.add_argument("--control-fd", type=int, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    valid = (
        math.isfinite(args.deadline_seconds)
        and args.deadline_seconds > 0
        and os.path.isabs(args.runtime)
        and args.snapshot.is_absolute()
        and args.snapshot_root.is_absolute()
        and args.snapshot.parent == args.snapshot_root
        and _SAFE_NAME.fullmatch(args.name) is not None
    )
    if not valid or not _signal_ready(args.ready_fd):
        return 2
    deadline = HardDeadline.after(args.deadline_seconds)
    try:
        action = _control_action(args.control_fd, deadline)
    finally:
        try:
            os.close(args.control_fd)
        except OSError:
            pass
    if action == DONE:
        _cleanup_until_confirmed(
            args.runtime,
            args.name,
            args.snapshot,
            args.snapshot_root,
            remove_container=False,
        )
        return 0
    _cleanup_until_confirmed(
        args.runtime,
        args.name,
        args.snapshot,
        args.snapshot_root,
        remove_container=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
