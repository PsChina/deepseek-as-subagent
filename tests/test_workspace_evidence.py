"""Host-side readback tests with actual workspace files and identity guards."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp.config import Config, DEFAULT_ALLOWED_TOOLS
from deepseek_mcp.policy_engine import compile_task_policy
from deepseek_mcp.task_contract import TaskContract
from deepseek_mcp.workspace_evidence import collect_workspace_evidence
from deepseek_mcp.verification import verify_execution_record


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class WorkspaceEvidenceTests(unittest.TestCase):
    def _policy(self):
        contract = TaskContract.from_dict({
            "permissions": {
                "tools": ["Read", "Write"],
                "read_paths": ["src/**"],
                "write_paths": ["src/**"],
            }
        })
        return compile_task_policy(contract, DEFAULT_ALLOWED_TOOLS)

    def _config(self, root: Path) -> Config:
        policy = self._policy()
        return Config(
            "", root, allowed_tools=list(policy.allowed_tools),
            effective_policy=policy,
        )

    def _record(self, content: str, path: str = "src/file.txt") -> dict:
        return {
            "tool": "Write", "path": path, "status": "committed",
            "transaction_id": "a" * 32, "sha256": _digest(content),
        }

    def test_actual_file_readback_detects_modification_and_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            target = root / "src" / "file.txt"
            target.write_text("committed", encoding="utf-8")
            cfg = self._config(root)
            record = self._record("committed")
            matched = collect_workspace_evidence(cfg, [record])
            self.assertEqual(matched[0]["status"], "matched")
            self.assertEqual(matched[0]["observed_sha256"], _digest("committed"))

            target.write_text("modified by another process", encoding="utf-8")
            changed = collect_workspace_evidence(cfg, [record])
            self.assertEqual(changed[0]["status"], "changed")
            verification = verify_execution_record(
                cfg.effective_policy.to_dict(), [record], 0, 1, 30, changed
            )
            self.assertEqual(verification["workspace_readback"], "changed")
            self.assertEqual(verification["overall_status"], "partial")

            target.unlink()
            missing = collect_workspace_evidence(cfg, [record])
            self.assertEqual(missing[0]["status"], "not_verifiable")
            self.assertNotIn("observed_sha256", missing[0])

    def test_readback_rejects_symlink_alias_to_out_of_scope_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "private").mkdir()
            secret = root / "private" / "secret.txt"
            secret.write_text("private-only", encoding="utf-8")
            alias = root / "src" / "file.txt"
            try:
                alias.symlink_to(secret)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            cfg = self._config(root)
            evidence = collect_workspace_evidence(cfg, [self._record("private-only")])
            self.assertEqual(evidence[0]["status"], "not_verifiable")
            self.assertNotIn("observed_sha256", evidence[0])

    def test_last_committed_version_is_checked_after_multiple_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "file.txt").write_text("second", encoding="utf-8")
            cfg = self._config(root)
            earlier, latest = self._record("first"), self._record("second")
            latest["transaction_id"] = "b" * 32
            checks = collect_workspace_evidence(cfg, [earlier, latest])
            self.assertEqual(len(checks), 1)
            self.assertEqual(checks[0]["expected_sha256"], _digest("second"))
            self.assertEqual(checks[0]["status"], "matched")

    def test_out_of_scope_paths_are_never_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "private.txt").write_text("sensitive", encoding="utf-8")
            cfg = self._config(root)
            record = self._record("sensitive", path="private.txt")
            evidence = collect_workspace_evidence(cfg, [record])
            self.assertEqual(evidence[0]["status"], "not_verifiable")
            self.assertNotIn("observed_sha256", evidence[0])

    def test_handoff_evidence_digest_must_match_mutation_record(self) -> None:
        contract = self._policy().to_dict()
        record = self._record("expected")
        evidence = [{
            "path": "src/file.txt", "expected_sha256": _digest("other"),
            "observed_sha256": _digest("other"), "status": "matched",
        }]
        result = verify_execution_record(contract, [record], 0, 1, 8, evidence)
        self.assertEqual(result["workspace_readback"], "failed")
        self.assertEqual(result["overall_status"], "failed")

    def test_duplicate_or_missing_readback_cannot_be_passed(self) -> None:
        contract = self._policy().to_dict()
        record = self._record("expected")
        checked = {
            "path": "src/file.txt",
            "expected_sha256": _digest("expected"),
            "observed_sha256": _digest("expected"),
            "status": "matched",
        }
        for evidence in ([], [checked, checked]):
            with self.subTest(evidence=evidence):
                result = verify_execution_record(contract, [record], 0, 1, 8, evidence)
                self.assertEqual(result["workspace_readback"], "not_verifiable")
                self.assertEqual(result["overall_status"], "partial")


if __name__ == "__main__":
    unittest.main()
