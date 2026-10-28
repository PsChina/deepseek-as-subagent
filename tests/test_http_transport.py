"""Streamable-HTTP transport tests: argument policy, auth middleware, e2e."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

HAVE_PACKAGE = importlib.util.find_spec("deepseek_mcp") is not None
if HAVE_PACKAGE:
    from deepseek_mcp.http_transport import (
        BearerAuthASGI,
        TransportOptions,
        parse_server_args,
    )

TEST_TOKEN = "a-very-long-test-bearer-token-0123456789"
# Host proxy settings must not intercept loopback test traffic on either the
# test-process httpx client or the spawned server.
_BLANKED_PROXY_ENV = {
    "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
    "http_proxy": "", "https_proxy": "", "all_proxy": "",
    "NO_PROXY": "*", "no_proxy": "*",
}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _isolated_home_environment(home: str) -> dict[str, str]:
    environment = os.environ.copy()
    isolated_home = os.path.abspath(home)
    home_drive, home_path = os.path.splitdrive(isolated_home)
    environment.update(
        {
            "HOME": isolated_home,
            "USERPROFILE": isolated_home,
            "HOMEDRIVE": home_drive,
            "HOMEPATH": home_path or os.sep,
            "DEEPSEEK_MODE": "off",
            **_BLANKED_PROXY_ENV,
        }
    )
    environment.pop("DEEPSEEK_MCP_AUTH_TOKEN", None)
    environment.pop("DEEPSEEK_MCP_TRANSPORT", None)
    environment.pop("DEEPSEEK_MCP_HOST", None)
    environment.pop("DEEPSEEK_MCP_PORT", None)
    environment.pop("DEEPSEEK_MCP_STATELESS", None)
    return environment


@unittest.skipUnless(HAVE_PACKAGE, "deepseek_mcp is not importable")
class TransportArgumentTests(unittest.TestCase):
    def test_default_is_stdio(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            options = parse_server_args([])
        self.assertEqual(options.transport, "stdio")
        self.assertEqual(options.host, "127.0.0.1")
        self.assertEqual(options.port, 8765)
        self.assertIsNone(options.auth_token)

    def test_environment_and_flags_configure_http(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_MCP_TRANSPORT": "streamable-http",
                "DEEPSEEK_MCP_PORT": "9911",
                "DEEPSEEK_MCP_AUTH_TOKEN": TEST_TOKEN,
            },
        ):
            options = parse_server_args(["--host", "localhost", "--stateless"])
        self.assertEqual(options.transport, "streamable-http")
        self.assertEqual(options.host, "localhost")
        self.assertEqual(options.port, 9911)
        self.assertTrue(options.stateless)
        self.assertEqual(options.auth_token, TEST_TOKEN)

    def test_nonloopback_bind_requires_token(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            with self.assertRaises(SystemExit):
                parse_server_args(
                    ["--transport", "streamable-http", "--host", "0.0.0.0"]
                )
        with patch.dict(
            os.environ, {"DEEPSEEK_MCP_AUTH_TOKEN": TEST_TOKEN}
        ):
            options = parse_server_args(
                ["--transport", "streamable-http", "--host", "0.0.0.0"]
            )
        self.assertEqual(options.host, "0.0.0.0")

    def test_invalid_port_or_short_token_rejected(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEEPSEEK_MCP_AUTH_TOKEN", None)
            with self.assertRaises(SystemExit):
                parse_server_args(
                    ["--transport", "streamable-http", "--port", "99999"]
                )
        with patch.dict(os.environ, {"DEEPSEEK_MCP_AUTH_TOKEN": "short"}):
            with self.assertRaises(SystemExit):
                parse_server_args(["--transport", "streamable-http"])

    def test_host_with_scheme_rejected(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_MCP_AUTH_TOKEN": TEST_TOKEN}):
            with self.assertRaises(SystemExit):
                parse_server_args(
                    ["--transport", "streamable-http", "--host", "http://x/"]
                )


@unittest.skipUnless(HAVE_PACKAGE, "deepseek_mcp is not importable")
class BearerAuthMiddlewareTests(unittest.TestCase):
    @staticmethod
    async def _passthrough(scope: dict, receive, send) -> None:
        scope["reached"] = True

    @staticmethod
    def _capture_send() -> tuple[list, object]:
        events: list = []

        async def send(event: dict) -> None:
            events.append(event)

        return events, send

    def test_lifespan_passes_through(self) -> None:
        middleware = BearerAuthASGI(self._passthrough, TEST_TOKEN)
        scope = {"type": "lifespan"}

        async def receive():
            return {"type": "lifespan.startup"}

        asyncio.run(middleware(scope, receive, lambda event: asyncio.sleep(0)))
        self.assertTrue(scope.get("reached"))

    def test_missing_and_wrong_headers_are_rejected(self) -> None:
        middleware = BearerAuthASGI(self._passthrough, TEST_TOKEN)
        for scope in (
            {"type": "http", "headers": []},
            {"type": "http", "headers": [(b"authorization", b"Bearer nope")]},
            {
                "type": "http",
                "headers": [
                    (b"authorization", f"Bearer {TEST_TOKEN}".encode()),
                    (b"authorization", f"Bearer {TEST_TOKEN}".encode()),
                ],
            },
        ):
            events, send = self._capture_send()

            async def receive():
                return {"type": "http.request"}

            asyncio.run(middleware(scope, receive, send))
            self.assertEqual(events[0]["status"], 401)
            self.assertNotIn("reached", scope)

    def test_correct_header_reaches_application(self) -> None:
        middleware = BearerAuthASGI(self._passthrough, TEST_TOKEN)
        scope = {
            "type": "http",
            "headers": [(b"Authorization", f"Bearer {TEST_TOKEN}".encode())],
        }

        async def receive():
            return {"type": "http.request"}

        asyncio.run(middleware(scope, receive, lambda event: asyncio.sleep(0)))
        self.assertTrue(scope.get("reached"))


class _HttpTestServer:
    def __init__(self, home: str, extra_env: dict[str, str] | None = None):
        self.port = _free_port()
        environment = _isolated_home_environment(home)
        if extra_env:
            environment.update(extra_env)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "deepseek_mcp.server",
                "--transport",
                "streamable-http",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._wait_until_listening()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def _wait_until_listening(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self._fail_with_stderr("server exited early")
            try:
                with socket.create_connection(("127.0.0.1", self.port), 1):
                    return
            except OSError:
                time.sleep(0.1)
        self._fail_with_stderr("server never accepted connections")

    def _fail_with_stderr(self, reason: str) -> None:
        stderr = b""
        if self.process.stderr is not None:
            stderr = self.process.stderr.read()
        raise AssertionError(
            f"{reason}: returncode={self.process.returncode} stderr={stderr.decode('utf-8', 'replace')}"
        )

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=15)


@unittest.skipUnless(HAVE_PACKAGE, "deepseek_mcp is not importable")
class HttpRoundTripTests(unittest.TestCase):
    @staticmethod
    async def _ping(url: str, headers: dict[str, str] | None) -> str:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(url, headers=headers) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                listed = await session.list_tools()
                if "delegate_to_deepseek" not in {tool.name for tool in listed.tools}:
                    raise AssertionError("delegate_to_deepseek missing over HTTP")
                result = await session.call_tool("ping", {})
                return "\n".join(
                    block.text for block in result.content if hasattr(block, "text")
                )

    def test_ping_round_trip_without_auth(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict(
            os.environ, _BLANKED_PROXY_ENV
        ):
            server = _HttpTestServer(home)
            try:
                text = asyncio.run(
                    asyncio.wait_for(self._ping(server.url, None), timeout=30)
                )
            finally:
                server.stop()
        self.assertIn("pong from deepseek-mcp", text)

    def test_bearer_token_required_when_configured(self) -> None:
        import httpx

        with tempfile.TemporaryDirectory() as home, patch.dict(
            os.environ, _BLANKED_PROXY_ENV
        ):
            server = _HttpTestServer(
                home, {"DEEPSEEK_MCP_AUTH_TOKEN": TEST_TOKEN}
            )
            try:
                response = httpx.post(
                    server.url,
                    headers={
                        "content-type": "application/json",
                        "accept": "application/json, text/event-stream",
                    },
                    content=b"{}",
                    timeout=10,
                )
                self.assertEqual(response.status_code, 401)
                self.assertIn("Bearer", response.headers.get("www-authenticate", ""))
                wrong = httpx.post(
                    server.url,
                    headers={
                        "content-type": "application/json",
                        "accept": "application/json, text/event-stream",
                        "authorization": "Bearer wrong-token-should-not-pass-x",
                    },
                    content=b"{}",
                    timeout=10,
                )
                self.assertEqual(wrong.status_code, 401)
                with_token = httpx.post(
                    server.url,
                    headers={
                        "content-type": "application/json",
                        "accept": "application/json, text/event-stream",
                        "authorization": f"Bearer {TEST_TOKEN}",
                    },
                    content=b"{}",
                    timeout=10,
                )
                self.assertNotEqual(with_token.status_code, 401)
                text = asyncio.run(
                    asyncio.wait_for(
                        self._ping(
                            server.url,
                            {"Authorization": f"Bearer {TEST_TOKEN}"},
                        ),
                        timeout=30,
                    )
                )
            finally:
                server.stop()
        self.assertIn("pong from deepseek-mcp", text)

    def test_stateless_mode_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict(
            os.environ, _BLANKED_PROXY_ENV
        ):
            server = _HttpTestServer(home, {"DEEPSEEK_MCP_STATELESS": "1"})
            # The test helper always passes explicit CLI args; env stateless
            # still applies because argparse reads it as the flag default.
            try:
                text = asyncio.run(
                    asyncio.wait_for(self._ping(server.url, None), timeout=30)
                )
            finally:
                server.stop()
        self.assertIn("pong from deepseek-mcp", text)


if __name__ == "__main__":
    unittest.main()
