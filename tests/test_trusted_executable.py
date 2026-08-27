from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp.trusted_executable import (
    TrustedExecutableError,
    validate_trusted_executable,
)


@unittest.skipUnless(os.name == "posix", "POSIX ownership policy")
class TrustedExecutableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.bin = self.root / "bin"
        self.bin.mkdir(mode=0o700)
        self.executable = self.bin / "docker"
        self.executable.write_text("#!/bin/sh\n", encoding="utf-8")
        self.executable.chmod(0o700)

    def test_private_current_user_executable_is_accepted(self) -> None:
        self.assertEqual(
            validate_trusted_executable(self.executable, self.workspace),
            self.executable,
        )

    def test_group_writable_parent_or_executable_is_rejected(self) -> None:
        for target in (self.bin, self.executable):
            with self.subTest(target=target):
                target.chmod(0o770)
                with self.assertRaises(TrustedExecutableError):
                    validate_trusted_executable(self.executable, self.workspace)
                target.chmod(0o700)

    def test_foreign_owned_executable_is_rejected(self) -> None:
        with (
            patch("deepseek_mcp.trusted_executable.os.getuid", return_value=123456),
            self.assertRaisesRegex(TrustedExecutableError, "ownership|trusted"),
        ):
            validate_trusted_executable(self.executable, self.workspace)

    def test_lexical_workspace_symlink_to_trusted_target_is_rejected(self) -> None:
        link = self.workspace / "docker"
        link.symlink_to(self.executable)
        with self.assertRaisesRegex(TrustedExecutableError, "inside the workspace"):
            validate_trusted_executable(link, self.workspace)
