#!/usr/bin/env python3
"""Exercise the installed server through real MCP protocol calls.

Either spawn a stdio server (positional command) or connect to a running
streamable-HTTP endpoint (--url).  With --wait, HTTP connection failures are
retried until the deadline so installers can smoke-test a freshly started
daemon without sleeping blindly.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import timedelta
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

EXPECTED_TOOLS = {
    "ping",
    "delegate_to_deepseek",
    "delegate_to_deepseek_readonly",
    "start_deepseek",
    "start_deepseek_readonly",
    "get_deepseek_status",
    "send_deepseek_message",
    "cancel_deepseek",
    "get_deepseek_result",
    "get_deepseek_recovery",
    "acknowledge_deepseek_mutations",
}


def _task_context_schema(schema: dict) -> bool:
    properties = schema.get("properties", {})
    return (
        schema.get("type") == "object"
        and schema.get("required") == ["task"]
        and properties.get("task", {}).get("type") == "string"
        and properties.get("context", {}).get("type") == "string"
        and properties.get("context", {}).get("default") == ""
        and "additionalProperties" not in schema
    )


def _check_legacy_schemas(tools: dict) -> None:
    ping = tools["ping"].inputSchema
    if ping.get("type") != "object" or ping.get("properties") != {}:
        raise RuntimeError("ping input schema is not backward compatible")
    if ping.get("required") not in (None, []):
        raise RuntimeError("ping unexpectedly requires arguments")
    if not _task_context_schema(tools["delegate_to_deepseek"].inputSchema):
        raise RuntimeError("delegate_to_deepseek schema is not backward compatible")
    readonly = tools["delegate_to_deepseek_readonly"].inputSchema
    if not _task_context_schema(readonly):
        raise RuntimeError("readonly delegation schema is not compatible")


async def _verify_session(session: ClientSession) -> None:
    initialized = await session.initialize()
    if not initialized.serverInfo.name:
        raise RuntimeError("MCP initialize returned no server name")
    instructions = initialized.instructions or ""
    required = (
        "delegate_to_deepseek",
        "delegate_to_deepseek_readonly",
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


async def smoke(command: Path) -> None:
    parameters = StdioServerParameters(command=str(command))
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await _verify_session(session)


async def _wait_until_listening(url: str, deadline: float) -> None:
    """A transport-level HTTP reply (any status) proves the daemon is up."""
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await client.post(
                    url,
                    headers={
                        "content-type": "application/json",
                        "accept": "application/json, text/event-stream",
                    },
                    content=b"{}",
                )
                return
            except httpx.HTTPError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.2)


async def smoke_url(url: str, headers: dict[str, str], wait: float) -> None:
    await _wait_until_listening(url, time.monotonic() + wait)
    async with streamablehttp_client(url, headers=headers or None) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await _verify_session(session)


def _parse_headers(pairs: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator or not name.strip():
            raise SystemExit(f"--header expects NAME=value, got: {pair!r}")
        headers[name.strip()] = value
    return headers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", type=Path)
    parser.add_argument("--url", help="streamable-HTTP MCP endpoint to connect to")
    parser.add_argument(
        "--header", action="append", default=[],
        help="NAME=value request header for --url (repeatable, e.g. Authorization)",
    )
    parser.add_argument(
        "--wait", type=float, default=15.0,
        help="seconds to retry connecting to --url before failing (default 15)",
    )
    args = parser.parse_args()
    if (args.command is None) == (args.url is None):
        parser.error("provide exactly one stdio command or --url")
    if args.command is not None:
        if not args.command.is_file():
            parser.error(f"MCP command does not exist: {args.command}")
        asyncio.run(asyncio.wait_for(smoke(args.command), timeout=20))
    else:
        timeout = args.wait + 30
        asyncio.run(
            asyncio.wait_for(
                smoke_url(args.url, _parse_headers(args.header), args.wait),
                timeout=timeout,
            )
        )
    print("       MCP initialize/list_tools/ping OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
