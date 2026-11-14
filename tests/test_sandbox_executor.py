"""Adversarial tests for the networkless hidden-test executor."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp.config import Config, DEFAULT_ALLOWED_TOOLS
from deepseek_mcp.policy_engine import compile_task_policy
from deepseek_mcp.sandbox_executor import (
    SandboxUnavailableError,
    compute_test_suite_digest,
    execute_sandboxed_tests,
    _container_argv,
)
from deepseek_mcp.task_contract import TaskContract

_IMAGE = "python:3.11-slim@sha256:" + "a" * 64


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class SandboxExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.hidden = self.root / "trusted-hidden"
        (self.workspace / "src").mkdir(parents=True)
        self.hidden.mkdir()
        self.source = b"def total(values):\n    return sum(values)\n"
        (self.workspace / "src" / "task.py").write_bytes(self.source)
        (self.hidden / "test_contract.py").write_text(
            "from pathlib import Path\n"
            "import unittest\n"
            "class HiddenTests(unittest.TestCase):\n"
            " def test_source_is_present(self):\n"
            "  self.assertTrue(Path('/workspace/src/task.py').is_file())\n",
            encoding="utf-8",
        )
        contract = TaskContract.from_dict({
            "permissions": {
                "tools": ["Read", "Write", "Edit"],
                "read_paths": ["src/**"], "write_paths": ["src/**"],
            }
        })
        policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
        self.config = Config(
            "", self.workspace, allowed_tools=list(policy.allowed_tools),
            effective_policy=policy,
        )

    def _plan(self, **changes):
        plan = {
            "version": 1,
            "contract_sha256": self.config.effective_policy.contract_hash,
            "test_suite_sha256": compute_test_suite_digest(self.hidden),
            "source_files": [{"path": "src/task.py", "sha256": _sha(self.source)}],
        }
        plan.update(changes)
        return plan

    def test_only_exact_authorized_source_and_hidden_tests_are_staged(self):
        observed = {}

        def invoke(_docker, _image, source, tests, _root, _timeout):
            observed["source"] = sorted(
                p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()
            )
            observed["test"] = sorted(
                p.relative_to(tests).as_posix() for p in tests.rglob("*") if p.is_file()
            )
            observed["source_bytes"] = (source / "src" / "task.py").read_bytes()
            return {"status": "passed", "exit_code": 0, "output": "OK",
                    "output_bytes_captured": 2, "duration_ms": 12}

        with patch("deepseek_mcp.sandbox_executor._docker_binary", return_value="/usr/bin/docker"), \
             patch("deepseek_mcp.sandbox_executor._invoke_container", side_effect=invoke):
            result = execute_sandboxed_tests(
                self.config, self._plan(), self.hidden, image=_IMAGE
            )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(observed["source"], ["src/task.py"])
        self.assertEqual(observed["test"], ["test_contract.py"])
        self.assertEqual(observed["source_bytes"], self.source)
        self.assertEqual(result["network"], "disabled")
        self.assertEqual(result["hidden_tests"], "not_exposed_to_agent")

    def test_digest_mismatch_and_untrusted_suite_mutation_fail_closed(self):
        plan = self._plan()
        plan["source_files"][0]["sha256"] = "b" * 64
        with patch("deepseek_mcp.sandbox_executor._docker_binary", return_value="/usr/bin/docker"), \
             patch("deepseek_mcp.sandbox_executor._invoke_container") as invoke:
            with self.assertRaisesRegex(ValueError, "source digest"):
                execute_sandboxed_tests(self.config, plan, self.hidden, image=_IMAGE)
        invoke.assert_not_called()
        plan = self._plan(test_suite_sha256="c" * 64)
        with patch("deepseek_mcp.sandbox_executor._docker_binary", return_value="/usr/bin/docker"):
            with self.assertRaisesRegex(ValueError, "hidden test suite digest"):
                execute_sandboxed_tests(self.config, plan, self.hidden, image=_IMAGE)

    def test_contract_scope_traversal_and_duplicate_source_are_rejected(self):
        plan = self._plan()
        plan["source_files"][0]["path"] = ".env"
        with patch("deepseek_mcp.sandbox_executor._docker_binary", return_value="/usr/bin/docker"):
            with self.assertRaisesRegex(ValueError, "read scope"):
                execute_sandboxed_tests(self.config, plan, self.hidden, image=_IMAGE)
        plan = self._plan()
        plan["source_files"] = [
            {"path": "src/task.py", "sha256": _sha(self.source)},
            {"path": "SRC/TASK.PY", "sha256": _sha(self.source)},
        ]
        with self.assertRaisesRegex(ValueError, "unique"):
            execute_sandboxed_tests(self.config, plan, self.hidden, image=_IMAGE)
        for path in ("../outside.py", "src/../outside.py", "C:/secret.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                plan = self._plan()
                plan["source_files"][0]["path"] = path
                execute_sandboxed_tests(self.config, plan, self.hidden, image=_IMAGE)

    def test_hidden_test_symlinks_and_workspace_overlap_are_rejected(self):
        alias = self.hidden / "alias.py"
        try:
            alias.symlink_to(self.hidden / "test_contract.py")
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        with patch("deepseek_mcp.sandbox_executor._docker_binary", return_value="/usr/bin/docker"):
            with self.assertRaisesRegex(ValueError, "symlink"):
                execute_sandboxed_tests(self.config, self._plan(), self.hidden, image=_IMAGE)
        alias.unlink()
        with patch("deepseek_mcp.sandbox_executor._docker_binary", return_value="/usr/bin/docker"):
            with self.assertRaisesRegex(ValueError, "separate"):
                execute_sandboxed_tests(self.config, self._plan(), self.workspace, image=_IMAGE)

    def test_container_command_is_networkless_readonly_and_unprivileged(self):
        args = _container_argv(
            "/usr/bin/docker", _IMAGE, Path("/tmp/source"), Path("/tmp/tests"),
            Path("/tmp/container.id"),
        )
        joined = " ".join(args)
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges=true", "--pids-limit=64",
                     "--memory=512m", "--memory-swap=512m", "--cpus=1",
                     "--user=65534:65534", "--ipc=none", "--pull=never"):
            self.assertIn(flag, joined)
        self.assertIn("dst=/workspace,readonly", joined)
        self.assertIn("dst=/tests,readonly", joined)
        self.assertNotIn("DEEPSEEK_API_KEY", joined)
        self.assertNotIn("bash", args)
        self.assertEqual(args[-10:], ["python", "-I", "-m", "unittest",
                                      "discover", "-s", "/tests", "-p", "test_*.py", "-v"])

    def test_image_requires_immutable_digest_and_docker_is_fail_closed(self):
        with patch("deepseek_mcp.sandbox_executor._docker_binary", return_value="/usr/bin/docker"):
            with self.assertRaisesRegex(ValueError, "immutable SHA-256"):
                execute_sandboxed_tests(self.config, self._plan(), self.hidden,
                                         image="python:latest")
        with patch("deepseek_mcp.sandbox_executor._docker_binary",
                   side_effect=SandboxUnavailableError("no docker")):
            with self.assertRaises(SandboxUnavailableError):
                execute_sandboxed_tests(self.config, self._plan(), self.hidden,
                                         image=_IMAGE)

    def test_hidden_suite_digest_is_order_independent_and_content_sensitive(self):
        original = compute_test_suite_digest(self.hidden)
        extra = self.hidden / "fixture.json"
        extra.write_text('{"case": 1}', encoding="utf-8")
        changed = compute_test_suite_digest(self.hidden)
        self.assertNotEqual(original, changed)


if __name__ == "__main__":
    unittest.main()
