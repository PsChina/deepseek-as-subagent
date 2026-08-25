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
import sys
from pathlib import Path

# Windows defaults to ProactorEventLoop, which can deadlock with stdio subprocesses.
# Switch before importing/starting FastMCP.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from mcp.server.fastmcp import FastMCP

from . import __version__
from .agent_loop import AgentLoopError
from .config import Config
from .job_manager import DeepSeekJobManager, JobError

_LOG_DIR = Path.home() / ".deepseek-mcp"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_SERVER_LOG = _LOG_DIR / "server.log"
_USAGE_LOG = _LOG_DIR / "usage.log"

try:
    os.chmod(_LOG_DIR, 0o700)
except OSError:
    pass

for _p in (_SERVER_LOG, _USAGE_LOG):
    if not _p.exists():
        try:
            _p.touch(mode=0o600)
        except OSError:
            pass
    try:
        os.chmod(_p, 0o600)
    except OSError:
        pass

logging.basicConfig(
    filename=str(_SERVER_LOG),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

_HOST_INSTRUCTIONS = """
Use `delegate_to_deepseek` for self-contained, execution-heavy work. Decide
whether to delegate BEFORE reading repository source; lightweight file
listing/counting is fine. Keep architecture, ambiguous root-cause analysis,
security-sensitive judgment, and tiny edits in the host agent. DeepSeek cannot
see host chat, AGENTS.md, or CLAUDE.md, so pass all required context explicitly.
Always verify delegated output and relevant tests before declaring success.

For work that may need mid-flight steering or cancellation, prefer the
background-job API: `start_deepseek` returns a job_id immediately; then use
`send_deepseek_message`, `get_deepseek_status`, `cancel_deepseek`, and
`get_deepseek_result`. Steering/cancellation takes effect at safe points between
model/tool operations, not by interrupting an in-flight model request or tool.
V1 intentionally permits only one DeepSeek execution at a time across both
synchronous and background APIs.

Treat delegation as an execution optimization, not as a replacement for the
host agent's judgment. Good delegation units have clear success criteria and
are mostly implementation, batch editing, test generation, mechanical
refactoring, data/code transformation, repetitive repository work, or other
execution-heavy work that DeepSeek can finish inside the workspace.

If the host reads all relevant files first and then delegates, both agents pay
the repository-reading cost. Lightweight discovery such as directory listing,
file counting, or locating candidate paths is fine before the decision.

When delegating, pass a complete task description with file paths, constraints,
and success criteria. DeepSeek does not see the host's chat history or project
instruction files unless that information is explicitly put in task/context.

After delegation, verify the result. Read a representative sample, run relevant
tests/checks, and take over locally if DeepSeek fails twice or produces a result
that requires substantial judgment to repair.
""".strip()


# MCP protocol supports server-level instructions during initialization. Codex
# reads them as server-wide guidance. Keep the first ~512 characters
# self-contained because clients may surface/truncate instructions differently.
# AGENTS.md remains an optional stronger/project-specific policy layer.
mcp = FastMCP("deepseek-mcp", instructions=_HOST_INSTRUCTIONS)
job_manager = DeepSeekJobManager()


@mcp.tool()
def ping() -> str:
    """Health check for the deepseek-mcp server.

    Returns version, mode, and whether the DeepSeek configuration is loadable.
    """
    mode = os.getenv("DEEPSEEK_MODE", "auto")
    try:
        cfg = Config.load()
        ws_short = _shorten_path(cfg.workspace)
        config_status = f"workspace={ws_short} (sandbox), model={cfg.model}"
    except Exception as e:
        config_status = f"NOT_CONFIGURED ({e})"
    return f"pong from deepseek-mcp v{__version__} | mode={mode} | {config_status}"


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


def _load_config() -> Config:
    if os.getenv("DEEPSEEK_MODE", "auto") == "off":
        raise JobError("DeepSeek delegation is disabled (DEEPSEEK_MODE=off)")
    try:
        return Config.load()
    except Exception as e:
        raise JobError(f"deepseek-mcp not configured: {e}") from e


@mcp.tool()
def delegate_to_deepseek(task: str, context: str = "") -> str:
    """Delegate a focused task synchronously to DeepSeek as a real sub-agent.

    The MCP call remains open until DeepSeek finishes. Prefer this simple API
    when no mid-flight steering/cancellation is needed. For steerable long work,
    use `start_deepseek` and the background-job tools instead.
    """
    mode = os.getenv("DEEPSEEK_MODE", "auto")
    if mode == "off":
        return (
            "DeepSeek delegation is disabled (DEEPSEEK_MODE=off). "
            "Continue the task yourself in the main conversation."
        )

    try:
        config = Config.load()
    except Exception as e:
        return f"ERROR: deepseek-mcp not configured: {e}"

    full_task = task
    if context:
        full_task = f"{task}\n\n# Additional context\n{context}"

    logger.info(
        "delegate_to_deepseek invoked. Task length=%d, context length=%d",
        len(task),
        len(context),
    )

    try:
        result = job_manager.run_sync(full_task, config)
    except JobError as e:
        return f"ERROR: DeepSeek execution busy: {e}"
    except AgentLoopError as e:
        logger.exception("Agent loop failed")
        return f"ERROR: DeepSeek agent loop failed: {e}"
    except Exception as e:
        logger.exception("Unexpected error during delegation")
        return f"ERROR: unexpected failure: {e}"

    logger.info(
        "delegate_to_deepseek done. turns=%d tool_calls=%d tokens=%d duration=%.2fs",
        result["turns_used"],
        result["tool_calls"],
        result["tokens"]["total"],
        result["duration_seconds"],
    )

    _record_usage(task, result)

    return (
        f"{result['final_message']}\n\n"
        f"---\n"
        f"[deepseek-mcp] {result['turns_used']} turns, "
        f"{result['tool_calls']} tool calls, "
        f"{result['tokens']['total']} tokens, "
        f"{result['duration_seconds']}s"
    )


@mcp.tool()
def start_deepseek(task: str, context: str = "") -> str:
    """Start one steerable DeepSeek background job and return its job_id quickly.

    V1 permits only one DeepSeek execution at a time. The job continues in a
    daemon worker thread after this MCP request returns.
    """
    try:
        payload = job_manager.start(task, context, _load_config())
    except JobError as e:
        return _json({"ok": False, "error": str(e)})
    logger.info("Background DeepSeek job started: %s", payload["job_id"])
    return _json({"ok": True, **payload})


@mcp.tool()
def get_deepseek_status(job_id: str) -> str:
    """Get state for a background DeepSeek job without waiting for completion."""
    try:
        payload = job_manager.status(job_id)
    except JobError as e:
        return _json({"ok": False, "error": str(e)})
    return _json({"ok": True, **payload})


@mcp.tool()
def send_deepseek_message(job_id: str, message: str) -> str:
    """Queue a steering instruction for a running DeepSeek background job.

    The instruction is consumed at the next safe point between model/tool
    operations. It does not interrupt an already in-flight model request/tool.
    """
    try:
        payload = job_manager.send_message(job_id, message)
    except JobError as e:
        return _json({"ok": False, "error": str(e)})
    logger.info("Queued steering message for DeepSeek job %s", job_id)
    return _json({"ok": True, **payload})


@mcp.tool()
def cancel_deepseek(job_id: str) -> str:
    """Request cancellation of a running DeepSeek background job.

    Cancellation is cooperative and becomes final at the next agent-loop safe
    point; a currently executing API request or tool call is not force-killed.
    """
    try:
        payload = job_manager.cancel(job_id)
    except JobError as e:
        return _json({"ok": False, "error": str(e)})
    logger.info("Cancellation requested for DeepSeek job %s", job_id)
    return _json({"ok": True, **payload})


@mcp.tool()
def get_deepseek_result(job_id: str) -> str:
    """Return final result for a background DeepSeek job, or ready=false if running."""
    try:
        payload = job_manager.result(job_id)
        usage_record = job_manager.claim_usage_record(job_id)
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
            task_summary, usage_result = usage_record
            _record_usage(task_summary, usage_result)
    return _json({"ok": True, **payload})


def _record_usage(task: str, result: dict) -> None:
    try:
        if _USAGE_LOG.exists() and _USAGE_LOG.stat().st_size > 10 * 1024 * 1024:
            try:
                _USAGE_LOG.replace(_USAGE_LOG.with_suffix(".log.1"))
            except OSError:
                pass
        with open(_USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(
                f"{result['duration_seconds']:.1f}s  "
                f"turns={result['turns_used']:>2}  "
                f"tools={result['tool_calls']:>2}  "
                f"tokens={result['tokens']['total']:>6}  "
                f"task={task[:60]!r}\n"
            )
        try:
            os.chmod(_USAGE_LOG, 0o600)
        except OSError:
            pass
    except Exception:
        pass


def main() -> None:
    """CLI entrypoint."""
    logger.info(
        "deepseek-mcp v%s starting (mode=%s)",
        __version__,
        os.getenv("DEEPSEEK_MODE", "auto"),
    )
    try:
        mcp.run()
    except Exception as e:
        logger.exception("MCP server crashed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
