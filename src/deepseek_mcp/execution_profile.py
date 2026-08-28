"""Freeze the capability selected by a delegation API before it starts."""
from __future__ import annotations

from dataclasses import dataclass, replace

from .config import DEFAULT_ALLOWED_TOOLS, Config


@dataclass(frozen=True)
class ExecutionProfile:
    """Immutable capabilities selected by the MCP endpoint, not the model."""

    capability: str
    allowed_tools: tuple[str, ...]

    def bind(self, config: Config) -> Config:
        return replace(
            config,
            allowed_tools=list(self.allowed_tools),
            delegation_capability=self.capability,
        )


CODING_PROFILE = ExecutionProfile("coding", tuple(DEFAULT_ALLOWED_TOOLS))
READONLY_PROFILE = ExecutionProfile("readonly", ("Read", "Glob", "Grep"))


def configure_delegation(config: Config, profile: ExecutionProfile) -> Config:
    """Bind the API-selected immutable capability without runtime probes."""
    return profile.bind(config)
