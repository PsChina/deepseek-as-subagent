"""Public DeepSeek model profiles exposed to MCP hosts."""
from __future__ import annotations

from typing import Literal

ModelChoice = Literal["flash", "pro"]


def resolve_model(choice: str, *, flash_model: str, pro_model: str) -> str:
    """Resolve a stable public profile to the user-configured provider model ID."""
    if choice == "flash":
        return flash_model
    if choice == "pro":
        return pro_model
    raise ValueError("model must be exactly 'flash' or 'pro'")
