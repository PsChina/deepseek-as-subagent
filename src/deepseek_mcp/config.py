"""Load ``~/.deepseek-mcp/config.json`` with safe production defaults."""
from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .child_runtime import ChildRuntimeError, runtime_is_within_workspace
from . import windows_file_io
from .safety import is_unsafe_workspace_root
from .workspace_guard import configure_workspace_identity
CONFIG_PATH = Path.home() / ".deepseek-mcp" / "config.json"
MAX_CONFIG_BYTES = 1024 * 1024
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_MAX_TURNS = 50
MAX_TURNS = 100
DEFAULT_MAX_RUN_SECONDS = 5 * 60 * 60
HARD_MAX_RUN_SECONDS = 48 * 60 * 60
DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Glob",
    "Grep",
    "NotebookEdit",
]
KNOWN_TOOLS = frozenset(
    {"Read", "Write", "Edit", "Bash", "Glob", "Grep", "NotebookEdit"}
)
MUTATION_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
CONFIG_KEYS = frozenset(
    {
        "api_key",
        "workspace",
        "model",
        "max_turns",
        "max_run_seconds",
        "allowed_tools",
        "base_url",
    }
)
logger = logging.getLogger(__name__)
def _validate_private_directory(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RuntimeError(f"Cannot inspect config directory {path}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise RuntimeError(f"Config directory must be a real directory: {path}")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError(
            f"Config directory must be owned by the current user with mode 0700: {path}"
        )

def _validate_private_file(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"Config must be a regular file: {CONFIG_PATH}")
    if os.name != "posix":
        return
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError(
            f"Config must be owned by the current user and private; "
            f"run: chmod 700 {CONFIG_PATH.parent} && chmod 600 {CONFIG_PATH}"
        )

def _read_config_text() -> str:
    if os.name == "nt":
        try:
            payload, _info = windows_file_io.read_regular(
                CONFIG_PATH, max_bytes=MAX_CONFIG_BYTES
            )
            return payload.decode("utf-8")
        except FileNotFoundError:
            raise
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"Config must be UTF-8: {CONFIG_PATH}") from exc
        except (OSError, windows_file_io.WindowsPathError) as exc:
            raise RuntimeError(f"Cannot safely read {CONFIG_PATH}: {exc}") from exc
    _validate_private_directory(CONFIG_PATH.parent)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(CONFIG_PATH, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RuntimeError(f"Cannot safely open {CONFIG_PATH}: {exc}") from exc
    try:
        _validate_private_file(descriptor)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(MAX_CONFIG_BYTES + 1)
        if len(payload) > MAX_CONFIG_BYTES:
            raise RuntimeError(f"Config is too large: {CONFIG_PATH}")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"Config must be UTF-8: {CONFIG_PATH}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class _DuplicateConfigKey(ValueError):
    pass


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for name, value in pairs:
        if name in result:
            raise _DuplicateConfigKey
        result[name] = value
    return result


def _load_data() -> dict:
    try:
        text = _read_config_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text, object_pairs_hook=_strict_json_object)
    except _DuplicateConfigKey:
        raise RuntimeError(f"Duplicate key in {CONFIG_PATH}") from None
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON in {CONFIG_PATH} "
            f"(line {exc.lineno}, col {exc.colno}): {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Top-level of {CONFIG_PATH} must be a JSON object")
    if not set(data).issubset(CONFIG_KEYS):
        raise RuntimeError(f"Unsupported key in {CONFIG_PATH}")
    return data


def _validate_credential_storage(data: dict) -> None:
    configured = data.get("api_key", "")
    if (
        os.name == "nt"
        and isinstance(configured, str)
        and configured.strip()
        and configured.strip() != "PASTE_YOUR_DEEPSEEK_KEY_HERE"
    ):
        raise RuntimeError(
            "Windows config files cannot store API keys safely; remove api_key "
            "from config.json and set DEEPSEEK_API_KEY in the process environment"
        )


def _load_api_key(data: dict) -> str:
    _validate_credential_storage(data)
    configured = data.get("api_key", "")
    value = os.getenv("DEEPSEEK_API_KEY") or configured
    if not isinstance(value, str):
        raise RuntimeError("DeepSeek API key must be a string")
    credential = value.strip()
    if not credential or credential == "PASTE_YOUR_DEEPSEEK_KEY_HERE":
        raise RuntimeError(
            f"DeepSeek API key not configured. Set DEEPSEEK_API_KEY "
            f"or edit {CONFIG_PATH}"
        )
    if not credential.startswith("sk-"):
        logger.warning("DeepSeek API key does not start with 'sk-'; verify the key")
    return credential

def _workspace_setting(data: dict) -> tuple[object | None, bool]:
    environment = os.getenv("DEEPSEEK_WORKSPACE")
    if environment is not None:
        return environment, True
    if "workspace" in data:
        return data["workspace"], True
    return None, False


