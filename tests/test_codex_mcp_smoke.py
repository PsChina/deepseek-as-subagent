from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _isolated_home_environment(home: str) -> dict[str, str]:
    """Point both POSIX and Windows home discovery at a disposable profile."""
    environment = os.environ.copy()
    isolated_home = os.path.abspath(home)
    home_drive, home_path = os.path.splitdrive(isolated_home)
    environment.update(
        {
            "HOME": isolated_home,
            "USERPROFILE": isolated_home,
            "HOMEDRIVE": home_drive,
            "HOMEPATH": home_path or os.sep,
        }
    )
    return environment


class CodexMcpSmokeTests(unittest.TestCase):
    def test_initialize_list_tools_and_ping_over_stdio(self) -> None:
        executable = Path(sys.executable).with_name("deepseek-mcp")
        if sys.platform == "win32":
            executable = executable.with_suffix(".exe")
        if not executable.exists():
            self.skipTest("deepseek-mcp entrypoint is not installed")

        with tempfile.TemporaryDirectory() as home:
            environment = _isolated_home_environment(home)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "adapters" / "codex" / "mcp_smoke.py"),
                    str(executable),
                ],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=25,
            )

        self.assertIn("MCP initialize/list_tools/ping OK", completed.stdout)


if __name__ == "__main__":
    unittest.main()
