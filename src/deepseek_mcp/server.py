"""MCP server 入口。

暴露两个工具给 Claude Code:
  - ping: 健康检查
  - delegate_to_deepseek: 真正的 sub-agent，把任务外包给 DeepSeek 跑完整 agent loop

环境变量:
  - DEEPSEEK_MODE=off: delegate 工具会立即返回 disabled 提示，Claude 不会再调
  - DEEPSEEK_API_KEY: 覆盖配置文件中的 api_key
  - DEEPSEEK_WORKSPACE: 覆盖配置文件中的 workspace
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import __version__
from .agent_loop import AgentLoopError, run_agent
from .config import Config

# 日志写到 ~/.deepseek-mcp/server.log（不污染 stdout，stdout 是 MCP 协议通道）
_LOG_DIR = Path.home() / ".deepseek-mcp"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(_LOG_DIR / "server.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


mcp = FastMCP("deepseek-mcp")


@mcp.tool()
def ping() -> str:
    """Health check. Confirms the deepseek-mcp server is alive.

    Use this before delegate_to_deepseek if you're not sure whether DeepSeek is configured.
    Returns version, mode (auto/off), and whether config is loadable.
    """
    mode = os.getenv("DEEPSEEK_MODE", "auto")
    try:
        cfg = Config.load()
        config_status = f"workspace={cfg.workspace} (sandbox), model={cfg.model}"
    except Exception as e:
        config_status = f"NOT_CONFIGURED ({e})"
    return f"pong from deepseek-mcp v{__version__} | mode={mode} | {config_status}"


@mcp.tool()
def delegate_to_deepseek(task: str, context: str = "") -> str:
    """Delegate a focused task to DeepSeek as a real sub-agent.

    DeepSeek runs its own agent loop with Read/Write/Edit/Bash/Glob/Grep tools
    inside the configured workspace. Use this for batch / repetitive / mechanical
    tasks where you want to save main-conversation tokens and let DeepSeek do the
    heavy lifting end-to-end.

    Good fits:
      - Extract i18n keys from N files into JSON
      - Translate large chunks of text
      - Scan logs for patterns
      - Bulk refactors with a clear pattern
      - One-off ETL scripts

    Bad fits (do it yourself instead):
      - Architectural design / cross-file judgment
      - Bug root-cause analysis
      - Tasks requiring project-specific idioms from CLAUDE.md / dev-cases

    Args:
        task: Clear description of what DeepSeek should accomplish, including
              success criteria and file paths involved.
        context: Optional additional context — project conventions, related
                 files DeepSeek should consider, output format requirements.
                 Include this when project-specific knowledge matters.

    Returns:
        A summary of what DeepSeek did, including files affected, turns used,
        tokens consumed, and any issues. Always verify the result by reading
        a sample of the affected files before declaring success to the user.
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

    logger.info("delegate_to_deepseek invoked. Task length=%d, context length=%d", len(task), len(context))

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

    # 用量记录（人类可读追加到 usage.log）
    try:
        with open(_LOG_DIR / "usage.log", "a") as f:
            f.write(
                f"{result['duration_seconds']:.1f}s  "
                f"turns={result['turns_used']:>2}  "
                f"tools={result['tool_calls']:>2}  "
                f"tokens={result['tokens']['total']:>6}  "
                f"task={task[:60]!r}\n"
            )
    except Exception:
        pass  # 日志失败不影响主流程

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
    logger.info("deepseek-mcp v%s starting (mode=%s)", __version__, os.getenv("DEEPSEEK_MODE", "auto"))
    try:
        mcp.run()
    except Exception as e:
        logger.exception("MCP server crashed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
