"""Bounded Bash execution on the trusted host below the tool-child boundary."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field

from .config import Config
from .file_identity import ToolInputError, bounded_integer
from .safety import SandboxViolation, check_command

MAX_TOOL_OUTPUT = 50_000
MAX_BASH_TIMEOUT = 600
DEFAULT_BASH_TIMEOUT = 60
MAX_STREAM_BYTES = 25_000
PROCESS_STOP_SECONDS = 5
_HOST_ENVIRONMENT = (
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
)


class TrustedHostError(RuntimeError):
    """The supervised trusted-host command could not be completed safely."""


class _WindowsBasicLimits(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _WindowsIoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "read_operation_count", "write_operation_count", "other_operation_count",
        "read_transfer_count", "write_transfer_count", "other_transfer_count",
    )]


class _WindowsExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _WindowsBasicLimits),
        ("io_info", _WindowsIoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


@dataclass
class _WindowsJob:
    """Own a Windows process job until every host descendant must be gone."""

    handle: int
    close_handle: object

    def close(self) -> bool:
        if not self.handle:
            return True
        handle, self.handle = self.handle, 0
        return bool(self.close_handle(handle))


@dataclass
class _CapturedStream:
    data: bytearray = field(default_factory=bytearray)
    total_bytes: int = 0
    error: BaseException | None = None

    def append(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        remaining = MAX_STREAM_BYTES - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])


@dataclass(frozen=True)
class _BashResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_total: int
    stderr_total: int
    timed_out: bool


def _parse_timeout(args: dict) -> int:
    return bounded_integer(
        args.get("timeout", DEFAULT_BASH_TIMEOUT),
        "timeout", minimum=1, maximum=MAX_BASH_TIMEOUT,
    )


def _decode_stream(data: bytes, total_bytes: int) -> str:
    text = data.decode("utf-8", errors="replace")
    if total_bytes > len(data):
        text += f"\n... [truncated, captured {len(data)} of {total_bytes} bytes]"
    return text


def _format_result(result: _BashResult) -> str:
    stdout = _decode_stream(result.stdout, result.stdout_total)
    stderr = _decode_stream(result.stderr, result.stderr_total)
    combined = f"[exit {result.returncode}]\n--- stdout ---\n{stdout}"
    if stderr:
        combined += f"\n--- stderr ---\n{stderr}"
    if len(combined) > MAX_TOOL_OUTPUT:
        combined = combined[:MAX_TOOL_OUTPUT] + "\n... [tool output truncated]"
    return combined


def _host_environment(workspace) -> dict[str, str]:
    """Pass execution essentials only; never inherit the real user home."""
    environment = {
        name: os.environ[name] for name in _HOST_ENVIRONMENT if name in os.environ
    }
    environment.setdefault("PATH", os.defpath)
    home = str(workspace)
    environment["HOME"] = home
    if os.name == "nt":
        drive, path = os.path.splitdrive(home)
        environment["USERPROFILE"] = home
        environment["HOMEDRIVE"] = drive
        environment["HOMEPATH"] = path or os.sep
    return environment


def _host_argv(command: str) -> list[str]:
    if os.name == "nt":
        interpreter = os.environ.get("COMSPEC") or "cmd.exe"
        return [interpreter, "/d", "/s", "/c", command]
    return ["/bin/sh", "-c", command]


def _drain(stream, capture: _CapturedStream) -> None:
    try:
        while chunk := stream.read(8192):
            capture.append(chunk)
    except BaseException as exc:
        capture.error = exc


def _stop_host_process(process: subprocess.Popen[bytes]) -> bool:
    try:
        if os.name == "posix":
            # The shell can exit before a background descendant.  Its process
            # group remains killable until that final descendant has gone.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            _stop_windows_process_tree(process)
        process.wait(timeout=PROCESS_STOP_SECONDS)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _stop_windows_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Use the native tree terminator before falling back to the shell process."""
    try:
        subprocess.run(
            ["taskkill", "/pid", str(process.pid), "/t", "/f"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=PROCESS_STOP_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    if process.poll() is None:
        process.kill()


def _join_readers(readers: list[threading.Thread], streams: list) -> bool:
    ok = True
    for reader in readers:
        reader.join(PROCESS_STOP_SECONDS)
        ok = ok and not reader.is_alive()
    for stream in streams:
        try:
            stream.close()
        except OSError:
            ok = False
    return ok


def _host_options() -> dict[str, object]:
    options: dict[str, object] = {"start_new_session": os.name == "posix"}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return options


def _create_windows_job(process: subprocess.Popen[bytes]) -> _WindowsJob | None:
    """Bind the host shell tree to KILL_ON_JOB_CLOSE on Windows."""
    if os.name != "nt":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateJobObjectW
    create.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    create.restype = wintypes.HANDLE
    set_information = kernel.SetInformationJobObject
    set_information.argtypes = (
        wintypes.HANDLE, wintypes.INT, ctypes.c_void_p, wintypes.DWORD,
    )
    set_information.restype = wintypes.BOOL
    assign = kernel.AssignProcessToJobObject
    assign.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    assign.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    handle = create(None, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    limits = _WindowsExtendedLimits()
    limits.basic_limit_information.limit_flags = 0x00002000
    try:
        if not set_information(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        if not assign(handle, process._handle):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    except BaseException:
        close(handle)
        raise
    return _WindowsJob(handle, close)


def _start_host_command(
    command: str, config: Config, stdout: _CapturedStream, stderr: _CapturedStream,
) -> tuple[subprocess.Popen[bytes], list[threading.Thread], list, _WindowsJob | None]:
    process = subprocess.Popen(
        _host_argv(command), shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=config.workspace,
        env=_host_environment(config.workspace), close_fds=True, **_host_options(),
    )
    try:
        job = _create_windows_job(process)
    except BaseException:
        _stop_host_process(process)
        raise
    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=_drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    return process, readers, [process.stdout, process.stderr], job


def _trusted_host_result(
    process: subprocess.Popen[bytes] | None,
    stdout: _CapturedStream,
    stderr: _CapturedStream,
    timed_out: bool,
    cause: BaseException | None,
    stopped: bool,
    readers_ok: bool,
) -> _BashResult:
    if cause is not None:
        raise TrustedHostError(f"trusted host command failed: {cause}") from cause
    if not stopped or not readers_ok:
        raise TrustedHostError("trusted host command cleanup failed")
    if stdout.error is not None or stderr.error is not None:
        raise TrustedHostError("trusted host output capture failed")
    if process is None or process.returncode is None:
        raise TrustedHostError("trusted host command did not return a status")
    return _BashResult(
        process.returncode, bytes(stdout.data), bytes(stderr.data),
        stdout.total_bytes, stderr.total_bytes, timed_out,
    )


def _run_on_trusted_host(command: str, config: Config, timeout: int) -> _BashResult:
    """Run below tool_child so its cancellation/deadline boundary owns cleanup."""
    process: subprocess.Popen[bytes] | None = None
    job: _WindowsJob | None = None
    readers: list[threading.Thread] = []
    streams: list = []
    stdout = _CapturedStream()
    stderr = _CapturedStream()
    timed_out = False
    cause: BaseException | None = None
    try:
        process, readers, streams, job = _start_host_command(command, config, stdout, stderr)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    except BaseException as exc:
        cause = exc
    finally:
        stopped = process is None or _stop_host_process(process)
        if job is not None:
            stopped = job.close() and stopped
        readers_ok = _join_readers(readers, streams)
    return _trusted_host_result(
        process, stdout, stderr, timed_out, cause, stopped, readers_ok,
    )


def _command_request(args: dict, max_timeout: int | None) -> tuple[str, int] | str:
    command = args.get("command", "")
    if not isinstance(command, str) or not command.strip():
        return "ERROR: missing required 'command' argument"
    try:
        check_command(command)
        timeout = _parse_timeout(args)
    except (SandboxViolation, ToolInputError) as exc:
        return f"ERROR: {exc}"
    if max_timeout is not None:
        timeout = min(timeout, max(1, max_timeout))
    return command, timeout


def execute_bash(
    args: dict,
    config: Config,
    lease_fd: int | None = None,
    max_timeout: int | None = None,
) -> str:
    del lease_fd
    request = _command_request(args, max_timeout)
    if isinstance(request, str):
        return request
    command, timeout = request
    try:
        result = _run_on_trusted_host(command, config, timeout)
    except (TrustedHostError, RuntimeError, TypeError, ValueError) as exc:
        return f"ERROR: trusted host unavailable: {exc}"
    if result.timed_out:
        return f"ERROR: command timed out after {timeout}s; trusted-host process was terminated"
    try:
        return _format_result(result)
    except Exception as exc:
        return f"ERROR: failed to format Bash result: {exc}"
