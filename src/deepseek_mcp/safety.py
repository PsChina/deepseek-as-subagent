"""Workspace path checks and command-policy defence in depth.

The command policy is intentionally not treated as a security boundary. Bash is
disabled by default and, when explicitly enabled, runs only through the
container boundary in :mod:`deepseek_mcp.container_sandbox`.
"""
from __future__ import annotations

import shlex
from pathlib import Path

_VCS_DIRECTORY_NAMES = frozenset({".git", ".hg", ".svn"})
_AGENT_CONTROL_DIRECTORY_NAMES = frozenset(
    {".agents", ".claude", ".codex", ".deepseek-mcp"}
)
_AGENT_CONTROL_FILE_NAMES = frozenset(
    {".mcp.json", "agents.md", "claude.md", "codex.md"}
)
_SENSITIVE_HOME_PATHS = (
    (".aws",), (".azure",), (".gnupg",), (".kube",), (".ssh",),
    (".config", "gcloud"), ("Library", "Keychains"),
)


def is_vcs_control_name(name: str) -> bool:
    """Return whether one path component names VCS control state."""
    return name.casefold() in _VCS_DIRECTORY_NAMES


def is_agent_control_name(name: str) -> bool:
    folded = name.casefold()
    return folded in _AGENT_CONTROL_DIRECTORY_NAMES or folded in _AGENT_CONTROL_FILE_NAMES


def is_protected_host_path(path: Path) -> bool:
    """Keep model file tools away from host agent/config and VCS control state."""
    try:
        candidate = path.resolve()
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return True
    for parts in _SENSITIVE_HOME_PATHS:
        protected = home.joinpath(*parts)
        if candidate == protected or protected in candidate.parents:
            return True
    if any(
        is_vcs_control_name(part) or part.casefold() in _AGENT_CONTROL_DIRECTORY_NAMES
        for part in candidate.parts
    ):
        return True
    return candidate.name.casefold() in _AGENT_CONTROL_FILE_NAMES


def is_unsafe_workspace_root(path: Path) -> bool:
    """Reject broad home/ancestor roots and known credential/control roots."""
    try:
        candidate = path.resolve(strict=True)
        home = Path.home().resolve(strict=True)
    except (OSError, RuntimeError):
        return True
    return candidate == home or candidate in home.parents or is_protected_host_path(candidate)

# 危险命令检测的两种粒度：
#   1) DANGEROUS_TOKENS：第一个 token（程序名）整体匹配，难以用 \ 编码绕过
#   2) DANGEROUS_PHRASES：完整短语子串匹配（rm -rf / 这种"不可能合法"的组合）
DANGEROUS_TOKENS = {
    "sudo",
    "su",
    "nc", "ncat", "netcat",
    "ssh", "scp", "sftp", "rsync",
    "curl", "wget",
    "telnet",
    "socat",
}

# 配套程序名集合（出现在 token 流任意位置即拒绝）
DANGEROUS_ANYWHERE_TOKENS = {
    "sudo", "su",
}

# 完整短语匹配（保留旧风格，专门抓"形态独特"的危险组合）
DANGEROUS_PHRASES = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf $HOME",
    "rm -rf *",
    "rm -rf .",
    ":(){:|:&};:",  # fork bomb
    "mkfs.",
    "> /dev/sd",
    "chmod -R 777 /",
    "dd if=/dev/zero",
    "dd if=/dev/random",
    "/dev/tcp/",
    "/dev/udp/",
]

# Python / shell / 解释器 -c 后内联代码：很容易藏恶意命令，统一拒绝
DANGEROUS_INLINE_INTERPRETERS = {
    ("python", "-c"), ("python3", "-c"),
    ("perl", "-e"), ("ruby", "-e"),
    ("node", "-e"), ("php", "-r"),
    ("awk", "-e"),
    ("sh", "-c"), ("bash", "-c"), ("zsh", "-c"), ("ksh", "-c"), ("dash", "-c"),
}

# 包管理"装东西"动作：易被滥用装恶意包
PACKAGE_INSTALL_PREFIXES = [
    ("pip", "install"), ("pip3", "install"),
    ("pipx", "install"),
    ("npm", "install"), ("npm", "i"),
    ("yarn", "add"),
    ("pnpm", "install"), ("pnpm", "add"),
    ("uv", "pip"),
    ("uv", "add"),
    ("gem", "install"),
    ("cargo", "install"),
    ("brew", "install"),
    ("apt", "install"), ("apt-get", "install"),
    ("dnf", "install"), ("yum", "install"),
]

# 发布 / 推送动作：写到外部世界的"出口"
PUBLISH_PREFIXES = [
    ("git", "push"),
    ("npm", "publish"),
    ("twine", "upload"),
    ("cargo", "publish"),
    ("gh", "release"),
]


