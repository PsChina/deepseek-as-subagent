"""MCP server entrypoint.

Exposes two tools to MCP-capable hosts such as Claude Code and Codex CLI:
  - ping: health check
  - delegate_to_deepseek: run DeepSeek as a real sub-agent with its own tool loop

Environment variables:
  - DEEPSEEK_MODE=off: disable delegation for this process
  - DEEPSEEK_API_KEY: override api_key from config.json
  - DEEPSEEK_WORKSPACE: override workspace from config.json
"""
from __future__ import annotations

import asyncio
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
from .agent_loop import AgentLoopError, run_agent
from .config import Config

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
DeepSeek is available as a delegated coding sub-agent through the
`delegate_to_deepseek` tool. Treat delegation as an execution optimization,
not as a replacement for the host agent's judgment.

Delegate when the work is a self-contained logical unit with clear success
criteria and is mostly implementation, batch editing, test generation,
mechanical refactoring, data/code transformation, repetitive repository work,
or other execution-heavy work that DeepSeek can finish inside the workspace.

Keep the work in the host agent when it depends on conversation-only context,
architectural trade-offs, ambiguous product decisions, root-cause analysis,
security-sensitive judgment, or a very small edit where delegation overhead is
larger than the task.

Important: decide whether to delegate before reading large amounts of source.
If the host reads all relevant files first and then delegates, both agents pay
the repository-reading cost. Lightweight discovery such as directory listing,
file counting, or locating candidate paths is fine before the decision.

When calling `delegate_to_deepseek`, pass a complete task description with file
paths, constraints, and success criteria. DeepSeek does not see the host's chat
history or project instruction files unless that information is explicitly put
in `task` or `context`.

After delegation, verify the result. Read a representative sample, run relevant
tests/checks, and take over locally if DeepSeek fails twice or produces a result
that requires substantial judgment to repair.
""".strip()


# MCP protocol supports server-level instructions during initialization. Modern
# Codex clients consume these instructions, so Codex can learn delegation policy
# immediately after MCP registration instead of requiring a pasted AGENTS.md
# block. Older clients simply ignore this field and still work with the tools.
mcp = FastMCP("deepseek-mcp", instructions=_HOST_INSTRUCTIONS)


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


@mcp.tool()
def delegate_to_deepseek(task: str, context: str = "") -> str:
    """Delegate a focused task to DeepSeek as a real sub-agent.

    DeepSeek runs its own agent loop with Read/Write/Edit/Bash/Glob/Grep/
    NotebookEdit tools inside the configured workspace. Prefer this for
    self-contained execution-heavy work such as batch changes, mechanical
    refactors, test generation, scripted transformations, and other tasks with
    clear success criteria.

    Avoid delegating architecture decisions, ambiguous root-cause analysis,
    security-sensitive judgment, or tiny edits where orchestration overhead is
    larger than the work.

    Args:
        task: Complete description of what DeepSeek should accomplish,
              including relevant file paths, boundaries, and success criteria.
        context: Optional project conventions or external facts DeepSeek needs.
                 DeepSeek cannot see the host conversation or AGENTS.md/
                 CLAUDE.md unless those details are copied here.

    Returns:
        DeepSeek's final message plus turns/tool-calls/token/duration metadata.
        The host agent should verify representative output and relevant tests
        before declaring the task complete.
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
        result = run_agent(full_task, config)
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

    return (
        f"{result['final_message']}\n\n"
        f"---\n"
        f"[deepseek-mcp] {result['turns_used']} turns, "
        f"{result['tool_calls']} tool calls, "
        f"{result['tokens']['total']} tokens, "
        f"{result['duration_seconds']}s"
    )


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
