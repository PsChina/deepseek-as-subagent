"""加载 ~/.deepseek-mcp/config.json + env 覆盖。

优先级：环境变量 > 配置文件 > 默认值（cwd）。
workspace 默认跟随 MCP server 进程的 cwd —— 即 Claude Code 启动时的目录，
和 Claude 主进程沙箱一致，不强制用户配置。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path.home() / ".deepseek-mcp" / "config.json"

DEFAULT_MODEL = "deepseek-v4-pro"  # 主力模型（推理强）；省钱场景改 deepseek-v4-flash
DEFAULT_MAX_TURNS = 50
DEFAULT_ALLOWED_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

logger = logging.getLogger(__name__)


@dataclass
class Config:
    api_key: str
    workspace: Path
    model: str = DEFAULT_MODEL
    max_turns: int = DEFAULT_MAX_TURNS
    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    base_url: str = "https://api.deepseek.com"

    @classmethod
    def load(cls) -> "Config":
        # DEEPSEEK_MODE=off → 让 server.py 自己判断是否暴露工具
        # 这里只负责加载真实配置
        data: dict = {}
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text())

        api_key = os.getenv("DEEPSEEK_API_KEY") or data.get("api_key", "")
        if not api_key or api_key == "PASTE_YOUR_DEEPSEEK_KEY_HERE":
            raise RuntimeError(
                f"DeepSeek API key not configured. "
                f"Set DEEPSEEK_API_KEY env var or edit {CONFIG_PATH}"
            )

        # workspace 解析：env > config > cwd
        # 配错了不 hard fail —— 警告 + fallback 到 cwd，确保 MCP 总能工作
        workspace_str = os.getenv("DEEPSEEK_WORKSPACE") or data.get("workspace", "")
        if workspace_str:
            workspace = Path(os.path.expanduser(workspace_str)).resolve()
            if not workspace.exists():
                logger.warning(
                    "Configured workspace does not exist: %s — falling back to cwd",
                    workspace,
                )
                workspace = Path.cwd()
        else:
            workspace = Path.cwd()  # 跟随 Claude Code 启动目录

        return cls(
            api_key=api_key,
            workspace=workspace,
            model=data.get("model", DEFAULT_MODEL),
            max_turns=int(data.get("max_turns", DEFAULT_MAX_TURNS)),
            allowed_tools=data.get("allowed_tools", list(DEFAULT_ALLOWED_TOOLS)),
            base_url=data.get("base_url", "https://api.deepseek.com"),
        )
