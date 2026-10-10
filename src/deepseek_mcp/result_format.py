"""Compact host-visible delegation summaries without sensitive tool output."""
from __future__ import annotations

import json


def format_sync_result(result: dict) -> str:
    bash = result.get("bash", {})
    summary = (
        f"{result['final_message']}\n\n"
        f"---\n"
        f"[deepseek-mcp] {result['turns_used']} turns, "
        f"{result['tool_calls']} tool calls, "
        f"{result['tokens']['total']} tokens, "
        f"{result['duration_seconds']}s"
    )
    if bash.get("calls"):
        summary += "\n[deepseek-mcp bash] " + json.dumps(
            bash, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
    return summary
