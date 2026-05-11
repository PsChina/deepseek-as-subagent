"""DeepSeek agent loop。

接收一个任务描述 → 让 DeepSeek 自己跑 Read/Edit/Bash 等工具循环 → 返回 final message。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from openai import OpenAI

from .config import Config
from .tools import build_tool_schemas, execute_tool

logger = logging.getLogger(__name__)


SYSTEM_PROMPT_TEMPLATE = """You are DeepSeek working as a sub-agent for Claude.

You're given a focused task to complete autonomously within a workspace.
You have local tools: {tools}

Rules:
1. Stay strictly within the workspace: {workspace}
2. Read before editing. Don't guess file contents.
3. For batch tasks (translating, extracting, refactoring many files), iterate file-by-file.
4. When done, return a final message summarizing:
   - What you did (file paths affected)
   - Any issues / files you couldn't process
   - A brief summary the parent (Claude) can use without re-reading everything
5. Don't ask clarifying questions back to the parent. Make reasonable assumptions
   and document them in your final message.
6. If a tool returns "ERROR: ...", read the error and decide: retry with fixed input,
   skip the file, or report and stop. Don't blindly loop on the same error.
"""


class AgentLoopError(Exception):
    """Agent loop failed (max turns exceeded, API error, etc)."""


def run_agent(task: str, config: Config) -> dict:
    """跑完整 agent loop。

    返回 dict:
      - final_message: str (DeepSeek 给的最终答复)
      - turns_used: int
      - tokens: {prompt, completion, total}
      - tool_calls: int
      - duration_seconds: float
    """
    client = OpenAI(api_key=config.api_key, base_url=config.base_url)
    tools = build_tool_schemas(config.allowed_tools)

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        tools=", ".join(config.allowed_tools),
        workspace=config.workspace,
    )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    total_prompt_tokens = 0
    total_completion_tokens = 0
    tool_call_count = 0
    started = time.time()

    for turn in range(config.max_turns):
        try:
            response = client.chat.completions.create(
                model=config.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
        except Exception as e:
            raise AgentLoopError(f"DeepSeek API error on turn {turn}: {e}") from e

        usage = response.usage
        if usage:
            total_prompt_tokens += usage.prompt_tokens
            total_completion_tokens += usage.completion_tokens

        msg = response.choices[0].message

        # 用 raw dict 保留所有字段，包括 DeepSeek v4-pro thinking mode 的 reasoning_content
        # —— 它要求下一轮必须把 reasoning_content 也回传，否则 400 报错
        raw = response.model_dump(exclude_none=True)
        msg_dict = raw["choices"][0]["message"]
        messages.append(msg_dict)

        # 没有 tool_calls 说明 DeepSeek 决定结束
        if not msg.tool_calls:
            return {
                "final_message": msg.content or "(empty response)",
                "turns_used": turn + 1,
                "tokens": {
                    "prompt": total_prompt_tokens,
                    "completion": total_completion_tokens,
                    "total": total_prompt_tokens + total_completion_tokens,
                },
                "tool_calls": tool_call_count,
                "duration_seconds": round(time.time() - started, 2),
            }

        # 依次执行 tool calls
        for tc in msg.tool_calls:
            tool_call_count += 1
            tool_name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError as e:
                result = f"ERROR: invalid JSON in tool arguments: {e}"
            else:
                logger.info(
                    "Turn %d tool_call: %s(%s)",
                    turn,
                    tool_name,
                    {k: v if not isinstance(v, str) or len(v) < 100 else f"<{len(v)} chars>" for k, v in args.items()},
                )
                result = execute_tool(tool_name, args, config.workspace)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
            )

    # 跑到 max_turns 没收敛
    raise AgentLoopError(
        f"Agent loop exceeded max_turns ({config.max_turns}). "
        f"Last assistant message: {messages[-1] if messages else 'none'}"
    )
