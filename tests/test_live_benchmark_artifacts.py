"""Strict schema tests for the split live benchmark artifact."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.verify_live_code_benchmark import _decode_record, _read_artifact


class LiveBenchmarkArtifactTests(unittest.TestCase):
    def _record(self):
        source = b"def total(values):\n    return sum(values)\n"
        return {
            "task_id": "repair-off-by-one",
            "repeat": 1,
            "agent_status": "completed",
            "policy_passed": True,
            "contract_sha256": "a" * 64,
            "source_b64": base64.b64encode(source).decode("ascii"),
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "duration_ms": 1234,
            "tool_calls": 3,
            "tokens": {"prompt": 100, "completion": 20, "total": 120},
        }

    def test_valid_record_round_trips_exact_source_bytes(self):
        raw = self._record()
        parsed, source = _decode_record(raw)
        self.assertEqual(parsed["task_id"], "repair-off-by-one")
        self.assertEqual(source, b"def total(values):\n    return sum(values)\n")

    def test_unknown_task_bad_base64_and_digest_tampering_are_rejected(self):
        raw = self._record()
        raw["task_id"] = "../../outside"
        with self.assertRaisesRegex(ValueError, "task ID"):
            _decode_record(raw)
        raw = self._record()
        raw["source_b64"] = "%%%not-base64%%%"
        with self.assertRaisesRegex(ValueError, "base64"):
            _decode_record(raw)
        raw = self._record()
        raw["source_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            _decode_record(raw)

    def test_unknown_fields_and_duplicate_json_keys_are_rejected(self):
        raw = self._record()
        raw["path"] = "../outside"
        with self.assertRaisesRegex(ValueError, "schema"):
            _decode_record(raw)
        duplicate = '{"schema_version":1,"schema_version":1}'
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.json"
            path.write_text(duplicate, encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "duplicate JSON keys"):
                    _read_artifact(path)

    def test_artifact_requires_exact_task_repeat_matrix(self):
        artifact = {
            "schema_version": 1,
            "created_at": "2026-11-14T00:00:00+00:00",
            "provider": "api.deepseek.com",
            "model": "deepseek-chat",
            "repeats_per_task": 1,
            "records": [self._record()],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "record count"):
                    _read_artifact(path)
            artifact["records"].append(dict(
                self._record(), task_id="repair-case-normalization"
            ))
            path.write_text(json.dumps(artifact), encoding="utf-8")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "must-not-exist"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "must not receive"):
                    _read_artifact(path)
            with patch.dict(os.environ, {}, clear=True):
                parsed = _read_artifact(path)
            self.assertEqual(len(parsed["records"]), 2)


if __name__ == "__main__":
    unittest.main()
