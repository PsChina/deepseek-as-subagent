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