def _load_workspace(data: dict) -> Path:
    configured, explicit = _workspace_setting(data)
    if explicit and (not isinstance(configured, str) or not configured.strip()):
        raise RuntimeError("workspace must be a non-empty path string")
    if explicit:
        assert isinstance(configured, str)
        try:
            candidate = Path(os.path.expanduser(configured)).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(f"Configured workspace is invalid: {exc}") from exc
    else:
        candidate = Path.cwd().resolve()
    if not candidate.exists():
        source = "Configured workspace" if configured else "Current workspace"
        raise RuntimeError(f"{source} does not exist: {candidate}")
    if not candidate.is_dir():
        raise RuntimeError(f"Workspace is not a directory: {candidate}")
    if is_unsafe_workspace_root(candidate):
        raise RuntimeError(
            "Workspace must be a project directory, not a home, credential, "
            "agent-control, or broad ancestor directory"
        )
    return candidate


def _validate_max_turns(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("max_turns must be an integer")
    if not 1 <= value <= MAX_TURNS:
        raise RuntimeError(f"max_turns must be between 1 and {MAX_TURNS}")
    return value


def _load_max_turns(data: dict) -> int:
    return _validate_max_turns(data.get("max_turns", DEFAULT_MAX_TURNS))


def _validate_max_run_seconds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("max_run_seconds must be an integer")
    if not 1 <= value <= HARD_MAX_RUN_SECONDS:
        raise RuntimeError(
            f"max_run_seconds must be between 1 and {HARD_MAX_RUN_SECONDS}"
        )
    return value


def _load_allowed_tools(data: dict) -> list[str]:
    tools = data.get("allowed_tools", list(DEFAULT_ALLOWED_TOOLS))
    if not isinstance(tools, list):
        raise RuntimeError("allowed_tools must be a list of tool names")
    if not all(isinstance(tool, str) for tool in tools):
        raise RuntimeError("allowed_tools must be a list of tool names")
    unknown = sorted(set(tools) - KNOWN_TOOLS)
    if unknown:
        raise RuntimeError(f"Unknown allowed_tools: {', '.join(unknown)}")
    if len(tools) != len(set(tools)):
        raise RuntimeError("allowed_tools must not contain duplicates")
    return list(tools)


def _validate_model(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("model must be a non-empty string")
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise RuntimeError("model must not contain surrounding or control whitespace")
    return value


def _parse_base_url(value: object):
    if not isinstance(value, str) or not value:
        raise RuntimeError("base_url must be a non-empty string")
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise RuntimeError("base_url must not contain whitespace or control characters")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise RuntimeError(f"base_url is invalid: {exc}") from exc
    return value, parsed, hostname


def _validate_url_authority(parsed, hostname: str | None) -> None:
    if not hostname:
        raise RuntimeError("base_url must contain a host and no user credentials")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("base_url must contain a host and no user credentials")
    if parsed.query or parsed.fragment:
        raise RuntimeError("base_url must not contain a query or fragment")


def _validate_base_url(value: object) -> str:
    value, parsed, hostname = _parse_base_url(value)
    _validate_url_authority(parsed, hostname)
    assert hostname is not None
    if parsed.scheme == "https":
        return value
    loopback_hosts = {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and hostname.lower() in loopback_hosts:
        return value
    raise RuntimeError("base_url must use HTTPS; HTTP is allowed only for loopback")


@dataclass(repr=False)
class Config:
    api_key: str
    workspace: Path
    model: str = DEFAULT_MODEL
    max_turns: int = DEFAULT_MAX_TURNS
    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    base_url: str = "https://api.deepseek.com"
    max_run_seconds: int = DEFAULT_MAX_RUN_SECONDS
    delegation_capability: str = field(default="coding", repr=False)
    expected_workspace_identity: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if is_unsafe_workspace_root(self.workspace):
            raise RuntimeError("workspace is a protected or overly broad host path")
        self.expected_workspace_identity = configure_workspace_identity(
            self.workspace, self.expected_workspace_identity
        )
        self.model = _validate_model(self.model)
        self.base_url = _validate_base_url(self.base_url)
        self.max_turns = _validate_max_turns(self.max_turns)
        self.max_run_seconds = _validate_max_run_seconds(self.max_run_seconds)
        if self.delegation_capability not in {"coding", "readonly"}:
            raise RuntimeError("invalid delegation capability")
        self._validate_mutation_runtime()

    def _validate_mutation_runtime(self) -> None:
        if not MUTATION_TOOLS.intersection(self.allowed_tools):
            return
        try:
            unsafe = runtime_is_within_workspace(self.workspace)
        except (ChildRuntimeError, OSError, RuntimeError):
            unsafe = True
        if unsafe:
            raise RuntimeError(
                "Mutation tools require a non-editable deepseek-mcp installation "
                "outside the delegated workspace; reinstall with the supported installer"
            )

    @classmethod
    def _from_data(cls, data: dict, credential: str) -> "Config":
        return cls(
            credential,
            workspace=_load_workspace(data),
            model=data.get("model", DEFAULT_MODEL),
            max_turns=_load_max_turns(data),
            max_run_seconds=_validate_max_run_seconds(
                data.get("max_run_seconds", DEFAULT_MAX_RUN_SECONDS)
            ),
            allowed_tools=_load_allowed_tools(data),
            base_url=data.get("base_url", "https://api.deepseek.com"),
        )

    @classmethod
    def validate_runtime_settings(cls) -> None:
        """Validate non-secret settings without requiring a configured API key."""
        data = _load_data()
        _validate_credential_storage(data)
        cls._from_data(data, "")

    @classmethod
    def load(cls) -> "Config":
        data = _load_data()
        return cls._from_data(data, _load_api_key(data))
