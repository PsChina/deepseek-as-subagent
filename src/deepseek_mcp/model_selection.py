"""Public DeepSeek model profiles exposed to MCP hosts."""
from __future__ import annotations

from typing import Literal

ModelChoice = Literal["flash", "pro"]
ReasoningEffort = Literal["none", "low", "high", "max"]


def resolve_model(choice: str, *, flash_model: str, pro_model: str) -> str:
    """Resolve a stable public profile to the user-configured provider model ID."""
    if choice == "flash":
        return flash_model
    if choice == "pro":
        return pro_model
    raise ValueError("model must be exactly 'flash' or 'pro'")


def resolve_reasoning_effort(
    choice: str, *, flash_effort: str, pro_effort: str
) -> str:
    """Resolve the reasoning effort configured for a public model profile."""
    if choice == "flash":
        return flash_effort
    if choice == "pro":
        return pro_effort
    raise ValueError("model must be exactly 'flash' or 'pro'")
