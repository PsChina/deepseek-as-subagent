"""Adversarial tests for trusted artifact verification and benchmark accounting."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp.benchmark_metrics import summarize_benchmark
from deepseek_mcp.config import Config, DEFAULT_ALLOWED_TOOLS
from deepseek_mcp.independent_verifier import verify_artifacts
from deepseek_mcp.policy_engine import compile_task_policy
from deepseek_mcp.task_contract import TaskContract
from deepseek_mcp.verification_plan import VerificationPlan


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class IndependentVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "output.py").write_bytes(b"good\n")
        (self.root / "src" / "guard.txt").write_text("keep", encoding="utf-8")
        contract = TaskContract.from_dict({
            "permissions": {
                "tools": ["Read", "Write", "Edit"],
                "read_paths": ["src/**"], "write_paths": ["src/**"],
            },
            "constraints": {"success_checks": ["python -m unittest discover"]},
        })
        policy = compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)
        self.config = Config(
            "", self.root, allowed_tools=list(policy.allowed_tools),
            effective_policy=policy,
        )

    def _plan(self, checks=()):
        return {"version": 1,
                "contract_sha256": self.config.effective_policy.contract_hash,
                "artifacts": [
                    {"path": "src/output.py", "sha256": _sha("good\n"), "role": "output"},
                    {"path": "src/guard.txt", "sha256": _sha("keep"), "role": "unchanged"},
                ],
                "success_checks": list(checks)}

    def test_exact_output_and_unchanged_baseline_are_artifact_verified(self):
        result = verify_artifacts(self.config, self._plan())
        self.assertEqual(result["status"], "artifact_verified")
        self.assertEqual([x["status"] for x in result["artifacts"]],
                         ["matched", "matched"])
        self.assertEqual(result["functional_correctness"],
                         "not_independently_verified")

    def test_changed_guard_is_failed_even_if_output_matches(self):
        (self.root / "src" / "guard.txt").write_text("tampered", encoding="utf-8")
        report = verify_artifacts(self.config, self._plan())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["artifacts"][1]["status"], "changed")

    def test_missing_file_and_unrun_tests_are_not_passed(self):
        (self.root / "src" / "output.py").unlink()
        report = verify_artifacts(self.config, self._plan())
        self.assertEqual(report["status"], "incomplete")
        report = verify_artifacts(self.config, self._plan(
            checks=["python -m unittest discover"]))
        self.assertEqual(report["success_checks"][0]["status"], "not_run")
        self.assertEqual(report["status"], "incomplete")

    def test_cannot_swap_contract_hash_or_invent_test_evidence(self):
        plan = self._plan()
        plan["contract_sha256"] = "b" * 64
        self.assertEqual(verify_artifacts(self.config, plan)["status"], "invalid_plan")
        plan = self._plan(checks=["trust me; all tests passed"])
        self.assertEqual(verify_artifacts(self.config, plan)["status"], "invalid_plan")

    def test_denied_secrets_and_symlink_aliases_do_not_verify(self):
        (self.root / ".env").write_text("SECRET", encoding="utf-8")
        secret = self._plan()
        secret["artifacts"] = [{"path": ".env", "sha256": _sha("SECRET"),
                                "role": "unchanged"}]
        denied = verify_artifacts(self.config, secret)
        self.assertEqual(denied["artifacts"][0]["status"], "denied")
        self.assertEqual(denied["status"], "failed")
        alias = self.root / "src" / "output.py"
        alias.unlink()
        try:
            alias.symlink_to(self.root / "src" / "guard.txt")
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink privilege unavailable: {error}")
        reported = verify_artifacts(self.config, self._plan())
        self.assertEqual(reported["artifacts"][0]["status"], "not_verifiable")
        self.assertEqual(reported["status"], "incomplete")

    def test_manifest_validation_rejects_traversal_duplicate_and_unknown_fields(self):
        for path in ("../x", "src/../x", "src//x", "/etc/passwd",
                     "C:/secret", "src\\alias.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                plan = self._plan()
                plan["artifacts"][0]["path"] = path
                VerificationPlan.from_dict(plan)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            plan = self._plan()
            plan["artifacts"][1]["path"] = "SRC/OUTPUT.PY"
            VerificationPlan.from_dict(plan)
        with self.assertRaises(ValueError):
            plan = self._plan()
            plan["unexpected"] = "ignore"
            VerificationPlan.from_dict(plan)

    def test_benchmark_fails_closed_for_unknown_or_unsafe_cases(self):
        records = [
            {"id": "ok", "verdict": "passed", "policy_passed": True,
             "duration_ms": 100, "tool_calls": 2},
            {"id": "unknown", "verdict": "incomplete", "policy_passed": True,
             "duration_ms": 200, "tool_calls": 1},
            {"id": "unsafe", "verdict": "passed", "policy_passed": False,
             "duration_ms": 300, "tool_calls": 3},
        ]
        summary = summarize_benchmark(records)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["unsafe"], 1)
        self.assertEqual(summary["incomplete"], 1)
        self.assertEqual(summary["median_duration_ms"], 200)
        with self.assertRaises(ValueError):
            summarize_benchmark([records[0], records[0]])
        with self.assertRaises(ValueError):
            summarize_benchmark([dict(records[0], verdict=[])])


if __name__ == "__main__":
    unittest.main()
