"""Opt-in smoke test against a real DeepSeek-compatible endpoint.

Run only in a trusted local environment with DEEPSEEK_LIVE_TEST=1 and
DEEPSEEK_API_KEY already set. CI intentionally does not supply credentials.
"""
from __future__ import annotations

import os
import secrets
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp.agent_loop import run_agent
from deepseek_mcp.config import Config
from deepseek_mcp.policy_engine import bind_task_contract


@unittest.skipUnless(
    os.environ.get("DEEPSEEK_LIVE_TEST") == "1",
    "live provider test requires explicit DEEPSEEK_LIVE_TEST=1",
)
class LiveDeepSeekContractSmokeTests(unittest.TestCase):
    def test_readonly_task_contract_against_real_api(self) -> None:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            self.skipTest("DEEPSEEK_API_KEY is not configured")
        model = os.environ.get("DEEPSEEK_LIVE_MODEL", "deepseek-chat")
        endpoint = os.environ.get("DEEPSEEK_LIVE_BASE_URL", "https://api.deepseek.com")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            (workspace / "src").mkdir()
            marker = "CONTRACT_LIVE_PROBE_" + secrets.token_hex(12)
            (workspace / "src" / "probe.txt").write_text(
                marker, encoding="utf-8"
            )
            config = Config(
                api_key=api_key, workspace=workspace, model=model,
                base_url=endpoint, allowed_tools=["Read"],
                delegation_capability="readonly", max_turns=3,
                max_output_tokens=384, max_run_seconds=120,
            )
            bind_task_contract(config, {
                "intent": "readonly_scan",
                "permissions": {
                    "tools": ["Read"],
                    "read_paths": ["src/**"],
                },
                "limits": {
                    "max_tool_calls": 2,
                    "max_mutation_bytes": 1024,
                },
            })
            result = run_agent(
                "Use the Read tool to open src/probe.txt. Then include the exact "
                "marker from that file in your final answer. Do not alter any files.",
                config,
            )
            self.assertFalse((workspace / "src" / "unexpected.txt").exists())
        self.assertIn(marker, result["final_message"])
        self.assertGreaterEqual(result["tool_calls"], 1)
        self.assertGreaterEqual(result["turns_used"], 2)
        self.assertEqual(result["mutations"], [])
        self.assertEqual(result["task_contract"]["intent"], "readonly_scan")
        self.assertEqual(result["bash"]["executed_calls"], 0)


if __name__ == "__main__":
    unittest.main()
