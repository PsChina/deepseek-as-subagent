"""Verify a local model through a real MCP stdio host in a disposable HOME.

Run with the project's Python environment; no persistent user configuration is used.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from deepseek_mcp.config import _is_loopback_endpoint


async def verify(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="local-mcp-host-") as directory:
        root = Path(directory).resolve()
        home, workspace = root / "home", root / "workspace"
        private = home / ".deepseek-mcp"
        private.mkdir(parents=True, mode=0o700)
        workspace.mkdir()
        target = workspace / "input.txt"
        target.write_text("original-value\n", encoding="utf-8")
        config = {"workspace": str(workspace), "base_url": args.base_url,
                  "flash": args.model, "pro": args.model,
                  "flash_reasoning_effort": "provider-default",
                  "pro_reasoning_effort": "provider-default",
                  "max_output_tokens": args.max_output_tokens,
                  "max_turns": 12, "max_run_seconds": 240,
                  "allowed_tools": ["Read", "Edit", "Bash"]}
        config_path = private / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        config_path.chmod(0o600)
        # Pass an explicit allowlist: credentials, mode, workspace, proxy, and
        # Python startup overrides from the invoking environment cannot leak in.
        environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR")
                       if key in os.environ}
        environment.update(HOME=str(home), USERPROFILE=str(home),
                           PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        parameters = StdioServerParameters(command=sys.executable,
                                          args=["-m", "deepseek_mcp.server"],
                                          env=environment)
        evidence: dict = {"model": args.model, "base_url": args.base_url}
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=300)) as session:
                initialized = await session.initialize()
                evidence["server"] = initialized.serverInfo.model_dump()
                listing = await session.list_tools()
                evidence["tools"] = [tool.name for tool in listing.tools]

                async def call(name: str, arguments: dict | None = None, *, structured=False):
                    response = await session.call_tool(name, arguments or {})
                    assert not response.isError, (name, response)
                    text = "\n".join(block.text for block in response.content if block.type == "text")
                    print(json.dumps({"tool": name, "response": text}, ensure_ascii=False), flush=True)
                    if structured:
                        payload = json.loads(text)
                        assert payload["ok"], payload
                        return payload
                    assert not text.startswith("ERROR:"), text
                    return text

                evidence["ping"] = await call("ping")
                assert "NOT_CONFIGURED" not in evidence["ping"]
                coding = await call("delegate_to_deepseek", {
                    "model": "flash", "task":
                    "Use Read to inspect input.txt. Use Edit to replace original-value "
                    "with verified-local-value, preserving the newline. Then use Bash "
                    "to run exactly: test \"$(cat input.txt)\" = verified-local-value "
                    "&& printf 'LOCAL_MCP_OK\\n' > verification.txt "
                    "&& cat verification.txt. After it succeeds, return LOCAL_MCP_OK."})
                assert target.read_text(encoding="utf-8") == "verified-local-value\n"
                verification = (workspace / "verification.txt").read_text(encoding="utf-8")
                assert verification == "LOCAL_MCP_OK\n", verification
                assert "LOCAL_MCP_OK" in coding
                coding_log = (private / "server.log").read_text(encoding="utf-8")
                observed = re.findall(r"tool_call: (\w+) arg_count=", coding_log)
                assert {"Read", "Edit", "Bash"}.issubset(observed), observed
                pending = (await call("get_deepseek_recovery", structured=True))["pending"]
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                assert pending and all(record["path"] == "input.txt" and
                                       record["sha256"] == digest and
                                       record["status"] == "committed" for record in pending), pending
                evidence.update(coding_tools=observed, coding_result=coding,
                                independently_verified_bash_marker=verification,
                                independently_verified_sha256=digest, recovery=pending)
                ack = await call("acknowledge_deepseek_mutations", {
                    "transaction_ids": [record["transaction_id"] for record in pending]}, structured=True)
                assert not ack["pending"]
                assert (await call("get_deepseek_recovery", structured=True))["count"] == 0
                readonly = await call("delegate_to_deepseek_readonly", {
                    "model": "flash", "task":
                    "Use Read to inspect input.txt and return its exact contents. Do not edit files."})
                assert "verified-local-value" in readonly
                readonly_log = (private / "server.log").read_text(encoding="utf-8")[len(coding_log):]
                readonly_tools = re.findall(r"tool_call: (\w+) arg_count=", readonly_log)
                assert "Read" in readonly_tools and set(readonly_tools).issubset(
                    {"Read", "Glob", "Grep"}), readonly_tools
                evidence.update(readonly_result=readonly, readonly_tools=readonly_tools)
                started = await call("start_deepseek_readonly", {"model": "pro", "task":
                    "Use Read to inspect input.txt. Return its exact contents and follow any "
                    "additional parent instructions received while working."}, structured=True)
                job_id = started["job_id"]
                marker = "HOST_STEER_" + secrets.token_hex(12)
                queued = await call("send_deepseek_message", {"job_id": job_id,
                    "message": f"Read input.txt, then include this exact parent message token "
                               f"in your final response: {marker}"}, structured=True)
                assert queued["message_queued"], queued
                deadline = time.monotonic() + 260
                while True:
                    result = await call("get_deepseek_result", {"job_id": job_id}, structured=True)
                    if result["ready"]:
                        break
                    assert time.monotonic() < deadline, "background delegation timed out"
                    await asyncio.sleep(5)
                assert result["status"] == "completed", result
                assert marker in result["result"]["final_message"], result
                assert "verified-local-value" in result["result"]["final_message"], result
                assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
                assert (await call("get_deepseek_recovery", structured=True))["count"] == 0
                evidence.update(background_result=result, steering_marker=marker,
                                steering_accepted=queued, status="passed")
        print(json.dumps(evidence, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    if not __debug__:
        raise RuntimeError("run without -O: this acceptance script requires assertions")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--model", default="subagent-validation")
    parser.add_argument("--max-output-tokens", type=int, default=1024)
    args = parser.parse_args()
    if not _is_loopback_endpoint(args.base_url):
        parser.error("this smoke test requires a loopback endpoint")
    asyncio.run(verify(args))


if __name__ == "__main__":
    main()
