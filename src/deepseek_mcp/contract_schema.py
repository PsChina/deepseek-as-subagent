"""Strict MCP input models for the public Task Contract schema."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .resource_budget import MAX_MUTATION_BYTES_PER_RUN, MAX_TOOL_CALLS_PER_RUN
from .task_contract import (
    DEFAULT_DENY_PATHS,
    MAX_LIST_ITEMS,
    MAX_PATH_PATTERN_CHARS,
    MAX_TEXT_CHARS,
)

PathPattern = Annotated[str, Field(min_length=1, max_length=MAX_PATH_PATTERN_CHARS)]
ContractText = Annotated[str, Field(min_length=1, max_length=MAX_TEXT_CHARS)]
ContractTool = Literal["Read", "Write", "Edit", "Glob", "Grep", "NotebookEdit"]


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ContractPermissionsInput(_StrictInput):
    tools: list[ContractTool] | None = Field(
        default=None, max_length=6,
        description="Optional tool subset. Bash is intentionally unavailable in contract-bound runs.",
    )
    read_paths: list[PathPattern] | None = Field(
        default=None, max_length=MAX_LIST_ITEMS,
        description="Workspace-relative paths the agent may read.",
    )
    write_paths: list[PathPattern] | None = Field(
        default=None, max_length=MAX_LIST_ITEMS,
        description="Workspace-relative paths the agent may create or modify.",
    )
    deny_paths: list[PathPattern] | None = Field(
        default=None, max_length=MAX_LIST_ITEMS - len(DEFAULT_DENY_PATHS),
        description="Additional deny patterns. Built-in credential denies cannot be removed.",
    )


class ContractConstraintsInput(_StrictInput):
    must_not_change: list[ContractText] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    success_checks: list[ContractText] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    expected_outputs: list[ContractText] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)


class ContractLimitsInput(_StrictInput):
    max_tool_calls: int | None = Field(default=None, ge=1, le=MAX_TOOL_CALLS_PER_RUN)
    max_mutation_bytes: int | None = Field(
        default=None, ge=1, le=MAX_MUTATION_BYTES_PER_RUN
    )


class TaskContractInput(_StrictInput):
    version: Literal[1] = Field(default=1, description="Task Contract schema version.")
    intent: Literal["implement", "readonly_scan", "audit", "review"] = Field(
        default="implement", description="The intended class of work."
    )
    permissions: ContractPermissionsInput = Field(default_factory=ContractPermissionsInput)
    constraints: ContractConstraintsInput = Field(default_factory=ContractConstraintsInput)
    limits: ContractLimitsInput = Field(default_factory=ContractLimitsInput)


def contract_payload(contract: TaskContractInput) -> dict:
    """Convert validated MCP input to the strict runtime parser's plain mapping."""
    return contract.model_dump(exclude_unset=True, exclude_none=True)
