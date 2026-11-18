from __future__ import annotations

import unittest

from pydantic import ValidationError

from deepseek_mcp.contract_schema import TaskContractInput, contract_payload


class ContractSchemaTests(unittest.TestCase):
    def test_payload_preserves_only_explicit_contract_fields(self):
        payload = contract_payload({
            "permissions": {
                "tools": ["Read", "Write"],
                "read_paths": ["src/**"],
                "write_paths": ["src/**"],
            },
            "constraints": {"success_checks": ["pytest tests/"]},
            "limits": {"max_tool_calls": 12},
        })
        self.assertEqual(payload["permissions"]["tools"], ["Read", "Write"])
        self.assertEqual(payload["permissions"]["write_paths"], ["src/**"])
        self.assertEqual(payload["constraints"], {"success_checks": ["pytest tests/"]})
        self.assertEqual(payload["limits"], {"max_tool_calls": 12})
        self.assertNotIn("version", payload)

    def test_model_instance_and_mapping_have_same_payload(self):
        raw = {"intent": "review", "permissions": {"read_paths": ["src/**"]}}
        self.assertEqual(contract_payload(TaskContractInput.model_validate(raw)), contract_payload(raw))

    def test_schema_rejects_bash_unknown_fields_and_raised_limits(self):
        with self.assertRaises(ValidationError):
            contract_payload({"permissions": {"tools": ["Bash"]}})
        with self.assertRaises(ValidationError):
            contract_payload({"unexpected": True})
        with self.assertRaises(ValidationError):
            contract_payload({"limits": {"max_tool_calls": 129}})

    def test_schema_does_not_coerce_versions_or_replace_explicit_null_scopes(self):
        for raw in (
            {"version": True},
            {"version": 1.0},
            {"permissions": {"read_paths": None}},
            {"permissions": {"write_paths": None}},
            {"permissions": {"deny_paths": None}},
            {"limits": {"max_tool_calls": None}},
        ):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                contract_payload(raw)
        self.assertEqual(contract_payload({"version": 1}), {"version": 1})
        self.assertEqual(
            contract_payload({"permissions": {"read_paths": []}}),
            {"permissions": {"read_paths": []}},
        )


if __name__ == "__main__":
    unittest.main()
