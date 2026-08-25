"""DeepSeek agent loop。

接收一个任务描述 → 让 DeepSeek 自己跑 Read/Edit/Bash 等工具循环 → 返回 final message。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable

from openai import APIConnectionError, APIError, OpenAI, RateLimitError

from .config import Config
from .tools import build_tool_schemas, execute_tool

logger = logging.getLogger(__name__)

# 单次 API 调用最多重试次数（不含首次）。只对网络 / 限流类瞬态错误生效。
API_RETRY_ATTEMPTS = 2
API_RETRY_BACKOFF_SECONDS = 2.0

# 工具参数日志：含敏感内容的字段（避免写到 server.log）
SENSITIVE_TOOL_ARG_KEYS = {"content", "new_string"}


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


class AgentLoopCancelled(AgentLoopError):
    """Agent loop was cancelled by the parent agent at a safe point."""


def run_agent(
    task: str,
    config: Config,
    *,
    control_poll: Callable[[], list[str]] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """跑完整 agent loop。

    Optional control hooks are checked only at safe points between model/tool
    operations. They do not interrupt an in-flight API request or a currently
    executing tool call.

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
        _check_cancel(cancel_check)
        _append_control_messages(messages, control_poll)

        response = _call_with_retry(client, config, messages, tools, turn)

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

        _check_cancel(cancel_check)

        # 没有 tool_calls 通常说明 DeepSeek 决定结束。但如果 parent 在这次
        # in-flight generation 期间发来了 steering message，先消费消息并继续一轮。
        if not msg.tool_calls:
            if _append_control_messages(messages, control_poll):
                continue
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

        # 依次执行 tool calls。cancel 可以在任意两个 tool call 之间生效；
        # steering message 则等这一组 tool responses 完整写回后，在下一轮消费，
        # 避免破坏 OpenAI tool-call / tool-response 配对协议。
        for tc in msg.tool_calls:
            _check_cancel(cancel_check)
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
                    _redact_args_for_log(args),
                )
                result = execute_tool(tool_name, args, config.workspace)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
            )
            _check_cancel(cancel_check)

    # 跑到 max_turns 没收敛 —— 只展示最后一条 assistant content，不夹带完整 tool_calls blob
    last_text = ""
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("content"):
            last_text = str(m["content"])[:500]
            break
    raise AgentLoopError(
        f"Agent loop exceeded max_turns ({config.max_turns}). "
        f"Last assistant text: {last_text or '(none)'}"
    )


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is None:
        return
    try:
        cancelled = cancel_check()
    except Exception as e:
        raise AgentLoopError(f"cancel channel failed: {e}") from e
    if cancelled:
        raise AgentLoopCancelled("DeepSeek job cancelled by parent agent")


def _append_control_messages(
    messages: list[dict],
    control_poll: Callable[[], list[str]] | None,
) -> int:
    if control_poll is None:
        return 0
    try:
        updates = [m.strip() for m in control_poll() if m and m.strip()]
    except Exception as e:
        raise AgentLoopError(f"control channel failed: {e}") from e
    if not updates:
        return 0

    body = "\n\n".join(f"Update {i + 1}:\n{text}" for i, text in enumerate(updates))
    messages.append(
        {
            "role": "user",
            "content": (
                "# Parent agent steering update\n"
                "The parent agent sent the following instructions while you were working. "
                "Apply the newest instructions from this point forward; they override earlier "
                "task details where they conflict.\n\n"
                f"{body}"
            ),
        }
    )
    logger.info("Applied %d parent steering message(s)", len(updates))
    return len(updates)


def _call_with_retry(client, config, messages, tools, turn):
    """带瞬态错误重试的单次 API 调用。

    只对 network / rate-limit / 5xx 这类瞬态错误重试；4xx 等永久错误直接抛。
    """
    last_exc = None
    for attempt in range(1 + API_RETRY_ATTEMPTS):
        try:
            return client.chat.completions.create(
                model=config.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
        except (APIConnectionError, RateLimitError) as e:
            last_exc = e
            wait = API_RETRY_BACKOFF_SECONDS * (attempt + 1)
            logger.warning(
                "Turn %d API transient error (attempt %d/%d): %s — retry in %.1fs",
                turn, attempt + 1, 1 + API_RETRY_ATTEMPTS, e, wait,
            )
            time.sleep(wait)
        except APIError as e:
            # 5xx 也重试，4xx 不重试
            status = getattr(e, "status_code", None)
            if status and 500 <= status < 600:
                last_exc = e
                wait = API_RETRY_BACKOFF_SECONDS * (attempt + 1)
                logger.warning(
                    "Turn %d API 5xx (attempt %d/%d): %s — retry in %.1fs",
                    turn, attempt + 1, 1 + API_RETRY_ATTEMPTS, e, wait,
                )
                time.sleep(wait)
                continue
            raise AgentLoopError(f"DeepSeek API error on turn {turn}: {e}") from e
        except Exception as e:
            raise AgentLoopError(f"DeepSeek API error on turn {turn}: {e}") from e
    raise AgentLoopError(
        f"DeepSeek API unreachable after {1 + API_RETRY_ATTEMPTS} attempts on turn {turn}: {last_exc}"
    ) from last_exc


def _redact_args_for_log(args: dict) -> dict:
    """工具参数写日志前脱敏 —— content/new_string 不能进 server.log（可能含 secrets）。"""
    redacted = {}
    for k, v in args.items():
        if k in SENSITIVE_TOOL_ARG_KEYS and isinstance(v, str):
            redacted[k] = f"<{len(v)} chars, redacted>"
        elif isinstance(v, str) and len(v) >= 100:
            redacted[k] = f"<{len(v)} chars>"
        else:
            redacted[k] = v
    return redacted
