"""Bounded Bash execution metadata; never retain commands or output text."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class BashOutcome:
    """Track attempted calls separately from commands that reached the shell."""

    attempts: int = 0
    executed_calls: int = 0
    last_status: str | None = None
    failures: list[dict] = field(default_factory=list)

    def payload(self) -> dict:
        return {
            "calls": self.attempts,
            "executed_calls": self.executed_calls,
            "failure_count": len(self.failures),
            "last_status": self.last_status,
            "failures": list(self.failures),
        }


def record_bash_result(state: BashOutcome, output: str, turn: int) -> None:
    """Track tool-level failures while allowing subsequent successful repairs."""
    state.attempts += 1
    match = re.fullmatch(r"\[exit (-?\d+)\]", output.partition("\n")[0])
    if match is not None:
        state.executed_calls += 1
        exit_code = int(match.group(1))
        status = "success" if exit_code == 0 else "nonzero_exit"
    else:
        exit_code = None
        status = "tool_error" if output.startswith("ERROR:") else "unknown"
    state.last_status = status
    if status != "success":
        state.failures.append({
            "turn": turn + 1,
            "status": status,
            "exit_code": exit_code,
        })
