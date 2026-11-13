from __future__ import annotations

import json
import unittest

from deepseek_mcp.bash_outcome import BashOutcome, record_bash_result
from deepseek_mcp.handoff import build_handoff
from deepseek_mcp.mutation_outcome import MutationAccumulator, mutation_record
from deepseek_mcp.result_format import format_sync_result
from deepseek_mcp.verification import verify_execution_record


class HandoffTests(unittest.TestCase):
    def _result(self):
        contract = {
            "intent": "implement",
            "contract_hash": "a" * 64,
            "allowed_tools": ["Read", "Write"],
            "read_paths": ["src/**"],
            "write_paths": ["src/**"],
            "deny_paths": [".env*", "**/.env*"],
            "max_tool_calls": 20,
            "max_mutation_bytes": 1024,
            "success_checks": ["pytest tests/"],
            "must_not_change": ["public API"],
            "expected_outputs": ["changed_files"],
        }
        accumulator = MutationAccumulator()
        accumulator.add(mutation_record(
            "b" * 32, "Write", "committed", path="src/app.py", sha256="c" * 64
        ))
        return {
            "final_message": "Implemented the feature.",
            "turns_used": 3,
            "tool_calls": 4,
            "mutation_bytes_reserved": 5,
            "tokens": {"prompt": 100, "completion": 50, "total": 150},
            "duration_seconds": 2.5,
            "bash": {"calls": 0, "executed_calls": 0, "failure_count": 0, "last_status": None, "failures": []},
            "mutations": accumulator.payload(),
            "task_contract": contract,
        }

    def test_handoff_contains_host_recorded_file_evidence(self):
        report = build_handoff(self._result())
        self.assertEqual(report["execution_status"], "completed")
        self.assertEqual(report["changed_files"][0]["path"], "src/app.py")
        self.assertEqual(report["changed_files"][0]["sha256"], "c" * 64)
        self.assertEqual(report["verification_status"], "partial")
        self.assertEqual(report["verification"]["resource_compliance"], "passed")
        self.assertTrue(report["needs_review"])
        self.assertEqual(report["verification"]["success_checks"][0]["status"], "not_run")
        self.assertEqual(report["verification"]["semantic_constraints"][0]["status"], "not_verified")
        self.assertEqual(report["verification"]["functional_correctness"], "not_independently_verified")

    def test_absolute_changed_file_path_fails_policy_verification(self):
        result = self._result()
        result["mutations"][0]["path"] = "/etc/passwd"
        report = build_handoff(result)
        self.assertEqual(report["verification"]["policy_compliance"], "failed")

    def test_bash_execution_under_contract_is_reported_as_policy_failure(self):
        result = self._result()
        result["bash"]["calls"] = 1
        result["bash"]["executed_calls"] = 1
        report = build_handoff(result)
        self.assertEqual(report["verification"]["policy_compliance"], "failed")
        self.assertEqual(report["verification_status"], "failed")
        self.assertTrue(report["needs_review"])


    def test_denied_bash_attempt_is_not_misreported_as_execution(self):
        state = BashOutcome()
        record_bash_result(
            state,
            "ERROR: tool 'Bash' is not allowed by the task contract",
            0,
        )
        self.assertEqual(state.attempts, 1)
        self.assertEqual(state.executed_calls, 0)

        result = self._result()
        result["bash"]["calls"] = state.attempts
        result["bash"]["executed_calls"] = state.executed_calls
        report = build_handoff(result)
        self.assertEqual(report["bash_summary"]["calls"], 1)
        self.assertEqual(report["bash_summary"]["executed_calls"], 0)
        self.assertEqual(report["verification"]["policy_compliance"], "passed")

    def test_edit_mutation_must_also_be_inside_read_scope(self):
        result = self._result()
        result["task_contract"]["allowed_tools"].append("Edit")
        result["task_contract"]["read_paths"] = ["tests/**"]
        result["mutations"][0]["tool"] = "Edit"
        report = build_handoff(result)
        self.assertEqual(report["verification"]["policy_compliance"], "failed")
        self.assertTrue(any("outside the effective read scope" in risk
                            for risk in report["verification"]["risks"]))

    def test_tool_call_limit_is_verified_in_handoff(self):
        result = self._result()
        result["tool_calls"] = 21
        report = build_handoff(result)
        self.assertEqual(report["verification"]["resource_compliance"], "failed")
        self.assertEqual(report["verification_status"], "failed")
        self.assertTrue(any("tool call count exceeded" in risk
                            for risk in report["verification"]["risks"]))

    def test_mutation_byte_limit_is_verified_in_handoff(self):
        result = self._result()
        result["mutation_bytes_reserved"] = 1025
        report = build_handoff(result)
        self.assertEqual(report["verification"]["resource_compliance"], "failed")
        self.assertEqual(report["verification_status"], "failed")
        self.assertTrue(any("reserved mutation bytes exceeded" in risk
                            for risk in report["verification"]["risks"]))

    def test_missing_resource_metrics_are_not_reported_as_verified(self):
        result = self._result()
        result.pop("mutation_bytes_reserved")
        report = build_handoff(result)
        self.assertEqual(report["verification"]["resource_compliance"], "not_verifiable")
        self.assertEqual(report["verification_status"], "partial")

    def test_policy_violation_is_reported_as_failed(self):
        result = self._result()
        result["mutations"][0]["path"] = "private/secret.py"
        report = build_handoff(result)
        self.assertEqual(report["verification"]["policy_compliance"], "failed")
        self.assertEqual(report["verification_status"], "failed")

    def test_mutation_record_accepts_valid_posix_colon_filename(self):
        record = mutation_record("d" * 32, "Write", "committed", path="src/name:variant.py", sha256="e" * 64)
        self.assertEqual(record.as_dict()["path"], "src/name:variant.py")

    def test_legacy_results_do_not_change_format(self):
        self.assertIsNone(build_handoff({"final_message": "legacy"}))
        formatted = format_sync_result({"final_message": "legacy", "turns_used": 1, "tool_calls": 0, "tokens": {"total": 1}, "duration_seconds": 1, "bash": {}})
        self.assertNotIn("[deepseek-mcp handoff]", formatted)

    def test_sync_format_includes_machine_readable_handoff(self):
        formatted = format_sync_result(self._result())
        encoded = formatted.split("[deepseek-mcp handoff]\n", 1)[1]
        report = json.loads(encoded)
        self.assertEqual(report["changed_files"][0]["path"], "src/app.py")


if __name__ == "__main__":
    unittest.main()
