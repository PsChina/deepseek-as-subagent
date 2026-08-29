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
from deepseek_mcp.config import Config, DEFAULT_MODEL
from deepseek_mcp.execution_profile import CODING_PROFILE, READONLY_PROFILE
from deepseek_mcp.job_manager import JobError


class ModelSelectionTests(unittest.TestCase):
    def _config(self, workspace: Path) -> Config:
        return Config("sk-test", workspace, allowed_tools=["Read"])

    def test_config_and_public_tools_default_to_flash(self) -> None:
        self.assertEqual(DEFAULT_MODEL, "deepseek-v4-flash")
        for tool in (
            server.delegate_to_deepseek,
            server.delegate_to_deepseek_readonly,
            server.start_deepseek,
            server.start_deepseek_readonly,
        ):
            parameter = inspect.signature(tool).parameters["model"]
            self.assertEqual(parameter.default, "flash")

    def test_model_aliases_are_exact(self) -> None:
        self.assertEqual(server._resolve_model("flash"), "deepseek-v4-flash")
        self.assertEqual(server._resolve_model("pro"), "deepseek-v4-pro")
        with self.assertRaisesRegex(JobError, "flash.*pro"):
            server._resolve_model("other")

    def test_load_config_overrides_legacy_config_model_with_flash_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(Path(tmpdir))
            config.model = "legacy-configured-model"
            with (
                patch.object(server.Config, "load", return_value=config),
                patch.object(server, "configure_delegation", side_effect=lambda cfg, _profile: cfg),
            ):
                loaded = server._load_config(CODING_PROFILE)

        self.assertIs(loaded, config)
        self.assertEqual(loaded.model, "deepseek-v4-flash")

    def test_load_config_can_select_pro(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(Path(tmpdir))
            with (
                patch.object(server.Config, "load", return_value=config),
                patch.object(server, "configure_delegation", side_effect=lambda cfg, _profile: cfg),
            ):
                loaded = server._load_config(READONLY_PROFILE, "pro")

        self.assertEqual(loaded.model, "deepseek-v4-pro")

    def test_background_job_receives_selected_model_once(self) -> None:
        manager = Mock()
        manager.start.return_value = {"job_id": "job-1", "status": "running"}
        config = Mock(model="deepseek-v4-pro")
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
