from __future__ import annotations

import unittest
from unittest.mock import patch

from deepseek_mcp.hard_deadline import HardDeadline, limited, remaining


class HardDeadlineTests(unittest.TestCase):
    def test_realtime_clock_expires_run_when_monotonic_pauses_during_sleep(self) -> None:
        deadline = HardDeadline(monotonic_end=110.0, realtime_end=1010.0)
        with (
            patch("deepseek_mcp.hard_deadline.time.monotonic", return_value=101.0),
            patch("deepseek_mcp.hard_deadline.time.time", return_value=1011.0),
        ):
            self.assertLessEqual(remaining(deadline), 0)

    def test_monotonic_clock_still_prevents_wall_clock_rollback_extension(self) -> None:
        deadline = HardDeadline(monotonic_end=110.0, realtime_end=1010.0)
        with (
            patch("deepseek_mcp.hard_deadline.time.monotonic", return_value=111.0),
            patch("deepseek_mcp.hard_deadline.time.time", return_value=900.0),
        ):
            self.assertLessEqual(remaining(deadline), 0)

    def test_suspend_and_realtime_rollback_cannot_jointly_extend_run(self) -> None:
        deadline = HardDeadline(110.0, 1010.0, continuous_end=210.0)
        with (
            patch("deepseek_mcp.hard_deadline.time.monotonic", return_value=101.0),
            patch("deepseek_mcp.hard_deadline.time.time", return_value=900.0),
            patch("deepseek_mcp.hard_deadline.continuous_time", return_value=211.0),
        ):
            self.assertLessEqual(remaining(deadline), 0)

    def test_request_limit_cannot_extend_existing_hard_deadline(self) -> None:
        run = HardDeadline(monotonic_end=110.0, realtime_end=1010.0)
        with (
            patch("deepseek_mcp.hard_deadline.time.monotonic", return_value=100.0),
            patch("deepseek_mcp.hard_deadline.time.time", return_value=1000.0),
        ):
            request, run_limited = limited(run, 20.0)

        self.assertIs(request, run)
        self.assertTrue(run_limited)


if __name__ == "__main__":
    unittest.main()
