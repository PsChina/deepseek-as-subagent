"""Bounded subprocess and pipe primitives for the container boundary."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from typing import BinaryIO

MAX_STREAM_BYTES = 25_000
ENGINE_CLEANUP_TIMEOUT = 15


class ContainerSandboxError(RuntimeError):
    """The required container boundary could not be established or cleaned up."""


@dataclass
class CapturedStream:
    data: bytearray = field(default_factory=bytearray)
    total_bytes: int = 0
    error: BaseException | None = None

    def append(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        remaining = MAX_STREAM_BYTES - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])


@dataclass(frozen=True)
class ControlResult:
    returncode: int
    stdout: bytes
    stdout_total: int


def lease_pass_fds(lease_fd: int | None) -> tuple[int, ...]:
    if lease_fd is None:
        return ()
    if isinstance(lease_fd, bool) or not isinstance(lease_fd, int) or lease_fd < 0:
        raise ContainerSandboxError("lease_fd must be an open file descriptor")
    try:
        os.fstat(lease_fd)
    except OSError as exc:
        raise ContainerSandboxError("lease_fd must be an open file descriptor") from exc
    return (lease_fd,)


def drain(stream: BinaryIO, capture: CapturedStream) -> None:
    try:
        while chunk := stream.read(8192):
            capture.append(chunk)
    except BaseException as exc:
        capture.error = exc


def kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        try:
            process.kill()
        except OSError:
            return


def stop_process(process: subprocess.Popen[bytes]) -> bool:
    if process.returncode is not None:
        return True
    kill_process_group(process)
    for _ in range(2):
        try:
            process.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
        except BaseException:
            try:
                process.kill()
            except OSError:
                pass
    return False


def wait_process(process: subprocess.Popen[bytes], timeout: int) -> tuple[bool, bool]:
    try:
        process.wait(timeout=timeout)
        return False, True
    except subprocess.TimeoutExpired:
        return True, stop_process(process)


def join_readers(readers: list[threading.Thread], streams: list[BinaryIO]) -> bool:
    ok = True
    for reader in readers:
        try:
            reader.join(timeout=5)
        except RuntimeError:
            continue
        except BaseException:
            ok = False
    for stream in streams:
        try:
            stream.close()
        except BaseException:
            ok = False
    alive = []
    for reader in readers:
        try:
            if reader.is_alive():
                reader.join(timeout=1)
            if reader.is_alive():
                alive.append(reader)
        except RuntimeError:
            continue
        except BaseException:
            ok = False
    return ok and not alive


def _start_capture(
    process: subprocess.Popen[bytes], capture: bool, captured: CapturedStream
) -> tuple[threading.Thread | None, list[BinaryIO]]:
    if not capture:
        return None, []
    if process.stdout is None:
        raise ContainerSandboxError("Container runtime control pipe was not created")
    reader = threading.Thread(target=drain, args=(process.stdout, captured), daemon=True)
    reader.start()
    return reader, [process.stdout]


def _raise_control_failure(
    process: subprocess.Popen[bytes],
    reader: threading.Thread | None,
    streams: list[BinaryIO],
    cause: BaseException,
) -> None:
    stopped = _stop_control_process(process)
    readers_ok = join_readers([reader] if reader else [], streams)
    if not stopped or not readers_ok:
        raise ContainerSandboxError("Container runtime control cleanup failed") from cause
    if isinstance(cause, ContainerSandboxError):
        raise cause
    raise ContainerSandboxError(f"Container runtime control failed: {cause}") from cause


def _stop_control_process(process: subprocess.Popen[bytes]) -> bool:
    if process.returncode is not None:
        return True
    try:
        process.kill()
        process.wait(timeout=5)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _wait_control_process(
    process: subprocess.Popen[bytes], timeout: int
) -> tuple[bool, bool]:
    try:
        process.wait(timeout=timeout)
        return False, True
    except subprocess.TimeoutExpired:
        return True, _stop_control_process(process)


def run_control(
    runtime: str,
    args: list[str],
    env: dict[str, str],
    *,
    capture: bool = False,
) -> ControlResult:
    stdout_target = subprocess.PIPE if capture else subprocess.DEVNULL
    try:
        process = subprocess.Popen(
            [runtime, *args], shell=False, stdin=subprocess.DEVNULL,
            stdout=stdout_target, stderr=subprocess.DEVNULL, env=env,
            # Keep control CLIs in the supervised tool process group. The
            # independent container watchdog has its own launch boundary.
            close_fds=True, start_new_session=False,
        )
    except OSError as exc:
        raise ContainerSandboxError(f"Failed to start container runtime: {exc}") from exc
    reader: threading.Thread | None = None
    captured = CapturedStream()
    streams: list[BinaryIO] = []
    try:
        reader, streams = _start_capture(process, capture, captured)
        timed_out, stopped = _wait_control_process(
            process, ENGINE_CLEANUP_TIMEOUT
        )
        readers_ok = join_readers([reader] if reader else [], streams)
    except BaseException as exc:
        _raise_control_failure(process, reader, streams, exc)
    if timed_out or not stopped or not readers_ok:
        failure = ContainerSandboxError("Container runtime control command did not terminate")
        _raise_control_failure(process, reader, streams, failure)
    if captured.error is not None:
        failure = ContainerSandboxError("Container runtime control capture failed")
        _raise_control_failure(process, reader, streams, failure)
    return ControlResult(process.returncode, bytes(captured.data), captured.total_bytes)


def container_name_absent(
    runtime: str, name: str, env: dict[str, str]
) -> bool:
    """Confirm absence using a successful, bounded container inventory."""
    result = run_control(
        runtime,
        ["ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"],
        env,
        capture=True,
    )
    if result.returncode != 0 or result.stdout_total > MAX_STREAM_BYTES:
        return False
    try:
        names = result.stdout.decode("ascii").splitlines()
    except UnicodeDecodeError:
        return False
    return name not in names
