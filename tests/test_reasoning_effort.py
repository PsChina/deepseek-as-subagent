from __future__ import annotations

import unittest

from deepseek_mcp.provider_child import _request_arguments


class ReasoningEffortRequestTests(unittest.TestCase):
    def _arguments(self, effort: str) -> dict:
        return _request_arguments(
            {"model": "configured-model", "reasoning_effort": effort},
            [{"role": "user", "content": "test"}],
            [],
        )

    def test_none_disables_thinking_without_invalid_reasoning_effort(self) -> None:
        arguments = self._arguments("none")
        self.assertEqual(arguments["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn("reasoning_effort", arguments)

    def test_low_high_and_max_enable_thinking_with_exact_effort(self) -> None:
        for effort in ("low", "high", "max"):
            with self.subTest(effort=effort):
                arguments = self._arguments(effort)
                self.assertEqual(arguments["reasoning_effort"], effort)
                self.assertEqual(
                    arguments["extra_body"], {"thinking": {"type": "enabled"}}
                )

    def test_missing_effort_keeps_previous_high_default(self) -> None:
        arguments = _request_arguments(
            {"model": "configured-model"},
            [{"role": "user", "content": "test"}],
            [],
        )
        self.assertEqual(arguments["reasoning_effort"], "high")
        self.assertEqual(arguments["extra_body"], {"thinking": {"type": "enabled"}})

    def test_unknown_effort_is_rejected_before_network_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "reasoning effort"):
            self._arguments("ultra")


if __name__ == "__main__":
    unittest.main()
