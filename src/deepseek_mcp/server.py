"""MCP server entrypoint.

Exposes synchronous delegation plus an optional steerable background-job API to
MCP-capable hosts such as Claude Code and Codex CLI.

Environment variables:
  - DEEPSEEK_MODE=off: disable delegation for this process
  - DEEPSEEK_API_KEY: override api_key from config.json
  - DEEPSEEK_WORKSPACE: override workspace from config.json
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
from pathlib import Path
from threading import Event, Lock

# Windows defaults to ProactorEventLoop, which can deadlock with stdio subprocesses.
# Switch before importing/starting FastMCP.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import __version__
from .agent_loop import AgentLoopCancelled, AgentLoopError
from .provider_retry import MutationOutcomeError, MutationOutcomeCancelled
from .mutation_outcome import mutation_failure_message, records_from_result
from .config import Config
from .execution_profile import (
    CODING_PROFILE, READONLY_PROFILE, ExecutionProfile, configure_delegation,
)
from .host_instructions import HOST_INSTRUCTIONS as _HOST_INSTRUCTIONS
from .job_manager import DeepSeekJobManager, JobBusy, JobError, validate_delegation_input
from .model_selection import ModelChoice, resolve_profile
from .private_logging import PrivateBoundedLogStream
from .process_hardening import disable_core_dumps
from .transaction_recovery import (
    TransactionRecoveryError, acknowledge_with_lease,
    load_recovery_config, query_with_lease,
)

_VALID_MODES = frozenset({"auto", "off"})
def _deepseek_mode() -> str:
    value = os.getenv("DEEPSEEK_MODE", "auto")
    if value not in _VALID_MODES:
        raise JobError("DEEPSEEK_MODE must be exactly 'auto' or 'off'")
    return value

logger = logging.getLogger(__name__)
_PACKAGE_LOGGER = logging.getLogger("deepseek_mcp")
_NULL_LOG_HANDLER = logging.NullHandler()
_PACKAGE_LOGGER.addHandler(_NULL_LOG_HANDLER)
_PACKAGE_LOGGER.propagate = False
_LOG_SETUP_LOCK = Lock()
_LOG_READY = False


def _runtime_paths() -> tuple[Path, Path, Path]:
    log_dir = Path.home() / ".deepseek-mcp"
    return log_dir, log_dir / "server.log", log_dir / "usage.log"


def _prepare_private_log_dir() -> tuple[Path, Path]:
    # Python's Windows os.open cannot securely open a directory with no-follow
    # semantics. Disable persistent logs there instead of silently downgrading
    # the fd-based symlink boundary used on POSIX.
    if os.name == "nt":
        raise OSError("secure persistent logging is unavailable on Windows")
    log_dir, server_log, usage_log = _runtime_paths()
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(log_dir, flags)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o700)
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("DeepSeek log path is not a directory")
        if os.name != "nt" and (
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise OSError("DeepSeek log directory is not private")
    finally:
        os.close(descriptor)
    return server_log, usage_log


def _ensure_runtime_logging() -> None:
    """Configure file logging lazily so importing the MCP server is read-only."""
    global _LOG_READY
    if _LOG_READY:
        return
    with _LOG_SETUP_LOCK:
        if _LOG_READY:
            return
        handler: logging.Handler | None = None
        stream = None
        try:
            server_log, _ = _prepare_private_log_dir()
            stream = PrivateBoundedLogStream(server_log)
            handler = logging.StreamHandler(stream)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            )
            _PACKAGE_LOGGER.addHandler(handler)
            _PACKAGE_LOGGER.setLevel(logging.INFO)
        except Exception:
            if handler is not None:
                _PACKAGE_LOGGER.removeHandler(handler)
                handler.close()
            if stream is not None:
                stream.close()
            if _NULL_LOG_HANDLER not in _PACKAGE_LOGGER.handlers:
                _PACKAGE_LOGGER.addHandler(_NULL_LOG_HANDLER)
        _PACKAGE_LOGGER.propagate = False
        _LOG_READY = True

# MCP protocol supports server-level instructions during initialization. Codex
# reads them as server-wide guidance. Keep the first ~512 characters
# self-contained because clients may surface/truncate instructions differently.
# AGENTS.md remains an optional stronger/project-specific policy layer.
mcp = FastMCP("deepseek-mcp", instructions=_HOST_INSTRUCTIONS)
job_manager = DeepSeekJobManager()

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_AGENT_EXECUTION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)
_READONLY_AGENT_EXECUTION = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
_AGENT_CONTROL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)
_LOCAL_CANCELLATION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)
_RESULT_WITH_BOOKKEEPING = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_RECOVERY_ACK = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False,
)

@mcp.tool(annotations=_READ_ONLY)
def ping() -> str:
    """Health check for the deepseek-mcp server.

    Returns version, mode, and whether the DeepSeek configuration is loadable.
    """
    try:
        mode = _deepseek_mode()
    except JobError as error:
        return f"pong from deepseek-mcp v{__version__} | mode=invalid | NOT_CONFIGURED ({error})"
    try:
        cfg = Config.load()
        ws_short = _shorten_path(cfg.workspace)
        tools = ",".join(cfg.allowed_tools)
        config_status = (
            f"workspace={ws_short} (sandbox), flash={cfg.flash_model}/{cfg.flash_reasoning_effort}, "
            f"pro={cfg.pro_model}/{cfg.pro_reasoning_effort}, tools={tools}"
        )
    except Exception as e:
        config_status = f"NOT_CONFIGURED ({e})"
    return f"pong from deepseek-mcp v{__version__} | mode={mode} | {config_status}"

@mcp.tool(annotations=_RESULT_WITH_BOOKKEEPING)
def get_deepseek_recovery() -> str:
    """List durable, unacknowledged workspace mutation outcomes."""
    try:
        records = query_with_lease(load_recovery_config())
    except (RuntimeError, TransactionRecoveryError) as error:
        return _json({"ok": False, "error": str(error)})
    return _json({"ok": True, "pending": records, "count": len(records)})

@mcp.tool(annotations=_RECOVERY_ACK)
def acknowledge_deepseek_mutations(transaction_ids: list[str]) -> str:
    """Acknowledge exact transaction IDs after the host verifies their files."""
    try:
        removed, pending = acknowledge_with_lease(
            load_recovery_config(), transaction_ids
        )
    except (RuntimeError, TransactionRecoveryError) as error:
        return _json({"ok": False, "error": str(error)})
    return _json({"ok": True, "acknowledged": removed, "pending": pending})


def _shorten_path(p: Path) -> str:
    """Shorten long paths for compact ping output."""
    s = str(p)
    home = str(Path.home())
    if s.startswith(home):
        s = "~" + s[len(home):]
    if len(s) > 60:
        parts = s.split("/")
        if len(parts) > 4:
            s = "/".join(parts[:2] + ["..."] + parts[-2:])
    return s


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _load_config(profile: ExecutionProfile = CODING_PROFILE, model: ModelChoice = "flash") -> Config:
    if _deepseek_mode() == "off":
        raise JobError("DeepSeek delegation is disabled (DEEPSEEK_MODE=off)")
    try:
        config = configure_delegation(Config.load(), profile)
        config.model, config.reasoning_effort = resolve_profile(
            model, flash_model=config.flash_model, pro_model=config.pro_model,
            flash_effort=config.flash_reasoning_effort,
            pro_effort=config.pro_reasoning_effort,
        )
        return config
    except JobError:
        raise
    except Exception as e:
        raise JobError(f"deepseek-mcp not configured: {e}") from e


def _format_sync_result(result: dict) -> str:
    return (
        f"{result['final_message']}\n\n"
        f"---\n"
        f"[deepseek-mcp] {result['turns_used']} turns, "
        f"{result['tool_calls']} tool calls, "
        f"{result['tokens']['total']} tokens, "
        f"{result['duration_seconds']}s"
    )


def _build_full_task(task: str, context: str) -> str:
    validate_delegation_input(task, context)
    return f"{task}\n\n# Additional context\n{context}" if context else task


def _consume_cancelled_worker(worker: asyncio.Task) -> None:
    """Always retrieve a detached thread task outcome after repeated cancellation."""
    try:
        worker.exception()
    except BaseException:
        pass

async def _run_sync_cancellable(full_task: str, config: Config) -> dict:
    cancel_event = Event()
    worker = asyncio.create_task(
        asyncio.to_thread(
            job_manager.run_sync,
            full_task,
            config,
            cancel_event,
        )
    )
    worker.add_done_callback(_consume_cancelled_worker)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel_event.set()
        try:
            result = await asyncio.shield(worker)
        except MutationOutcomeError:
            raise
        except (AgentLoopCancelled, AgentLoopError, JobError):
            pass
        except Exception:
            logger.error("Cancelled DeepSeek worker failed category=internal")
        else:
            records = records_from_result(result)
            if records:
                message = mutation_failure_message(
                    records, "MCP request cancelled after workspace update"
                )
                raise MutationOutcomeCancelled(message, tuple(records)) from None
        raise

async def _delegate(task: str, context: str, profile: ExecutionProfile, model: ModelChoice) -> str:
    try:
        config, full_task = _prepare_sync_request(task, context, profile, model)
    except JobError as e:
        return str(e)
    try:
        result = await _run_sync_cancellable(full_task, config)
    except JobBusy as e:
        return f"ERROR: DeepSeek execution busy: {e}"
    except JobError as e:
        return f"ERROR: DeepSeek execution blocked: {e}"
    except MutationOutcomeError as e:
        logger.error("DeepSeek delegation stopped category=mutation_outcome")
        return f"ERROR: {e}"
    except AgentLoopError:
        logger.error("DeepSeek delegation failed category=agent")
        return "ERROR: DeepSeek agent loop failed"
    except Exception:
        logger.error("DeepSeek delegation failed category=internal")
        return "ERROR: unexpected DeepSeek failure"

    _log_sync_completion(result)
    _record_usage(len(task), result)
    return _format_sync_result(result)

@mcp.tool(annotations=_AGENT_EXECUTION)
async def delegate_to_deepseek(task: str, context: str = "", model: ModelChoice = "flash") -> str:
    """Run a full coding delegation; Flash is default, Pro is for hard tasks."""
    return await _delegate(task, context, CODING_PROFILE, model)

@mcp.tool(annotations=_READONLY_AGENT_EXECUTION)
async def delegate_to_deepseek_readonly(task: str, context: str = "", model: ModelChoice = "flash") -> str:
    """Run pure file analysis; Flash is default, Pro is for hard tasks."""
    return await _delegate(task, context, READONLY_PROFILE, model)


def _prepare_sync_request(task: str, context: str, profile: ExecutionProfile, model: ModelChoice = "flash") -> tuple[Config, str]:
    if _deepseek_mode() == "off":
        raise JobError(
            "DeepSeek delegation is disabled (DEEPSEEK_MODE=off). "
            "Continue the task yourself in the main conversation."
        )
    try:
        config = _load_config(profile, model)
    except JobError:
        raise
    except Exception as error:
        raise JobError(f"ERROR: deepseek-mcp not configured: {error}") from None
    try:
        full_task = _build_full_task(task, context)
    except JobError as error:
        raise JobError(f"ERROR: invalid DeepSeek delegation input: {error}") from None
    logger.info(
        "delegate_to_deepseek invoked. model=%s task length=%d, context length=%d",
        model,
        len(task),
        len(context),
    )
    return config, full_task


def _log_sync_completion(result: dict) -> None:
    logger.info(
        "delegate_to_deepseek done. turns=%d tool_calls=%d tokens=%d duration=%.2fs",
        result["turns_used"],
        result["tool_calls"],
        result["tokens"]["total"],
        result["duration_seconds"],
    )


def _start_delegation(task: str, context: str, profile: ExecutionProfile, model: ModelChoice) -> str:
    try:
        payload = job_manager.start(task, context, _load_config(profile, model))
    except JobError as e:
        return _json({"ok": False, "error": str(e)})
    logger.info("Background DeepSeek job started: %s model=%s", payload["job_id"], model)
    return _json({"ok": True, **payload})

@mcp.tool(annotations=_AGENT_EXECUTION)
def start_deepseek(task: str, context: str = "", model: ModelChoice = "flash") -> str:
    """Start a coding job; Flash is default, Pro is for hard tasks."""
    return _start_delegation(task, context, CODING_PROFILE, model)

@mcp.tool(annotations=_READONLY_AGENT_EXECUTION)
def start_deepseek_readonly(task: str, context: str = "", model: ModelChoice = "flash") -> str:
    """Start a read-only job; Flash is default, Pro is for hard tasks."""
    return _start_delegation(task, context, READONLY_PROFILE, model)

@mcp.tool(annotations=_READ_ONLY)
def get_deepseek_status(job_id: str) -> str:
    """Get state for a background DeepSeek job without waiting for completion."""
    try:
        payload = job_manager.status(job_id)
    except JobError as e:
        return _json({"ok": False, "error": str(e)})
    return _json({"ok": True, **payload})

@mcp.tool(annotations=_AGENT_CONTROL)
def send_deepseek_message(job_id: str, message: str) -> str:
    """Queue a steering instruction for a running DeepSeek background job.

    The instruction is consumed at the next safe point between model/tool
    operations. It does not replace an already in-flight model request/tool.
    """
    try:
        payload = job_manager.send_message(job_id, message)
    except JobError as e:
        return _json({"ok": False, "error": str(e)})
    logger.info("Queued steering message for DeepSeek job %s", job_id)
    return _json({"ok": True, **payload})

@mcp.tool(annotations=_LOCAL_CANCELLATION)
def cancel_deepseek(job_id: str) -> str:
    """Request cancellation of a running DeepSeek background job.

    Cancellation terminates an in-flight provider request or local tool
    subprocess. An accepted request wins before result commit.
    """
    try:
        payload = job_manager.cancel(job_id)
    except JobError as e:
        return _json({"ok": False, "error": str(e)})
    logger.info("Cancellation requested for DeepSeek job %s", job_id)
    return _json({"ok": True, **payload})

@mcp.tool(annotations=_RESULT_WITH_BOOKKEEPING)
def get_deepseek_result(job_id: str) -> str:
    """Return final result for a background DeepSeek job, or ready=false if running."""
    try:
        payload, usage_record = job_manager.result_with_usage_claim(job_id)
    except JobError as e:
        return _json({"ok": False, "error": str(e)})

    result = payload.get("result")
    if payload.get("ready") and payload.get("status") == "completed" and result:
        logger.info(
            "Background DeepSeek job %s completed. turns=%d tools=%d tokens=%d",
            job_id,
            result["turns_used"],
            result["tool_calls"],
            result["tokens"]["total"],
        )
        if usage_record:
            task_length, usage_result = usage_record
            persisted = _record_usage(task_length, usage_result)
            job_manager.finish_usage_record(job_id, persisted)
    return _json({"ok": True, **payload})


def _record_usage(task_length: int, result: dict) -> bool:
    if os.name == "nt":
        return True
    try:
        _, usage_log = _prepare_private_log_dir()
        stream = PrivateBoundedLogStream(usage_log, rotate_on_full=True)
        try:
            record = (
                f"{result['duration_seconds']:.1f}s  "
                f"turns={result['turns_used']:>2}  "
                f"tools={result['tool_calls']:>2}  "
                f"tokens={result['tokens']['total']:>6}  "
                f"task_chars={task_length}\n"
            )
            return stream.write(record) == len(record)
        finally:
            stream.close()
    except Exception:
        return False


def main() -> None:
    """CLI entrypoint."""
    try:
        disable_core_dumps()
        mode = _deepseek_mode()
    except (RuntimeError, JobError):
        raise SystemExit(1) from None
    _ensure_runtime_logging()
    logger.info(
        "deepseek-mcp v%s starting (mode=%s)",
        __version__,
        mode,
    )
    try:
        mcp.run()
    except Exception as e:
        logger.error("MCP server crashed type=%s", type(e).__name__)
        sys.exit(1)


if __name__ == "__main__":
    main()
