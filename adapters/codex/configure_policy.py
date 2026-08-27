"""Ownership and policy edits for a managed Codex MCP registration."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping, MutableMapping
from pathlib import Path

import tomlkit

SERVER_NAME = "deepseek"
MANAGED_MARKER = "managed-by: deepseek-as-subagent"
LEGACY_EXPOSED_TOOLS = [
    "ping",
    "delegate_to_deepseek",
    "start_deepseek",
    "get_deepseek_status",
    "send_deepseek_message",
    "cancel_deepseek",
    "get_deepseek_result",
]
EXPOSED_TOOLS = [
    *LEGACY_EXPOSED_TOOLS,
    "get_deepseek_recovery",
    "acknowledge_deepseek_mutations",
]
EXECUTION_TOOLS = frozenset({"delegate_to_deepseek", "start_deepseek"})
RECOVERY_TOOLS = ("get_deepseek_recovery", "acknowledge_deepseek_mutations")
FORWARDED_ENV_VARS = [
    "DEEPSEEK_API_KEY", "DEEPSEEK_WORKSPACE", "DEEPSEEK_MODE",
    "DOCKER_HOST", "CONTAINER_HOST", "HTTP_PROXY", "HTTPS_PROXY",
    "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy",
    "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
]
GENERATION_NAME = re.compile(r"generation\.[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
SAFE_SERVER_KEYS = {
    "command", "enabled", "enabled_tools", "disabled_tools",
    "startup_timeout_sec", "tool_timeout_sec", "default_tools_approval_mode",
    "env_vars", "tools",
}
DEFAULT_TOOL_TIMEOUT_SECONDS = 18_060
LEGACY_TOOL_TIMEOUT_SECONDS = 18_000


class ConfigTransactionError(RuntimeError):
    """Base error for a rejected or failed config transaction."""


class OwnershipError(ConfigTransactionError):
    """The existing same-name MCP entry is not recognizably ours."""


def _mapping(value: object, label: str) -> MutableMapping[str, object]:
    if not isinstance(value, MutableMapping):
        raise ConfigTransactionError(f"{label} must be a TOML table")
    return value


def _servers_table(document) -> MutableMapping[str, object]:
    servers = document.get("mcp_servers")
    if servers is None:
        servers = tomlkit.table()
        document.add("mcp_servers", servers)
    return _mapping(servers, "mcp_servers")


def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_managed_generation_command(command: str | Path, root: Path) -> bool:
    raw = os.fspath(command)
    if not os.path.isabs(raw) or any(part in {".", ".."} for part in Path(raw).parts):
        return False
    candidate = Path(_normalized_path(command))
    managed_root = Path(_normalized_path(root))
    try:
        relative = candidate.relative_to(managed_root)
    except ValueError:
        return False
    if len(relative.parts) != 3:
        return False
    generation, directory, executable = relative.parts
    if GENERATION_NAME.fullmatch(generation) is None:
        return False
    suffix = (directory.lower(), executable.lower())
    return suffix in {
        ("bin", "deepseek-mcp"),
        ("bin", "deepseek-mcp.exe"),
        ("scripts", "deepseek-mcp"),
        ("scripts", "deepseek-mcp.exe"),
    }


def _managed_root_from_target(target: Path) -> Path:
    root = Path(_normalized_path(target)).parent.parent.parent
    if not _is_managed_generation_command(target, root):
        raise ConfigTransactionError(
            "installer command is not a direct managed-generation entrypoint"
        )
    return root


def _is_owned_entry(
    server: MutableMapping[str, object], target: Path | None = None
) -> bool:
    trivia = getattr(server, "trivia", None)
    if MANAGED_MARKER in str(getattr(trivia, "comment", "")):
        return True
    if "url" in server:
        return False
    command = server.get("command")
    if not isinstance(command, str):
        return False
    root = (
        _managed_root_from_target(target)
        if target is not None
        else Path.home() / ".deepseek-mcp" / "codex-venvs"
    )
    return _is_managed_generation_command(command, root)


def _validate_optional_string_list(
    server: MutableMapping[str, object], key: str,
) -> None:
    value = server.get(key)
    if value is not None and (
        not isinstance(value, list)
        or not all(isinstance(item, str) for item in value)
    ):
        raise ConfigTransactionError(
            f"mcp_servers.deepseek.{key} must be a string list"
        )


def _validate_owned_launch_policy(server: MutableMapping[str, object]) -> None:
    unsafe_keys = sorted(set(server) - SAFE_SERVER_KEYS)
    if unsafe_keys:
        raise OwnershipError(
            "managed mcp_servers.deepseek contains launch fields that cannot be "
            f"preserved safely ({', '.join(unsafe_keys)}); rerun with --force-replace"
        )
    existing = server.get("env_vars")
    if existing is not None and (
        not isinstance(existing, list)
        or not all(isinstance(item, str) for item in existing)
    ):
        raise ConfigTransactionError("mcp_servers.deepseek.env_vars must be a string list")
    unexpected = sorted(set(existing or []) - set(FORWARDED_ENV_VARS))
    if unexpected:
        raise OwnershipError(
            "managed mcp_servers.deepseek forwards unapproved environment variables "
            f"({', '.join(unexpected)}); rerun with --force-replace"
        )
    _validate_optional_string_list(server, "enabled_tools")
    _validate_optional_string_list(server, "disabled_tools")


def _mark_owned(server: MutableMapping[str, object]) -> None:
    trivia = getattr(server, "trivia", None)
    existing = str(getattr(trivia, "comment", ""))
    if MANAGED_MARKER in existing:
        return
    add_comment = getattr(server, "comment", None)
    if not callable(add_comment):
        raise ConfigTransactionError(
            "mcp_servers.deepseek must be a regular TOML table to add ownership metadata"
        )
    existing = existing.removeprefix("#").strip()
    add_comment(f"{existing}; {MANAGED_MARKER}" if existing else MANAGED_MARKER)


def _tool_policy(server: MutableMapping[str, object]) -> None:
    tools = server.get("tools")
    if tools is None:
        tools = tomlkit.table()
        server["tools"] = tools
    table = _mapping(tools, "mcp_servers.deepseek.tools")
    for name in (
        "get_deepseek_result",
        "get_deepseek_recovery",
        "acknowledge_deepseek_mutations",
    ):
        policy = table.get(name)
        if policy is None:
            policy = tomlkit.table()
            table[name] = policy
        _mapping(policy, f"mcp_servers.deepseek.tools.{name}").setdefault(
            "approval_mode", "approve"
        )


def _forwarded_environment(server: MutableMapping[str, object]) -> None:
    server["env_vars"] = FORWARDED_ENV_VARS.copy()


def _validate_recovery_visibility(server: MutableMapping[str, object]) -> None:
    enabled = set(server.get("enabled_tools", []))
    disabled = set(server.get("disabled_tools", []))
    active_execution = (enabled - disabled).intersection(EXECUTION_TOOLS)
    if active_execution and disabled.intersection(RECOVERY_TOOLS):
        raise ConfigTransactionError(
            "enabled DeepSeek execution cannot disable transaction recovery tools"
        )


def configure_install(document, command: Path, force_replace: bool = False) -> list[str]:
    _managed_root_from_target(command)
    servers = _servers_table(document)
    server = servers.get(SERVER_NAME)
    warnings: list[str] = []
    if force_replace:
        server = tomlkit.table()
        servers[SERVER_NAME] = server
    elif server is None:
        server = tomlkit.table()
        servers[SERVER_NAME] = server
    else:
        server = _mapping(server, "mcp_servers.deepseek")
        if not _is_owned_entry(server, command):
            raise OwnershipError(
                "mcp_servers.deepseek is not recognizably managed by "
                "deepseek-as-subagent; refusing to replace it without --force-replace"
            )
        _validate_owned_launch_policy(server)
    _apply_defaults(server, command)
    _validate_recovery_visibility(server)
    approval = server.get("default_tools_approval_mode")
    if approval not in {"writes", "prompt"}:
        warnings.append(
            f"existing default_tools_approval_mode={approval!r} was preserved; "
            "writes is the recommended production policy"
        )
    timeout = server.get("tool_timeout_sec")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout < DEFAULT_TOOL_TIMEOUT_SECONDS
    ):
        raise ConfigTransactionError(
            "mcp_servers.deepseek.tool_timeout_sec must be at least 18060 "
            "(5-hour run plus cleanup grace)"
        )
    return warnings


def _apply_defaults(server: MutableMapping[str, object], command: Path) -> None:
    _mark_owned(server)
    server["command"] = str(command)
    server["enabled"] = True
    enabled_tools = server.get("enabled_tools")
    if enabled_tools is None:
        server["enabled_tools"] = EXPOSED_TOOLS.copy()
    elif EXECUTION_TOOLS.intersection(enabled_tools):
        for name in RECOVERY_TOOLS:
            if name not in enabled_tools:
                enabled_tools.append(name)
    server.setdefault("startup_timeout_sec", 20)
    if server.get("tool_timeout_sec") == LEGACY_TOOL_TIMEOUT_SECONDS:
        server["tool_timeout_sec"] = DEFAULT_TOOL_TIMEOUT_SECONDS
    server.setdefault("tool_timeout_sec", DEFAULT_TOOL_TIMEOUT_SECONDS)
    server.setdefault("default_tools_approval_mode", "writes")
    _forwarded_environment(server)
    _tool_policy(server)


def _registration_transport(payload: object) -> Mapping[str, object]:
    if not isinstance(payload, Mapping) or payload.get("name") != SERVER_NAME:
        raise ConfigTransactionError("Codex returned the wrong MCP registration")
    if payload.get("enabled") is not True:
        raise ConfigTransactionError("Codex reports the DeepSeek MCP registration disabled")
    transport = payload.get("transport")
    if not isinstance(transport, Mapping) or transport.get("type") != "stdio":
        raise ConfigTransactionError("Codex did not register a stdio MCP transport")
    return transport


def _validate_registration_command(
    transport: Mapping[str, object], expected_command: Path, venv_root: Path
) -> None:
    command = transport.get("command")
    if not isinstance(command, str) or _normalized_path(command) != _normalized_path(
        expected_command
    ):
        raise ConfigTransactionError("Codex registered an unexpected MCP command")
    if not _is_managed_generation_command(command, venv_root):
        raise ConfigTransactionError("Codex command is outside the managed generation root")


def _validate_registration_environment(transport: Mapping[str, object]) -> None:
    if transport.get("args") not in (None, []):
        raise ConfigTransactionError("Codex registration retained unexpected arguments")
    if transport.get("cwd") is not None:
        raise ConfigTransactionError("Codex registration retained an unexpected cwd")
    if transport.get("env") not in (None, {}):
        raise ConfigTransactionError("Codex registration retained inline environment values")
    env_vars = transport.get("env_vars", [])
    if not isinstance(env_vars, list) or not all(isinstance(item, str) for item in env_vars):
        raise ConfigTransactionError("Codex returned invalid forwarded environment metadata")
    if len(env_vars) != len(FORWARDED_ENV_VARS) or set(env_vars) != set(
        FORWARDED_ENV_VARS
    ):
        raise ConfigTransactionError("Codex registration forwards unexpected environment variables")


def validate_registration_payload(
    payload: object, expected_command: Path, venv_root: Path
) -> None:
    transport = _registration_transport(payload)
    _validate_registration_command(transport, expected_command, venv_root)
    _validate_registration_environment(transport)


def validate_registration_absent(payload: object) -> None:
    if not isinstance(payload, list):
        raise ConfigTransactionError("Codex returned an invalid MCP registration list")
    for entry in payload:
        if not isinstance(entry, Mapping):
            raise ConfigTransactionError("Codex returned an invalid MCP registration entry")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigTransactionError("Codex returned an invalid MCP registration name")
        if name == SERVER_NAME:
            raise ConfigTransactionError("Codex still reports a DeepSeek MCP registration")


def configure_uninstall(document, force: bool = False) -> None:
    servers = document.get("mcp_servers")
    if servers is None:
        return
    table = _mapping(servers, "mcp_servers")
    server = table.get(SERVER_NAME)
    if server is None:
        return
    server = _mapping(server, "mcp_servers.deepseek")
    if not _is_owned_entry(server) and not force:
        raise OwnershipError(
            "mcp_servers.deepseek is not recognizably managed by "
            "deepseek-as-subagent; refusing to remove it without --force"
        )
    del table[SERVER_NAME]
