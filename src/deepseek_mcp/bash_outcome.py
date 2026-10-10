"""Bounded Bash execution metadata; never retain commands or output text."""
from __future__ import annotations

import re
from typing import Protocol


class BashOutcomeSink(Protocol):
    bash_calls: int
    last_bash_status: str | None
    bash_failures: list[dict]


def record_bash_result(state: BashOutcomeSink, output: str, turn: int) -> None:
    """Track tool-level failures while allowing subsequent successful repairs."""
    state.bash_calls += 1
    match = re.fullmatch(r"\[exit (-?\d+)\]", output.partition("\n")[0])
    if match is not None:
        exit_code = int(match.group(1))
        status = "success" if exit_code == 0 else "nonzero_exit"
    else:
        exit_code = None
        status = "tool_error" if output.startswith("ERROR:") else "unknown"
    state.last_bash_status = status
    if status != "success":
        state.bash_failures.append({
            "turn": turn + 1,
            "status": status,
            "exit_code": exit_code,
        })
