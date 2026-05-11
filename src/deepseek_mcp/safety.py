"""沙箱：路径限制 + 命令黑名单。

设计目标：DeepSeek 是"听话的助手但不一定可靠"——它可能误读路径、误跑命令。
不上 docker（启动慢、依赖重），用进程内的轻量检查防住 95% 的误操作。
"""
from __future__ import annotations

from pathlib import Path

# 危险命令字符串黑名单（出现在 Bash 工具的命令里即拒绝）
# 注意：这是粗粒度防御，重点防"误删整盘"和"逃逸沙箱"，不是 against 主动恶意攻击者。
DANGEROUS_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf $HOME",
    "rm -rf *",
    "sudo ",
    "dd if=",
    ":(){:|:&};:",  # fork bomb
    "mkfs.",
    "> /dev/sd",
    "chmod -R 777 /",
    "curl ",  # 默认禁止外发流量；用户要打开需要在 config.json 显式开
    "wget ",
    "nc ",
    "ncat ",
    "ssh ",
    "scp ",
    "rsync ",
    "git push",  # 防止 DeepSeek 不小心推代码
    "npm publish",
    "pip install",  # 防止装恶意包
]


class SandboxViolation(Exception):
    """工具调用违反沙箱规则。返回给 DeepSeek 让它知道为什么失败。"""


def resolve_safe_path(rel_or_abs: str, workspace: Path) -> Path:
    """把 DeepSeek 传来的路径解析到绝对路径，并校验在 workspace 内。

    返回值：解析后的绝对路径。
    抛出：SandboxViolation 如果路径逃出 workspace。
    """
    p = Path(rel_or_abs).expanduser()
    if not p.is_absolute():
        p = workspace / p
    abs_path = p.resolve()

    # 用 commonpath 检测，比 startswith 更安全（避免 /workspace-evil 这种前缀绕过）
    try:
        rel = abs_path.relative_to(workspace.resolve())
        _ = rel  # 通过检查
    except ValueError as e:
        raise SandboxViolation(
            f"Path {abs_path} is outside workspace {workspace}. "
            f"Tools can only access files within the configured workspace."
        ) from e

    return abs_path


def check_command(command: str) -> None:
    """检查 Bash 命令是否在黑名单里。抛 SandboxViolation 即拒绝。"""
    lower = command.lower()
    for pattern in DANGEROUS_PATTERNS:
        if pattern.lower() in lower:
            raise SandboxViolation(
                f"Command blocked by sandbox: contains dangerous pattern '{pattern}'. "
                f"If you need this capability, ask the user to enable it explicitly."
            )
