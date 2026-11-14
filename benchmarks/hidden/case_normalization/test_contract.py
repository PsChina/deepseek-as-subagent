"""Private acceptance tests for the string-normalization coding task."""
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


class HiddenNormalizationTests(unittest.TestCase):
    def test_trims_and_lowercases_ascii(self):
        self.assertEqual(_candidate().slug(" HeLLo "), "hello")

    def test_handles_empty_and_whitespace(self):
        self.assertEqual(_candidate().slug("   "), "")

    def test_does_not_remove_internal_spaces(self):
        self.assertEqual(_candidate().slug("A B"), "a b")
