from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp.config import (
    Config,
    DEFAULT_FLASH_MODEL,
    DEFAULT_PRO_MODEL,
    DEFAULT_REASONING_EFFORT,
    PROVIDER_DEFAULT_REASONING_EFFORT,
    REASONING_EFFORT_OPTIONS,
)


class ConfigModelSlotTests(unittest.TestCase):
    def _load_from_data(self, data: dict) -> Config:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            with (
                patch("deepseek_mcp.config._load_data", return_value=data),
                patch("deepseek_mcp.config._load_api_key", return_value="sk-test"),
                patch("deepseek_mcp.config._load_workspace", return_value=workspace),
            ):
                return Config.load()

    def test_flash_and_pro_have_official_model_defaults(self) -> None:
        config = self._load_from_data({})
        self.assertEqual(config.flash_model, DEFAULT_FLASH_MODEL)
        self.assertEqual(config.pro_model, DEFAULT_PRO_MODEL)
        self.assertEqual(config.model, DEFAULT_FLASH_MODEL)
        self.assertEqual(DEFAULT_REASONING_EFFORT, "high")
        self.assertEqual(
            config.flash_reasoning_effort, PROVIDER_DEFAULT_REASONING_EFFORT
        )
        self.assertEqual(
            config.pro_reasoning_effort, PROVIDER_DEFAULT_REASONING_EFFORT
        )
        self.assertEqual(config.reasoning_effort, PROVIDER_DEFAULT_REASONING_EFFORT)
        self.assertEqual(REASONING_EFFORT_OPTIONS, ("none", "low", "high", "max"))

    def test_flash_and_pro_accept_user_provider_model_names_and_efforts(self) -> None:
        config = self._load_from_data(
            {
                "flash": "deepseek-v4.1-flash",
                "flash_reasoning_effort": "none",
                "pro": "deepseek-v4.1-pro",
                "pro_reasoning_effort": "max",
                "_reasoning_effort_options": ["none", "low", "high", "max"],
            }
        )
        self.assertEqual(config.flash_model, "deepseek-v4.1-flash")
        self.assertEqual(config.pro_model, "deepseek-v4.1-pro")
        self.assertEqual(config.model, "deepseek-v4.1-flash")
        self.assertEqual(config.flash_reasoning_effort, "none")
        self.assertEqual(config.pro_reasoning_effort, "max")
        self.assertEqual(config.reasoning_effort, "none")

    def test_reasoning_effort_can_be_configured_per_slot_independently(self) -> None:
        config = self._load_from_data({"flash_reasoning_effort": "low"})
        self.assertEqual(config.flash_reasoning_effort, "low")
        self.assertEqual(
            config.pro_reasoning_effort, PROVIDER_DEFAULT_REASONING_EFFORT
        )

    def test_reasoning_effort_hint_field_is_runtime_irrelevant(self) -> None:
        config = self._load_from_data(
            {
                "_reasoning_effort_options": "documentation-only",
                "flash_reasoning_effort": "low",
                "pro_reasoning_effort": "high",
            }
        )
        self.assertEqual(config.flash_reasoning_effort, "low")
        self.assertEqual(config.pro_reasoning_effort, "high")

    def test_reasoning_effort_rejects_unknown_values(self) -> None:
        for field in ("flash_reasoning_effort", "pro_reasoning_effort"):
            with self.subTest(field=field), self.assertRaisesRegex(
                RuntimeError, "none, low, high, max"
            ):
                self._load_from_data({field: "ultra"})

    def test_internal_provider_default_marker_is_not_a_user_option(self) -> None:
        for field in ("flash_reasoning_effort", "pro_reasoning_effort"):
            with self.subTest(field=field), self.assertRaisesRegex(
                RuntimeError, "none, low, high, max"
            ):
                self._load_from_data({field: PROVIDER_DEFAULT_REASONING_EFFORT})

    def test_legacy_single_model_maps_to_both_slots_without_new_reasoning_fields(self) -> None:
        config = self._load_from_data({"model": "vendor-legacy-model"})
        self.assertEqual(config.flash_model, "vendor-legacy-model")
        self.assertEqual(config.pro_model, "vendor-legacy-model")
        self.assertEqual(config.model, "vendor-legacy-model")
        self.assertEqual(config.reasoning_effort, PROVIDER_DEFAULT_REASONING_EFFORT)

    def test_legacy_model_cannot_be_mixed_with_new_slots(self) -> None:
        for data in (
            {"model": "legacy", "flash": "fast"},
            {"model": "legacy", "pro": "smart"},
        ):
            with self.subTest(data=data), self.assertRaisesRegex(
                RuntimeError, "cannot be combined"
            ):
                self._load_from_data(data)

    def test_model_slot_values_are_strict_strings(self) -> None:
        for data in (
            {"flash": ""},
            {"pro": " pro-model "},
            {"flash": 123},
        ):
            with self.subTest(data=data), self.assertRaisesRegex(
                RuntimeError, "flash|pro"
            ):
                self._load_from_data(data)


if __name__ == "__main__":
    unittest.main()
