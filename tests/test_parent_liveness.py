from __future__ import annotations

import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from deepseek_mcp import parent_liveness


class ParentLivenessWindowsModelTests(unittest.TestCase):
    def _kernel(self, wait_result: int):
        return SimpleNamespace(
            OpenProcess=Mock(return_value=0x1_0000_0001),
            WaitForSingleObject=Mock(return_value=wait_result),
            CloseHandle=Mock(return_value=True),
            GetTickCount64=Mock(return_value=1_000),
        )

    def test_windows_wait_preserves_pointer_sized_process_handle(self) -> None:
        kernel = self._kernel(0)
        with patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
            parent_liveness._windows_wait("pid:123", 1.0)

        kernel.WaitForSingleObject.assert_called_once()
        handle, milliseconds = kernel.WaitForSingleObject.call_args.args
        self.assertEqual(handle, 0x1_0000_0001)
        self.assertGreaterEqual(milliseconds, 1)
        self.assertLessEqual(milliseconds, 1000)
        kernel.CloseHandle.assert_called_once_with(0x1_0000_0001)

    def test_windows_wait_failure_fails_closed_without_long_retry(self) -> None:
        kernel = self._kernel(0xFFFFFFFF)
        with patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
            parent_liveness._windows_wait("pid:123", 3600.0)

        kernel.WaitForSingleObject.assert_called_once()
        kernel.CloseHandle.assert_called_once_with(0x1_0000_0001)


if __name__ == "__main__":
    unittest.main()