class SandboxViolation(Exception):
    """工具调用违反沙箱规则。返回给 DeepSeek 让它知道为什么失败。"""


def resolve_safe_path(rel_or_abs: str, workspace: Path) -> Path:
    """把 DeepSeek 传来的路径解析到绝对路径，并校验在 workspace 内。

    返回值：解析后的绝对路径。
    抛出：SandboxViolation 如果路径逃出 workspace。
    """
    if not isinstance(rel_or_abs, str):
        raise SandboxViolation("path must be a string")
    if not rel_or_abs:
        raise SandboxViolation("empty path is not allowed")
    if "\x00" in rel_or_abs:
        raise SandboxViolation("null byte in path is not allowed")

    try:
        p = Path(rel_or_abs).expanduser()
        if not p.is_absolute():
            p = workspace / p
        abs_path = p.resolve()
        ws_resolved = workspace.resolve()
    except (OSError, RuntimeError, ValueError):
        raise SandboxViolation("path cannot be safely resolved") from None

    try:
        abs_path.relative_to(ws_resolved)
    except ValueError as e:
        raise SandboxViolation(
            f"Path {abs_path} is outside workspace {ws_resolved}. "
            f"Tools can only access files within the configured workspace."
        ) from e

    if is_protected_host_path(abs_path):
        raise SandboxViolation("path targets protected host or VCS control state")

    return abs_path


def _tokenize(command: str) -> list[str]:
    """安全分词。命令引号不闭合时 shlex 会抛错；回退到 split。"""
    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return command.split()


def _check_phrases(command: str) -> None:
    lower = command.lower()
    for phrase in DANGEROUS_PHRASES:
        if phrase.lower() in lower:
            raise SandboxViolation(
                f"Command blocked by sandbox: contains dangerous phrase '{phrase}'."
            )


def _check_clause(tokens: list[str]) -> None:
    first = _strip_cmd_prefix(tokens[0])
    for token in tokens:
        if _strip_cmd_prefix(token) in DANGEROUS_ANYWHERE_TOKENS:
            raise SandboxViolation(
                f"Command blocked by sandbox: '{token}' not allowed."
            )
    if first in DANGEROUS_TOKENS:
        raise SandboxViolation(
            f"Command blocked by sandbox: program '{first}' not allowed "
            f"(network / privilege escalation tools are disabled)."
        )
    if len(tokens) < 2:
        return
    signature = (first, tokens[1])
    if signature in DANGEROUS_INLINE_INTERPRETERS:
        raise SandboxViolation(
            f"Command blocked by sandbox: inline code via '{first} {tokens[1]}' "
            f"is not allowed (write a file then run it instead)."
        )
    if signature in PACKAGE_INSTALL_PREFIXES:
        raise SandboxViolation(
            f"Command blocked by sandbox: package install '{first} {tokens[1]}' is not allowed."
        )
    if signature in PUBLISH_PREFIXES:
        raise SandboxViolation(
            f"Command blocked by sandbox: publish action '{first} {tokens[1]}' is not allowed."
        )


def check_command(command: str) -> None:
    """Reject known-dangerous commands before the container boundary."""
    if not command or not command.strip():
        raise SandboxViolation("empty command")
    _check_phrases(command)
    for clause in _split_clauses(command):
        tokens = _tokenize(clause)
        if tokens:
            _check_clause(tokens)


def _split_clauses(command: str) -> list[str]:
    """按 ; && || | 切分子句（粗粒度，不考虑引号内的分隔符 — 用足够好就行）。"""
    out: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(command)
    in_single = False
    in_double = False
    while i < n:
        c = command[i]
        # 简单引号跟踪，避免 ';' 在引号里被当分隔符
        if c == "'" and not in_double:
            in_single = not in_single
            buf.append(c)
            i += 1
            continue
        if c == '"' and not in_single:
            in_double = not in_double
            buf.append(c)
            i += 1
            continue
        if not in_single and not in_double:
            two = command[i:i+2]
            if c in ";|&" or two in ("&&", "||"):
                if buf:
                    out.append("".join(buf).strip())
                    buf = []
                i += 2 if two in ("&&", "||") else 1
                continue
        buf.append(c)
        i += 1
    if buf:
        out.append("".join(buf).strip())
    return [c for c in out if c]


def _strip_cmd_prefix(tok: str) -> str:
    """剥掉 'command'/'\\'/'/path/to/' 等程序名前缀，归一化判断。"""
    # 'command curl' / '\curl' / '/usr/bin/curl' → 'curl'
    if tok.startswith("\\"):
        tok = tok[1:]
    if "/" in tok:
        tok = tok.rsplit("/", 1)[-1]
    return tok.lower()
