#!/usr/bin/env python3
"""Exercise the installed server through real MCP stdio protocol calls."""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "ping",
    "delegate_to_deepseek",
    "start_deepseek",
    "get_deepseek_status",
    "send_deepseek_message",
    "cancel_deepseek",
    "get_deepseek_result",
    "get_deepseek_recovery",
    "acknowledge_deepseek_mutations",
}


def _check_legacy_schemas(tools: dict) -> None:
    ping = tools["ping"].inputSchema
    delegate = tools["delegate_to_deepseek"].inputSchema
    if ping.get("type") != "object" or ping.get("properties") != {}:
        raise RuntimeError("ping input schema is not backward compatible")
    if ping.get("required") not in (None, []):
        raise RuntimeError("ping unexpectedly requires arguments")
    properties = delegate.get("properties", {})
    compatible = (
        delegate.get("type") == "object"
        and delegate.get("required") == ["task"]
        and properties.get("task", {}).get("type") == "string"
        and properties.get("context", {}).get("type") == "string"
        and properties.get("context", {}).get("default") == ""
        and "additionalProperties" not in delegate
    )
    if not compatible:
        raise RuntimeError("delegate_to_deepseek schema is not backward compatible")


async def smoke(command: Path) -> None:
    parameters = StdioServerParameters(command=str(command))
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            if not initialized.serverInfo.name:
                raise RuntimeError("MCP initialize returned no server name")
            instructions = initialized.instructions or ""
            required = (
                "delegate_to_deepseek",
                "get_deepseek_recovery",
                "acknowledge_deepseek_mutations",
                "verify delegated output",
            )
            if any(fragment not in instructions for fragment in required):
                raise RuntimeError("MCP initialize returned incomplete host instructions")

            listed = await session.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            names = set(tools)
            if names != EXPECTED_TOOLS:
                raise RuntimeError(
                    f"unexpected MCP tools: missing={EXPECTED_TOOLS - names}, "
                    f"extra={names - EXPECTED_TOOLS}"
                )
            if any(tool.annotations is None for tool in listed.tools):
                raise RuntimeError("one or more MCP tools are missing annotations")
            _check_legacy_schemas(tools)

            result = await session.call_tool(
                "ping", {}, read_timeout_seconds=timedelta(seconds=10)
            )
            text = "\n".join(
                block.text for block in result.content if hasattr(block, "text")
            )
            if result.isError or "pong from deepseek-mcp" not in text:
                raise RuntimeError(f"ping failed: {text}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", type=Path)
    args = parser.parse_args()
    if not args.command.is_file():
        parser.error(f"MCP command does not exist: {args.command}")
    asyncio.run(asyncio.wait_for(smoke(args.command), timeout=20))
    print("       MCP initialize/list_tools/ping OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
