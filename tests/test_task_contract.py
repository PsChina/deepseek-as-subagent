from __future__ import annotations

import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from deepseek_mcp.agent_loop import AgentLoopError, _validate_tool_batch
from deepseek_mcp.config import Config, DEFAULT_ALLOWED_TOOLS
from deepseek_mcp.execution_profile import CODING_PROFILE, READONLY_PROFILE
from deepseek_mcp.policy_engine import EffectivePolicy, compile_task_policy
from deepseek_mcp.resource_budget import (
    MAX_MUTATION_BYTES_PER_RUN,
    MutationBudget,
    ResourceBudgetExceeded,
)
from deepseek_mcp.policy_enforcement import path_is_allowed, path_matches_pattern
from deepseek_mcp.task_contract import DEFAULT_DENY_PATHS, TaskContract, _validated_input
from deepseek_mcp.tool_child import _config as child_config
from deepseek_mcp.tool_process import _config_payload, execute_in_subprocess
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

    def test_deeply_nested_contract_is_rejected_as_validation_error(self) -> None:
        nested: object = []
        for _ in range(1200):
            nested = [nested]
        with self.assertRaisesRegex(ValueError, "bounded JSON-compatible"):
            _validated_input({"permissions": {"read_paths": nested}})

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

    def test_contract_limits_cannot_exceed_global_budgets(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_tool_calls"):
            TaskContract.from_dict({"limits": {"max_tool_calls": 129}})
        contract = TaskContract.from_dict({"limits": {"max_tool_calls": 2, "max_mutation_bytes": 1024}})
        policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
        state = SimpleNamespace(tool_calls=2, config=SimpleNamespace(effective_policy=policy))
        with self.assertRaises(AgentLoopError):
            _validate_tool_batch(state, [object()])
        with self.assertRaises(ResourceBudgetExceeded):
            with MutationBudget(limit=policy.max_mutation_bytes).reserve(1025):
                pass

    def test_concurrent_mutation_reservations_are_atomic(self) -> None:
        budget = MutationBudget(limit=64)

        def attempt(_index: int) -> bool:
            try:
                with budget.reserve(1):
                    return True
            except ResourceBudgetExceeded:
                return False

        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(attempt, range(512)))
        self.assertEqual(sum(results), 64)
        self.assertEqual(budget.used, 64)

    def test_child_budget_reconciliation_cannot_regress_or_overflow(self) -> None:
        budget = MutationBudget(limit=12, used=5)
        budget.advance_to(11)
        self.assertEqual(budget.used, 11)
        for value in (10, 13, True, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                budget.advance_to(value)
        self.assertEqual(budget.used, 11)

    def test_failed_reservation_is_not_refunded(self) -> None:
        budget = MutationBudget(limit=10)
        with self.assertRaisesRegex(RuntimeError, "after writing"):
            with budget.reserve(7):
                raise RuntimeError("after writing")
        self.assertEqual(budget.used, 7)

    def test_path_globs_support_recursive_double_star_and_deny_precedence(self) -> None:
        self.assertTrue(path_matches_pattern("src/nested/app.py", "src/**/*.py"))
        self.assertTrue(path_matches_pattern("src/app.py", "src/**/*.py"))
        self.assertFalse(path_matches_pattern("docs/readme.md", "src/**/*.py"))
        self.assertFalse(path_is_allowed("src/.env.local", ("src/**",), (".env*", "**/.env*")))
        self.assertFalse(path_is_allowed("src/.ENV.local", ("src/**",), (".env*", "**/.env*")))
        self.assertFalse(path_is_allowed(".aws/credentials", ("**/*",), DEFAULT_DENY_PATHS))
        for unsafe in ("/etc/passwd", "../outside", "src/../outside", "C:/secret", "src\\secret"):
            self.assertFalse(path_is_allowed(unsafe, ("**/*",), ()))
        self.assertFalse(path_is_allowed("src/.ENV.local", ("src/**",), DEFAULT_DENY_PATHS))
        if os.name != "nt":
            self.assertFalse(path_is_allowed("SRC/app.py", ("src/**",), ()))

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

    def test_isolated_tool_child_enforces_contract_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "private").mkdir()
            (root / "private" / "secret.txt").write_text("secret", encoding="utf-8")
            contract = TaskContract.from_dict({"permissions": {"tools": ["Read"], "read_paths": ["src/**"]}})
            policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
            config = Config("", root, allowed_tools=list(policy.allowed_tools), effective_policy=policy)
            result = execute_in_subprocess(config, "Read", {"path": "private/secret.txt"}, MutationBudget(), 10, None, None, time.monotonic() + 5)
        self.assertIn("outside the task contract read scope", result)

    def test_notebook_edit_obeys_write_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "private").mkdir()
            contract = TaskContract.from_dict({"permissions": {"tools": ["NotebookEdit"], "write_paths": ["src/**"]}})
            policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
            config = Config("", root, allowed_tools=list(policy.allowed_tools), effective_policy=policy)
            result = execute_tool("NotebookEdit", {"path": "private/new.ipynb", "edit_mode": "insert", "new_source": "x=1"}, config)
            self.assertIn("outside the task contract write scope", result)
            self.assertFalse((root / "private" / "new.ipynb").exists())

    def test_read_scope_checks_resolved_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "private").mkdir()
            secret = root / "private" / "secret.txt"
            secret.write_text("secret", encoding="utf-8")
            alias = root / "src" / "alias.txt"
            try:
                alias.symlink_to(secret)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable on this platform")
            contract = TaskContract.from_dict({"permissions": {"tools": ["Read"], "read_paths": ["src/**"]}})
            policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
            config = Config("", root, allowed_tools=list(policy.allowed_tools), effective_policy=policy)
            result = execute_tool("Read", {"path": "src/alias.txt"}, config)
        self.assertIn("outside the task contract read scope", result)

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


    def test_edit_operations_require_both_read_and_write_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            target = root / "src" / "app.py"
            target.write_text("secret = 1", encoding="utf-8")
            contract = TaskContract.from_dict({
                "permissions": {
                    "tools": ["Read", "Write", "Edit", "NotebookEdit"],
                    "read_paths": ["tests/**"],
                    "write_paths": ["src/**"],
                }
            })
            policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
            config = Config(
                "", root, allowed_tools=list(policy.allowed_tools),
                effective_policy=policy,
            )
            edit_result = execute_tool(
                "Edit",
                {"path": "src/app.py", "old_string": "secret = 1", "new_string": "secret = 2"},
                config,
            )
            notebook_result = execute_tool(
                "NotebookEdit",
                {"path": "src/notebook.ipynb", "edit_mode": "insert", "new_source": "x=1"},
                config,
            )
            write_result = execute_tool(
                "Write", {"path": "src/new.py", "content": "x = 1"}, config,
            )
            self.assertIn("outside the task contract read scope", edit_result)
            self.assertIn("outside the task contract read scope", notebook_result)
            self.assertEqual(target.read_text(encoding="utf-8"), "secret = 1")
            self.assertFalse((root / "src" / "notebook.ipynb").exists())
            self.assertTrue(write_result.startswith("OK: wrote"))

    def test_dispatcher_rejects_budget_above_global_hard_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = Config("", root, allowed_tools=["Write"])
            with self.assertRaises(ResourceBudgetExceeded):
                execute_tool(
                    "Write", {"path": "new.py", "content": "x"}, config,
                    mutation_budget=MutationBudget(limit=MAX_MUTATION_BYTES_PER_RUN + 1),
                )
            self.assertFalse((root / "new.py").exists())

    def test_subprocess_rejects_budget_above_task_contract_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            contract = TaskContract.from_dict({
                "permissions": {"tools": ["Write"], "write_paths": ["src/**"]},
                "limits": {"max_mutation_bytes": 8},
            })
            policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
            config = Config(
                "", root, allowed_tools=list(policy.allowed_tools),
                effective_policy=policy,
            )
            with self.assertRaises(ResourceBudgetExceeded):
                execute_in_subprocess(
                    config, "Write",
                    {"path": "src/new.py", "content": "123456789"},
                    MutationBudget(limit=9), 10, None, None, time.monotonic() + 5,
                )
            self.assertFalse((root / "src" / "new.py").exists())

    def test_dispatcher_applies_contract_budget_when_caller_omits_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            contract = TaskContract.from_dict({
                "permissions": {"tools": ["Write"], "write_paths": ["src/**"]},
                "limits": {"max_mutation_bytes": 8},
            })
            policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
            config = Config(
                "", root, allowed_tools=list(policy.allowed_tools),
                effective_policy=policy,
            )
            with self.assertRaises(ResourceBudgetExceeded):
                execute_tool("Write", {"path": "src/new.py", "content": "123456789"}, config)
            self.assertFalse((root / "src" / "new.py").exists())

    def test_subprocess_enforces_contract_budget_before_file_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            contract = TaskContract.from_dict({
                "permissions": {"tools": ["Write"], "write_paths": ["src/**"]},
                "limits": {"max_mutation_bytes": 8},
            })
            policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
            config = Config(
                "", root, allowed_tools=list(policy.allowed_tools),
                effective_policy=policy,
            )
            with self.assertRaises(ResourceBudgetExceeded):
                execute_in_subprocess(
                    config, "Write",
                    {"path": "src/new.py", "content": "123456789"},
                    MutationBudget(limit=8), 10, None, None, time.monotonic() + 5,
                )
            self.assertFalse((root / "src" / "new.py").exists())


if __name__ == "__main__":
    unittest.main()
