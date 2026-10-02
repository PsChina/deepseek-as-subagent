"""Exercise a real local model in a disposable workspace.

Run with the project's installed Python environment. No user files are delegated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from deepseek_mcp.agent_loop import run_agent
from deepseek_mcp.config import Config, _is_loopback_endpoint, _load_api_key
from deepseek_mcp.transaction_recovery import acknowledge_with_lease, query_with_lease


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-output-tokens", type=int, default=1024)
    args = parser.parse_args()
    if not _is_loopback_endpoint(args.base_url):
        parser.error("this smoke test requires a loopback endpoint")
    with tempfile.TemporaryDirectory(prefix="local-subagent-smoke-") as tmp:
        workspace = Path(tmp).resolve()
        (workspace / "input.txt").write_text("original-value\n", encoding="utf-8")
        # Construct directly so DEEPSEEK_WORKSPACE cannot redirect this smoke test.
        config = Config(
            api_key=_load_api_key({}, args.base_url), workspace=workspace,
            base_url=args.base_url, model=args.model,
            flash_model=args.model, pro_model=args.model,
            max_output_tokens=args.max_output_tokens,
            max_turns=10, max_run_seconds=180,
            allowed_tools=["Read", "Edit", "Bash"],
        )
        observed: list[str] = []
        outputs: list[object] = []

        # Capture the actual dispatched tools while retaining the real implementation.
        from unittest.mock import patch
        from deepseek_mcp import agent_loop
        original = agent_loop.execute_in_subprocess

        def execute(tool_config, name, *positional, **keyword):
            observed.append(name)
            output = original(tool_config, name, *positional, **keyword)
            if name == "Bash":
                outputs.append(output)
            return output

        with patch.object(agent_loop, "execute_in_subprocess", side_effect=execute):
            result = run_agent(
                "Use Read to inspect input.txt. Use Edit to replace original-value "
                "with verified-local-value, preserving the newline. Then use Bash "
                "to run this exact verification command: "
                "test \"$(cat input.txt)\" = verified-local-value && printf 'LOCAL_SMOKE_OK\\n'. "
                "Only after the command succeeds, return LOCAL_SMOKE_OK in your final message.",
                config,
            )
        if (workspace / "input.txt").read_text(encoding="utf-8") != "verified-local-value\n":
            raise RuntimeError("model did not produce the expected file contents")
        if not all(name in observed for name in ("Read", "Edit", "Bash")):
            raise RuntimeError(f"model skipped a required tool: {observed}")
        if "LOCAL_SMOKE_OK" not in result["final_message"]:
            raise RuntimeError("model did not report successful verification")
        if not any("LOCAL_SMOKE_OK" in str(output) for output in outputs):
            raise RuntimeError("verification command did not return its success marker")
        pending = query_with_lease(config)
        digest = hashlib.sha256((workspace / "input.txt").read_bytes()).hexdigest()
        if not pending or any(record["path"] != "input.txt" or record["sha256"] != digest
                              or record["status"] != "committed" for record in pending):
            raise RuntimeError("mutation recovery records did not match the verified file")
        _, remaining = acknowledge_with_lease(
            config, [str(record["transaction_id"]) for record in pending]
        )
        if remaining:
            raise RuntimeError("verified mutations were not acknowledged")
        readonly_config = replace(config, allowed_tools=["Read"], delegation_capability="readonly")
        coding_tools = observed.copy()
        observed.clear()
        with patch.object(agent_loop, "execute_in_subprocess", side_effect=execute):
            readonly_result = run_agent(
                "Use Read to inspect input.txt, then return its exact contents. Do not edit files.",
                readonly_config,
            )
        if not observed or set(observed) != {"Read"} or "verified-local-value" not in readonly_result["final_message"]:
            raise RuntimeError("read-only model delegation did not read and report the file")
        print(json.dumps({"status": "passed", "model": args.model,
                          "coding_tools": coding_tools,
                          "readonly_tools": observed, "result": result,
                          "readonly_result": readonly_result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
