from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CiBootstrapLockTests(unittest.TestCase):
    def test_setuptools_patch_is_hash_locked_and_installed_before_local_audit(self) -> None:
        lock = (ROOT / "requirements-ci-bootstrap.lock").read_text(encoding="utf-8")
        self.assertIn("setuptools==84.1.0", lock)
        self.assertIn(
            "--hash=sha256:5231689c6ac8156cd155cfb98c3fd6fb252c3e7e9bd3d78b64d3c55f132624c9",
            lock,
        )

        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        pin = workflow.index("-r requirements-ci-bootstrap.lock")
        audit = workflow.index('"$AUDIT_PY" -m pip_audit --local')
        self.assertLess(pin, audit)


if __name__ == "__main__":
    unittest.main()
