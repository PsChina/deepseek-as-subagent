from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from deepseek_mcp.config import Config, DEFAULT_ALLOWED_TOOLS
from deepseek_mcp.execution_profile import CODING_PROFILE, READONLY_PROFILE
from deepseek_mcp.policy_engine import EffectivePolicy, compile_task_policy
from deepseek_mcp.policy_enforcement import path_is_allowed, path_matches_pattern
from deepseek_mcp.task_contract import TaskContract
from deepseek_mcp.tool_child import _config as child_config
from deepseek_mcp.tool_process import _config_payload
from deepseek_mcp.tools import execute_tool


class TaskContractTests(unittest.TestCase):
    def test_contract_is_versioned_strict_and_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported task contract version"):
            TaskContract.from_dict({"version": 2})
        with self.assertRaisesRegex(ValueError, "unknown task contract field"):
            TaskContract.from_dict({"surprise": True})
        with self.assertRaisesRegex(ValueError, "Bash is not available"):
            TaskContract.from_dict({"permissions": {"tools": ["Read", "Bash"]}})
        with self.assertRaisesRegex(ValueError, "workspace-relative"):
            TaskContract.from_dict({"permissions": {"read_paths": ["../outside"]}})

    def test_mutation_requires_explicit_write_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit permissions.write_paths"):
            TaskContract.from_dict({"permissions": {"tools": ["Read", "Write"]}})

    def test_readonly_intent_cannot_declare_mutation(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot declare write_paths"):
            TaskContract.from_dict({
                "intent": "audit", "permissions": {"write_paths": ["src/**"]},
            })

    def test_policy_intersects_profile_and_contract_tools(self) -> None:
        contract = TaskContract.from_dict({
            "permissions": {"tools": ["Read", "Write", "Glob"], "write_paths": ["src/**"]}
        })
        policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
        self.assertEqual(policy.allowed_tools, ("Read", "Write", "Glob"))
        readonly_policy = compile_task_policy(contract, list(READONLY_PROFILE.allowed_tools))
        self.assertEqual(readonly_policy.allowed_tools, ("Read", "Glob"))

    def test_contract_without_write_scope_removes_mutation_and_bash(self) -> None:
        policy = compile_task_policy(TaskContract.from_dict({}), DEFAULT_ALLOWED_TOOLS)
        self.assertNotIn("Bash", policy.allowed_tools)
        self.assertFalse(set(policy.allowed_tools).intersection({"Write", "Edit", "NotebookEdit"}))

    def test_path_globs_support_recursive_double_star_and_deny_precedence(self) -> None:
        self.assertTrue(path_matches_pattern("src/nested/app.py", "src/**/*.py"))
        self.assertTrue(path_matches_pattern("src/app.py", "src/**/*.py"))
        self.assertFalse(path_matches_pattern("docs/readme.md", "src/**/*.py"))
        self.assertFalse(path_is_allowed("src/.env.local", ("src/**",), (".env*", "**/.env*")))

    def test_policy_round_trips_through_isolated_tool_process_config(self) -> None:
        contract = TaskContract.from_dict({
            "permissions": {"tools": ["Read", "Write"], "write_paths": ["src/**"]}
        })
        policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config("", Path(tmpdir), allowed_tools=list(policy.allowed_tools), effective_policy=policy)
            child = child_config(_config_payload(config))
        self.assertEqual(child.effective_policy, policy)
        self.assertEqual(child.allowed_tools, list(policy.allowed_tools))
        self.assertEqual(EffectivePolicy.from_dict(policy.to_dict()), policy)

    def test_read_and_write_are_enforced_by_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "docs").mkdir()
            (root / "src" / "app.py").write_text("print('ok')", encoding="utf-8")
            (root / "docs" / "readme.md").write_text("private", encoding="utf-8")
            (root / ".env").write_text("secret", encoding="utf-8")
            contract = TaskContract.from_dict({
                "permissions": {
                    "tools": ["Read", "Write"],
                    "read_paths": ["src/**"],
                    "write_paths": ["src/**"],
                }
            })
            policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
            config = Config("", root, allowed_tools=list(policy.allowed_tools), effective_policy=policy)
            self.assertIn("print('ok')", execute_tool("Read", {"path": "src/app.py"}, config))
            self.assertIn("outside the task contract read scope", execute_tool("Read", {"path": "docs/readme.md"}, config))
            self.assertIn("outside the task contract read scope", execute_tool("Read", {"path": ".env"}, config))
            self.assertTrue(execute_tool("Write", {"path": "src/new.py", "content": "x=1"}, config).startswith("OK:"))
            self.assertIn("outside the task contract write scope", execute_tool("Write", {"path": "docs/new.py", "content": "x=1"}, config))
            self.assertFalse((root / "docs" / "new.py").exists())
            self.assertIn("not allowed", execute_tool("Bash", {"command": "pwd"}, config))

    def test_glob_and_grep_do_not_disclose_out_of_scope_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "private").mkdir()
            (root / "src" / "visible.txt").write_text("needle visible", encoding="utf-8")
            (root / "private" / "hidden.txt").write_text("needle secret", encoding="utf-8")
            contract = TaskContract.from_dict({
                "permissions": {"tools": ["Glob", "Grep"], "read_paths": ["src/**"]}
            })
            policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
            config = Config("", root, allowed_tools=list(policy.allowed_tools), effective_policy=policy)
            glob_result = execute_tool("Glob", {"pattern": "**/*"}, config)
            grep_result = execute_tool("Grep", {"pattern": "needle"}, config)
        self.assertIn("src/visible.txt", glob_result)
        self.assertNotIn("private/hidden.txt", glob_result)
        self.assertIn("src/visible.txt", grep_result)
        self.assertNotIn("private/hidden.txt", grep_result)
        self.assertNotIn("needle secret", grep_result)

    def test_global_tool_allowlist_is_not_expanded_by_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config("", Path(tmpdir), allowed_tools=["Read"])
            coding = CODING_PROFILE.bind(config)
            readonly = READONLY_PROFILE.bind(config)
        self.assertEqual(coding.allowed_tools, ["Read"])
        self.assertEqual(readonly.allowed_tools, ["Read"])


if __name__ == "__main__":
    unittest.main()
