"""Public DeepSeek model profiles exposed to MCP hosts."""
from __future__ import annotations

from typing import Literal

ModelChoice = Literal["flash", "pro"]

_MODEL_IDS = {
    "flash": "deepseek-v4-flash",
    "pro": "deepseek-v4-pro",
}


def resolve_model(choice: str) -> str:
    """Resolve the stable public profile to the provider model id."""
    try:
        return _MODEL_IDS[choice]
    except KeyError:
        raise ValueError("model must be exactly 'flash' or 'pro'") from None
