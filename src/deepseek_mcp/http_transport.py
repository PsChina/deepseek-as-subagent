"""Streamable-HTTP transport support for the deepseek-mcp server.

Precedence is CLI flag > environment variable > default:

  DEEPSEEK_MCP_TRANSPORT  "stdio" (default) or "streamable-http"
  DEEPSEEK_MCP_HOST       bind address for streamable-http (default 127.0.0.1)
  DEEPSEEK_MCP_PORT       TCP port (default 8765; MCP endpoint is /mcp)
  DEEPSEEK_MCP_STATELESS  "1" creates one transport per request
  DEEPSEEK_MCP_AUTH_TOKEN optional bearer token; when set, every HTTP
                          request must carry "Authorization: Bearer <token>"

The exposed tools read and mutate a real workspace, so binding a
non-loopback host requires an auth token and disables DNS-rebinding
header protection (the token is the authentication boundary then).
"""
from __future__ import annotations

import argparse
import hmac
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

TRANSPORTS = ("stdio", "streamable-http")
DEFAULT_TRANSPORT = "stdio"
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8765
AUTH_TOKEN_ENV = "DEEPSEEK_MCP_AUTH_TOKEN"
MIN_TOKEN_CHARS = 16
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ASGIReceiveEvent = Mapping[str, Any]
ASGISendEvent = Mapping[str, Any]
ASGIApp = Callable[
    [dict, Callable[[], Awaitable[ASGIReceiveEvent]], Callable[[ASGISendEvent], Awaitable[None]]],
    Awaitable[None],
]


@dataclass(frozen=True)
class TransportOptions:
    transport: str
    host: str
    port: int
    stateless: bool
    auth_token: str | None


def _env_text(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _validate_host(host: str) -> str:
    host = host.strip()
    if not host or host != host.strip() or any(
        char.isspace() or ord(char) < 32 or char in "@/" for char in host
    ):
        raise ValueError(
            "host must be a bare hostname or IP (no scheme, credentials, or path)"
        )
    return host


def _validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    return port


def _auth_token() -> str | None:
    value = os.getenv(AUTH_TOKEN_ENV)
    if value is None or value.strip() == "":
        return None
    value = value.strip()
    if len(value) < MIN_TOKEN_CHARS or any(
        char.isspace() or ord(char) < 32 for char in value
    ):
        raise ValueError(
            f"{AUTH_TOKEN_ENV} must have at least {MIN_TOKEN_CHARS} characters "
            "and no whitespace"
        )
    return value


def parse_server_args(argv: Sequence[str] | None = None) -> TransportOptions:
    """Resolve transport options; exits the process on invalid input."""
    parser = argparse.ArgumentParser(
        prog="deepseek-mcp",
        description=(
            "DeepSeek sub-agent MCP server (stdio by default; "
            "--transport streamable-http for HTTP access)"
        ),
    )
    parser.add_argument(
        "--transport", choices=TRANSPORTS,
        default=_env_text("DEEPSEEK_MCP_TRANSPORT", DEFAULT_TRANSPORT),
    )
    parser.add_argument("--host", default=_env_text("DEEPSEEK_MCP_HOST", DEFAULT_HTTP_HOST))
    parser.add_argument("--port", default=_env_text("DEEPSEEK_MCP_PORT", str(DEFAULT_HTTP_PORT)))
    parser.add_argument(
        "--stateless", action="store_true",
        default=_env_text("DEEPSEEK_MCP_STATELESS", "") == "1",
        help="create a fresh HTTP transport per request (no session handshake state)",
    )
    args = parser.parse_args(argv)
    if args.transport == DEFAULT_TRANSPORT:
        return TransportOptions(DEFAULT_TRANSPORT, DEFAULT_HTTP_HOST, DEFAULT_HTTP_PORT, False, None)
    try:
        host = _validate_host(str(args.host))
        port = _validate_port(int(str(args.port)))
        token = _auth_token()
    except ValueError as error:
        parser.error(str(error))
        raise
    if host not in LOOPBACK_HOSTS and token is None:
        parser.error(
            f"binding {host!r} requires {AUTH_TOKEN_ENV}; the exposed tools mutate "
            "a real workspace"
        )
    return TransportOptions(args.transport, host, port, bool(args.stateless), token)


class BearerAuthASGI:
    """Reject HTTP requests that lack the configured bearer token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        provided = [
            value for name, value in scope.get("headers", ())
            if name.lower() == b"authorization"
        ]
        if len(provided) == 1 and hmac.compare_digest(provided[0], self._expected):
            await self._app(scope, receive, send)
            return
        payload = b'{"error":"unauthorized"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer realm="deepseek-mcp"'),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": payload})


def _configure_fastmcp(server: Any, options: TransportOptions) -> None:
    from mcp.server.transport_security import TransportSecuritySettings

    server.settings.host = options.host
    server.settings.port = options.port
    server.settings.stateless_http = options.stateless
    if options.host not in LOOPBACK_HOSTS:
        server.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )


def run_streamable_http(server: Any, options: TransportOptions) -> None:
    """Serve the given FastMCP app over streamable HTTP until interrupted."""
    import uvicorn

    _configure_fastmcp(server, options)
    app = server.streamable_http_app()
    if options.auth_token is not None:
        app = BearerAuthASGI(app, options.auth_token)
    config = uvicorn.Config(
        app,
        host=options.host,
        port=options.port,
        log_level=str(server.settings.log_level).lower(),
    )
    uvicorn.Server(config).run()
