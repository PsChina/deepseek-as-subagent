"""Local two-process convenience wrapper for the split live benchmark.

For stronger isolation, use the manual GitHub Actions workflow: the model and
hidden-test phases run on separate ephemeral workers.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="deepseek-benchmark-artifact-") as temporary:
        artifact = Path(temporary) / "benchmark-input.json"
        environment = dict(os.environ)
        environment["DEEPSEEK_BENCHMARK_ARTIFACT"] = str(artifact)
        prepared = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "prepare_live_code_benchmark.py")],
            env=environment, shell=False, check=False,
        )
        if prepared.returncode != 0:
            return prepared.returncode
        verification_environment = {
            name: value for name, value in environment.items()
            if name not in {"DEEPSEEK_API_KEY", "OPENAI_API_KEY"}
        }
        verified = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "verify_live_code_benchmark.py")],
            env=verification_environment, shell=False, check=False,
        )
        return verified.returncode


if __name__ == "__main__":
    sys.exit(main())
