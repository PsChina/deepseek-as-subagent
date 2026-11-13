"""MCP entrypoints for contract-bound delegations; legacy schemas stay stable."""
from __future__ import annotations

from .contract_schema import TaskContractInput, contract_payload
from .model_selection import ModelChoice


def register_contract_tools(mcp, delegate, start, coding_profile, readonly_profile, coding_annotations, readonly_annotations) -> None:
    @mcp.tool(annotations=coding_annotations)
    async def delegate_to_deepseek_with_contract(
        task: str, contract: TaskContractInput, context: str = "", model: ModelChoice = "flash"
    ) -> str:
        """Run a coding delegation under an explicit Task Contract."""
        return await delegate(task, context, coding_profile, model, contract_payload(contract))

    @mcp.tool(annotations=readonly_annotations)
    async def delegate_to_deepseek_readonly_with_contract(
        task: str, contract: TaskContractInput, context: str = "", model: ModelChoice = "flash"
    ) -> str:
        """Run a read-only delegation under an explicit Task Contract."""
        return await delegate(task, context, readonly_profile, model, contract_payload(contract))

    @mcp.tool(annotations=coding_annotations)
    def start_deepseek_with_contract(
        task: str, contract: TaskContractInput, context: str = "", model: ModelChoice = "flash"
    ) -> str:
        """Start a background coding job under an explicit Task Contract."""
        return start(task, context, coding_profile, model, contract_payload(contract))

    @mcp.tool(annotations=readonly_annotations)
    def start_deepseek_readonly_with_contract(
        task: str, contract: TaskContractInput, context: str = "", model: ModelChoice = "flash"
    ) -> str:
        """Start a background read-only job under an explicit Task Contract."""
        return start(task, context, readonly_profile, model, contract_payload(contract))
