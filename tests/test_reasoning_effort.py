from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from deepseek_mcp.config import PROVIDER_DEFAULT_REASONING_EFFORT
from deepseek_mcp.provider_child import _request_arguments
from deepseek_mcp.provider_process import _encoded_request


class ReasoningEffortRequestTests(unittest.TestCase):
    messages = [{"role": "user", "content": "test"}]

    def _arguments(self, effort: str) -> dict:
        return _request_arguments(
            {"model": "configured-model", "reasoning_effort": effort,
             "base_url": "https://api.deepseek.com"},
            self.messages,
            [],
        )

    def _encoded_settings(self, *, effort: str | None = None, include: bool = True) -> dict:
        config = SimpleNamespace(
            api_key="sk-test",
            base_url="https://api.deepseek.com",
            model="configured-model",
        )
        if include:
            config.reasoning_effort = effort
        payload = json.loads(_encoded_request(config, self.messages, []))
        return payload["settings"]

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

    def test_missing_effort_keeps_legacy_request_shape(self) -> None:
        settings = self._encoded_settings(include=False)
        self.assertNotIn("reasoning_effort", settings)
        arguments = _request_arguments(settings, self.messages, [])
        self.assertNotIn("reasoning_effort", arguments)
        self.assertNotIn("extra_body", arguments)

    def test_provider_default_marker_keeps_legacy_request_shape(self) -> None:
        settings = self._encoded_settings(effort=PROVIDER_DEFAULT_REASONING_EFFORT)
        self.assertNotIn("reasoning_effort", settings)
        arguments = _request_arguments(settings, self.messages, [])
        self.assertNotIn("reasoning_effort", arguments)
        self.assertNotIn("extra_body", arguments)

    def test_explicit_effort_survives_parent_child_request_boundary(self) -> None:
        for effort in ("none", "low", "high", "max"):
            with self.subTest(effort=effort):
                settings = self._encoded_settings(effort=effort)
                self.assertEqual(settings["reasoning_effort"], effort)
                arguments = _request_arguments(settings, self.messages, [])
                if effort == "none":
                    self.assertNotIn("reasoning_effort", arguments)
                    self.assertEqual(
                        arguments["extra_body"], {"thinking": {"type": "disabled"}}
                    )
                else:
                    self.assertEqual(arguments["reasoning_effort"], effort)
                    self.assertEqual(
                        arguments["extra_body"], {"thinking": {"type": "enabled"}}
                    )

    def test_unknown_effort_is_rejected_before_network_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "reasoning effort"):
            self._arguments("ultra")

    def test_generic_provider_never_receives_deepseek_thinking_extension(self) -> None:
        for effort in ("none", "low", "high", "max"):
            with self.subTest(effort=effort):
                arguments = _request_arguments(
                    {"model": "local", "base_url": "http://localhost:1234/v1",
                     "reasoning_effort": effort}, self.messages, [],
                )
                self.assertEqual(arguments["reasoning_effort"], effort)
                self.assertNotIn("extra_body", arguments)

    def test_output_limit_survives_parent_child_boundary(self) -> None:
        config = SimpleNamespace(api_key="local-no-auth", model="local",
                                 base_url="http://localhost:1234/v1",
                                 max_output_tokens=512)
        settings = json.loads(_encoded_request(config, self.messages, []))["settings"]
        self.assertEqual(_request_arguments(settings, self.messages, [])["max_tokens"], 512)

    def test_child_rejects_invalid_output_limit_before_network_call(self) -> None:
        for value in (0, -1, 16385, True, "1024"):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "max_output_tokens"):
                _request_arguments({"model": "local", "max_output_tokens": value}, self.messages, [])

    def test_deepseek_named_custom_host_does_not_receive_thinking_extension(self) -> None:
        arguments = _request_arguments(
            {"model": "local", "base_url": "https://api.deepseek.com.example.org/v1",
             "reasoning_effort": "high"}, self.messages, [],
        )
        self.assertNotIn("extra_body", arguments)


if __name__ == "__main__":
    unittest.main()
