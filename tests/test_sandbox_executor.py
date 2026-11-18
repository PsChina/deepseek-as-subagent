"""Tests for the networkless, host-verified candidate executor."""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from deepseek_mcp.config import Config, DEFAULT_ALLOWED_TOOLS
from deepseek_mcp.policy_engine import compile_task_policy
from deepseek_mcp.sandbox_executor import (
    SandboxSource,
    SandboxTestPlan,
    SandboxUnavailableError,
    compute_test_suite_digest,
    execute_sandboxed_tests,
)
from deepseek_mcp.sandbox_worker import (
    SandboxExecutionError,
    SandboxOutputLimitError,
    SandboxTimeoutError,
    _Protocol,
    container_argv,
)
from deepseek_mcp.task_contract import TaskContract

_IMAGE = "python:3.11-slim@sha256:" + "a" * 64
_SMOKE_IMAGE = os.environ.get("DEEPSEEK_SANDBOX_SMOKE_IMAGE")
_ROOT = Path(__file__).resolve().parents[1]


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
        self.entrypoint = self.workspace / "src" / "task.py"
        self.source = b"def total(values):\n    return sum(values)\n"
        self.entrypoint.write_bytes(self.source)
        (self.hidden / "test_contract.py").write_bytes(
            b"def run(candidate, fixtures):\n"
            b"    assert fixtures['sample.txt'] == b'host-only'\n"
            b"    assert candidate.call('total', [1, 3]) == 4\n"
            b"    return 2\n"
        )
        (self.hidden / "sample.txt").write_bytes(b"host-only")
        self.config = self._config()

    def _config(self, paths=("src/task.py",)) -> Config:
        contract = TaskContract.from_dict({"permissions": {"read_paths": list(paths)}})
        policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
        return Config("", self.workspace, allowed_tools=list(policy.allowed_tools), effective_policy=policy)

    def _plan(self, *, suite=None, source=None, **changes):
        plan = {
            "version": 1,
            "contract_sha256": self.config.effective_policy.contract_hash,
            "test_suite_sha256": compute_test_suite_digest(suite or self.hidden),
            "source_files": [{
                "path": "src/task.py",
                "sha256": _sha(self.source if source is None else source),
            }],
        }
        plan.update(changes)
        return plan

    @patch("deepseek_mcp.sandbox_executor._docker_binary", return_value="/usr/bin/docker")
    def test_only_the_candidate_source_is_staged(self, _docker):
        (self.workspace / "src" / "unrelated.py").write_bytes(b"not part of the plan")
        candidate = MagicMock(output_bytes=23)
        candidate.call.return_value = 4

        def start(_docker, _image, source, temp_root, _timeout):
            self.assertEqual(list(temp_root.iterdir()), [source])
            self.assertEqual(
                sorted(item.relative_to(source).as_posix() for item in source.rglob("*") if item.is_file()),
                ["src/task.py"],
            )
            self.assertEqual((source / "src" / "task.py").read_bytes(), self.source)
            return candidate

        with patch("deepseek_mcp.sandbox_executor.CandidateSession", side_effect=start):
            result = execute_sandboxed_tests(self.config, self._plan(), self.hidden, image=_IMAGE)
        candidate.call.assert_called_once_with("total", [1, 3])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["assertions"], 2)
        self.assertEqual(result["output_bytes_captured"], 23)
        self.assertEqual(result["network"], "disabled")
        self.assertEqual(result["hidden_tests"], "host_only_not_mounted")

    @patch("deepseek_mcp.sandbox_executor._docker_binary", return_value="/usr/bin/docker")
    def test_digest_mismatch_and_untrusted_suite_mutation_fail_closed(self, _docker):
        plan = self._plan(source=b"different")
        with patch("deepseek_mcp.sandbox_executor.CandidateSession") as invoke:
            with self.assertRaisesRegex(ValueError, "source digest"):
                execute_sandboxed_tests(self.config, plan, self.hidden, image=_IMAGE)
        invoke.assert_not_called()
        plan = self._plan()
        (self.hidden / "sample.txt").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "hidden test suite digest"):
            execute_sandboxed_tests(self.config, plan, self.hidden, image=_IMAGE)

    @patch("deepseek_mcp.sandbox_executor._docker_binary", return_value="/usr/bin/docker")
    def test_contract_scope_and_fixed_entrypoint_are_enforced(self, _docker):
        self.config = self._config(("docs/**",))
        with self.assertRaisesRegex(ValueError, "read scope"):
            execute_sandboxed_tests(self.config, self._plan(), self.hidden, image=_IMAGE)
        for path in (".env", "src/../outside.py", "/outside.py", "SRC/TASK.PY"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "entrypoint"):
                SandboxSource(path, _sha(self.source))
        plan = self._plan()
        plan["source_files"] *= 2
        with self.assertRaisesRegex(ValueError, "exactly one"):
            SandboxTestPlan.from_dict(plan)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            SandboxTestPlan("a" * 64, "b" * 64, (object(),))

    def test_hidden_test_symlinks_and_workspace_overlap_are_rejected(self):
        plan = self._plan()
        with self.assertRaisesRegex(ValueError, "separate"):
            execute_sandboxed_tests(self.config, plan, self.workspace, image=_IMAGE)
        alias = self.hidden / "alias.py"
        try:
            alias.symlink_to(self.hidden / "test_contract.py")
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "symlink"):
            execute_sandboxed_tests(self.config, plan, self.hidden, image=_IMAGE)

    def test_container_command_is_networkless_readonly_and_unprivileged(self):
        args = container_argv("/usr/bin/docker", _IMAGE, Path("/tmp/source"), "test")
        for flag in (
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true", "--pids-limit=64",
            "--memory=512m", "--memory-swap=512m", "--cpus=1",
            "--user=65534:65534", "--ipc=none", "--pull=never", "--log-driver=none",
        ):
            self.assertIn(flag, args)
        self.assertEqual(args.count("--mount"), 1)
        self.assertTrue(args[args.index("--mount") + 1].endswith("dst=/workspace,readonly"))
        self.assertFalse(any(option.startswith("--security-opt=seccomp") for option in args))
        self.assertEqual(args[-6:-1], ["--entrypoint=python", _IMAGE, "-I", "-B", "-c"])

    def test_image_requires_immutable_digest_and_docker_is_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "immutable SHA-256"):
            execute_sandboxed_tests(self.config, self._plan(), self.hidden, image="python:latest")
        with patch("deepseek_mcp.sandbox_executor._docker_binary",
                   side_effect=SandboxUnavailableError("no docker")):
            with self.assertRaises(SandboxUnavailableError):
                execute_sandboxed_tests(self.config, self._plan(), self.hidden, image=_IMAGE)

    @unittest.skipUnless(_SMOKE_IMAGE, "requires a pinned image in a real Docker runner")
    def test_real_docker_round_trips_both_hosted_suites(self):
        for case, source in (
            ("off_by_one", self.source),
            ("case_normalization", b"def slug(value):\n    return value.strip().lower()\n"),
        ):
            with self.subTest(case=case):
                self.entrypoint.write_bytes(source)
                suite = _ROOT / "benchmarks" / "hidden" / case
                result = execute_sandboxed_tests(
                    self.config, self._plan(suite=suite, source=source), suite,
                    image=_SMOKE_IMAGE, timeout_seconds=30,
                )
                self.assertEqual(result["status"], "passed", result)
                self.assertEqual(result["assertions"], 4)
                self.assertGreater(result["output_bytes_captured"], 0)

    @unittest.skipUnless(_SMOKE_IMAGE, "requires a pinned image in a real Docker runner")
    def test_real_docker_reports_a_failed_candidate(self):
        source = b"def total(values):\n    return sum(values) + 1\n"
        self.entrypoint.write_bytes(source)
        suite = _ROOT / "benchmarks" / "hidden" / "off_by_one"
        result = execute_sandboxed_tests(
            self.config, self._plan(suite=suite, source=source), suite,
            image=_SMOKE_IMAGE, timeout_seconds=30,
        )
        self.assertEqual(result["status"], "failed", result)


@unittest.skipUnless(os.name == "posix", "pipe selectors require a POSIX host")
class SandboxProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        self.writer = writer
        self.protocol = _Protocol(writer, reader)

    def test_full_input_pipe_respects_the_deadline(self):
        try:
            while True:
                os.write(self.writer, b"x" * 4096)
        except BlockingIOError:
            pass
        with self.assertRaises(SandboxTimeoutError):
            self.protocol.send({"name": "total"}, time.monotonic() + 0.05)

    def test_response_rejects_duplicates_and_enforces_aggregate_output_limit(self):
        os.write(self.writer, b'{"result": 1, "result": 2}\n')
        with self.assertRaises(SandboxExecutionError):
            self.protocol.receive(time.monotonic() + 1)
        with patch("deepseek_mcp.sandbox_worker._MAX_OUTPUT_BYTES", 64):
            os.write(self.writer, b"x" * 40)
            with self.assertRaises(SandboxOutputLimitError):
                self.protocol.receive(time.monotonic() + 1)


if __name__ == "__main__":
    unittest.main()
