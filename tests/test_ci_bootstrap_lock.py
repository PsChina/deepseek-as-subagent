from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CiBootstrapLockTests(unittest.TestCase):
    def test_setuptools_patch_is_hash_locked_and_installed_before_local_audit(self) -> None:
        lock = (ROOT / "requirements-ci-bootstrap.lock").read_text(encoding="utf-8")
        self.assertIn("setuptools==83.0.0", lock)
        self.assertIn(
            "--hash=sha256:29b23c360f22f414dc7336bb39178cc7bcbf6021ed2733cde173f09dba19abb3",
            lock,
        )

        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        pin = workflow.index("-r requirements-ci-bootstrap.lock")
        audit = workflow.index("python -m pip_audit --local")
        self.assertLess(pin, audit)


if __name__ == "__main__":
    unittest.main()
