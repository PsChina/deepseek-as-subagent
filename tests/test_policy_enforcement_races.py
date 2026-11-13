from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp import file_io
from deepseek_mcp.config import Config, DEFAULT_ALLOWED_TOOLS
from deepseek_mcp.policy_engine import compile_task_policy
from deepseek_mcp.task_contract import TaskContract
from deepseek_mcp.tools import execute_tool


class PolicyResolutionRaceTests(unittest.TestCase):
    def test_read_scope_is_rechecked_after_path_resolution_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src"
            private = root / "private"
            source.mkdir()
            private.mkdir()
            allowed = source / "allowed.txt"
            allowed.write_text("allowed", encoding="utf-8")
            secret = private / "secret.txt"
            secret.write_text("must-not-leak", encoding="utf-8")
            alias = source / "alias.txt"
            try:
                alias.symlink_to(allowed)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlink creation unavailable: {error}")

            contract = TaskContract.from_dict({
                "permissions": {
                    "tools": ["Read"],
                    "read_paths": ["src/**"],
                }
            })
            policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
            config = Config(
                "", root, allowed_tools=list(policy.allowed_tools),
                effective_policy=policy,
            )
            # The dispatcher sees an in-scope symlink target. Simulate an
            # attacker swapping it before file_io resolves the path to open.
            with patch.object(file_io, "resolve_safe_path", return_value=secret):
                result = execute_tool("Read", {"path": "src/alias.txt"}, config)

        self.assertIn("outside the task contract read scope", result)
        self.assertNotIn("must-not-leak", result)

    def test_write_scope_is_rechecked_after_path_resolution_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src"
            private = root / "private"
            allowed = source / "allowed"
            allowed.mkdir(parents=True)
            private.mkdir()
            secret_target = private / "new.txt"
            alias = source / "alias"
            try:
                alias.symlink_to(allowed, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlink creation unavailable: {error}")

            contract = TaskContract.from_dict({
                "permissions": {
                    "tools": ["Read", "Write"],
                    "read_paths": ["src/**"],
                    "write_paths": ["src/**"],
                }
            })
            policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
            config = Config(
                "", root, allowed_tools=list(policy.allowed_tools),
                effective_policy=policy,
            )
            # Simulate the alias being redirected to a private workspace path
            # between the early dispatcher check and the atomic writer.
            with patch.object(
                file_io, "resolve_safe_path", return_value=secret_target
            ):
                result = execute_tool(
                    "Write", {"path": "src/alias/new.txt", "content": "blocked"},
                    config,
                )

            self.assertFalse(secret_target.exists())

        self.assertIn("outside the task contract write scope", result)


if __name__ == "__main__":
    unittest.main()
