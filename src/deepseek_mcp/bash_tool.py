"""Bash tool adapter backed exclusively by the container sandbox."""
from __future__ import annotations

from .config import Config
from .container_sandbox import ContainerResult, ContainerSandboxError, run_in_container
from .file_identity import ToolInputError, bounded_integer
from .safety import SandboxViolation, check_command

MAX_TOOL_OUTPUT = 50_000
MAX_BASH_TIMEOUT = 600
DEFAULT_BASH_TIMEOUT = 60


def _parse_timeout(args: dict) -> int:
    return bounded_integer(
        args.get("timeout", DEFAULT_BASH_TIMEOUT),
        "timeout",
        minimum=1,
        maximum=MAX_BASH_TIMEOUT,
    )


def _decode_stream(data: bytes, total_bytes: int) -> str:
    text = data.decode("utf-8", errors="replace")
    if total_bytes > len(data):
        text += f"\n... [truncated, captured {len(data)} of {total_bytes} bytes]"
    return text


def _format_result(result: ContainerResult) -> str:
    stdout = _decode_stream(result.stdout, result.stdout_total)
    stderr = _decode_stream(result.stderr, result.stderr_total)
    combined = f"[exit {result.returncode}]\n--- stdout ---\n{stdout}"
    if stderr:
        combined += f"\n--- stderr ---\n{stderr}"
    if len(combined) > MAX_TOOL_OUTPUT:
        combined = combined[:MAX_TOOL_OUTPUT] + "\n... [tool output truncated]"
    return combined


def execute_bash(
    args: dict,
    config: Config,
    lease_fd: int | None = None,
    max_timeout: int | None = None,
) -> str:
    command = args.get("command", "")
    if not isinstance(command, str) or not command.strip():
        return "ERROR: missing required 'command' argument"
    try:
        check_command(command)
    except SandboxViolation as exc:
        return f"ERROR: {exc}"
    try:
        timeout = _parse_timeout(args)
    except ToolInputError as exc:
        return f"ERROR: {exc}"
    if max_timeout is not None:
        timeout = min(timeout, max(1, max_timeout))
    try:
        result = run_in_container(command, config, timeout, lease_fd=lease_fd)
    except (ContainerSandboxError, RuntimeError, TypeError, ValueError) as exc:
        return f"ERROR: container sandbox unavailable: {exc}"
    if result.timed_out:
        return f"ERROR: command timed out after {timeout}s; container was force-removed"
    try:
        return _format_result(result)
    except Exception as exc:
        # run_in_container returns only after removal is confirmed and its
        # watchdog is reaped, so formatting cannot orphan a container.
        return f"ERROR: failed to format container result: {exc}"
