"""Private acceptance tests for the off-by-one coding task."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


def _candidate():
    path = Path("/workspace/src/task.py")
    spec = importlib.util.spec_from_file_location("candidate_task", path)
    if spec is None or spec.loader is None:
        raise AssertionError("candidate source is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HiddenOffByOneTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(_candidate().total([]), 0)

    def test_positive_and_negative_values(self):
        self.assertEqual(_candidate().total([2, 3, -1]), 4)

    def test_zero_and_repeated_values(self):
        self.assertEqual(_candidate().total([0, 0, 7, 7]), 14)


class SandboxBoundaryTests(unittest.TestCase):
    def test_tests_run_as_non_root_without_provider_credentials(self):
        import os
        self.assertNotEqual(os.geteuid(), 0)
        self.assertNotIn("DEEPSEEK_API_KEY", os.environ)

    def test_workspace_mount_is_read_only(self):
        with self.assertRaises(PermissionError):
            with Path("/workspace/src/task.py").open("ab") as handle:
                handle.write(b"tamper")

    def test_container_has_no_outbound_network(self):
        import socket
        with self.assertRaises(OSError):
            socket.create_connection(("1.1.1.1", 443), timeout=1)
