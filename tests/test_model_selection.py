from __future__ import annotations

import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deepseek_mcp import server
from deepseek_mcp.config import (
    Config,
    DEFAULT_FLASH_MODEL,
    DEFAULT_MODEL,
    DEFAULT_PRO_MODEL,
)
from deepseek_mcp.execution_profile import CODING_PROFILE, READONLY_PROFILE
from deepseek_mcp.model_selection import resolve_model


class ModelSelectionTests(unittest.TestCase):
    def _config(self, workspace: Path) -> Config:
        return Config("sk-test", workspace, allowed_tools=["Read"])

    def test_config_and_public_tools_default_to_flash(self) -> None:
        self.assertEqual(DEFAULT_MODEL, DEFAULT_FLASH_MODEL)
        self.assertEqual(DEFAULT_FLASH_MODEL, "deepseek-v4-flash")
        self.assertEqual(DEFAULT_PRO_MODEL, "deepseek-v4-pro")
        for tool in (
            server.delegate_to_deepseek,
            server.delegate_to_deepseek_readonly,
            server.start_deepseek,
            server.start_deepseek_readonly,
        ):
            parameter = inspect.signature(tool).parameters["model"]
            self.assertEqual(parameter.default, "flash")

    def test_model_profiles_resolve_user_configured_provider_ids(self) -> None:
        self.assertEqual(
            resolve_model(
                "flash",
                flash_model="vendor-fast-model",
                pro_model="vendor-smart-model",
            ),
            "vendor-fast-model",
        )
        self.assertEqual(
            resolve_model(
                "pro",
                flash_model="vendor-fast-model",
                pro_model="vendor-smart-model",
            ),
            "vendor-smart-model",
        )
        with self.assertRaisesRegex(ValueError, "flash.*pro"):
            resolve_model(
                "other",
                flash_model="vendor-fast-model",
                pro_model="vendor-smart-model",
            )

    def test_load_config_uses_configured_flash_profile_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(Path(tmpdir))
            config.flash_model = "custom-flash"
            config.pro_model = "custom-pro"
            config.flash_reasoning_effort = "low"
            config.pro_reasoning_effort = "max"
            with (
                patch.object(server.Config, "load", return_value=config),
                patch.object(server, "configure_delegation", side_effect=lambda cfg, _profile: cfg),
            ):
                loaded = server._load_config(CODING_PROFILE)

        self.assertIs(loaded, config)
        self.assertEqual(loaded.model, "custom-flash")
        self.assertEqual(loaded.reasoning_effort, "low")

    def test_load_config_can_select_configured_pro_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(Path(tmpdir))
            config.flash_model = "custom-flash"
            config.pro_model = "custom-pro"
            config.flash_reasoning_effort = "none"
            config.pro_reasoning_effort = "max"
            with (
                patch.object(server.Config, "load", return_value=config),
                patch.object(server, "configure_delegation", side_effect=lambda cfg, _profile: cfg),
            ):
                loaded = server._load_config(READONLY_PROFILE, "pro")

        self.assertEqual(loaded.model, "custom-pro")
        self.assertEqual(loaded.reasoning_effort, "max")

    def test_background_job_receives_selected_model_once(self) -> None:
        manager = Mock()
        manager.start.return_value = {"job_id": "job-1", "status": "running"}
        config = Mock(model="custom-pro", reasoning_effort="max")
        with (
            patch.object(server, "job_manager", manager),
            patch.object(server, "_load_config", return_value=config) as load_config,
        ):
            payload = server.start_deepseek("hard task", model="pro")

        self.assertIn('"ok": true', payload)
        load_config.assert_called_once_with(CODING_PROFILE, "pro")
        manager.start.assert_called_once_with("hard task", "", config)
        self.assertNotIn("model", inspect.signature(server.send_deepseek_message).parameters)


if __name__ == "__main__":
    unittest.main()
