"""Subprocess fixture: run one tool and report its supervised child PID."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from deepseek_mcp.config import Config
from deepseek_mcp.execution_lock import acquire_workspace_lease
from deepseek_mcp.resource_budget import MutationBudget
from deepseek_mcp import tool_process

PINNED_IMAGE = "example.invalid/tool@sha256:" + ("d" * 64)


def main() -> None:
    workspace, lock_directory = map(Path, sys.argv[1:3])
    mode = sys.argv[3] if len(sys.argv) > 3 else "bash"
    original_start = tool_process._start_tool

    def reporting_start(timeout: float, lease_fd: int | None):
        process = original_start(timeout, lease_fd)
        print(process.pid, flush=True)
        return process

    tool_process._start_tool = reporting_start
    settings = {"allowed_tools": ["Write"]}
    if mode == "bash":
        settings.update({
            "allowed_tools": ["Read", "Bash"],
            "bash_backend": "container",
            "bash_runtime": "docker",
            "bash_image": PINNED_IMAGE,
        })
    config = Config("unused", workspace, max_run_seconds=60, **settings)
    lease = acquire_workspace_lease(workspace, lock_directory)
    try:
        if mode == "write":
            result = tool_process.execute_in_subprocess(
                config,
                "Write",
                {"path": "crash.txt", "content": "x" * 1_000_000},
                MutationBudget(),
                60,
                lease.fileno(),
                None,
                time.monotonic() + 60,
            )
            if not result.startswith("OK:"):
                raise RuntimeError("write fixture did not commit")
            print("COMMITTED", flush=True)
            time.sleep(60)
            return
        tool_process.execute_in_subprocess(
            config,
            "Bash",
            {"command": "sleep 60", "timeout": 60},
            MutationBudget(),
            60,
            lease.fileno(),
            None,
            time.monotonic() + 60,
        )
    finally:
        lease.release()


if __name__ == "__main__":
    main()
