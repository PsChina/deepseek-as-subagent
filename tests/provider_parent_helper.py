"""Subprocess fixture: start one provider child and report its PID."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from deepseek_mcp.config import Config
from deepseek_mcp import provider_process


def main() -> None:
    original_popen = provider_process.subprocess.Popen

    def reporting_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        print(process.pid, flush=True)
        return process

    provider_process.subprocess.Popen = reporting_popen
    config = Config(
        "placeholder",
        Path(sys.argv[1]),
        base_url=sys.argv[2],
        allowed_tools=["Read"],
    )
    provider_process.request_in_subprocess(
        config,
        [{"role": "user", "content": "wait"}],
        [],
        None,
        time.monotonic() + float(sys.argv[3]),
    )


if __name__ == "__main__":
    main()
