from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp.config import (
    Config,
    DEFAULT_FLASH_MODEL,
    DEFAULT_PRO_MODEL,
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

    def test_flash_and_pro_have_official_defaults(self) -> None:
        config = self._load_from_data({})
        self.assertEqual(config.flash_model, DEFAULT_FLASH_MODEL)
        self.assertEqual(config.pro_model, DEFAULT_PRO_MODEL)
        self.assertEqual(config.model, DEFAULT_FLASH_MODEL)

    def test_flash_and_pro_accept_user_provider_model_names(self) -> None:
        config = self._load_from_data(
            {"flash": "deepseek-v4.1-flash", "pro": "deepseek-v4.1-pro"}
        )
        self.assertEqual(config.flash_model, "deepseek-v4.1-flash")
        self.assertEqual(config.pro_model, "deepseek-v4.1-pro")
        self.assertEqual(config.model, "deepseek-v4.1-flash")

    def test_legacy_single_model_maps_to_both_slots(self) -> None:
        config = self._load_from_data({"model": "vendor-legacy-model"})
        self.assertEqual(config.flash_model, "vendor-legacy-model")
        self.assertEqual(config.pro_model, "vendor-legacy-model")
        self.assertEqual(config.model, "vendor-legacy-model")

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
