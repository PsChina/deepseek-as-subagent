"""Versioned structured handoff for contract-bound delegations."""
from __future__ import annotations

from .verification import verify_execution_record


def _changed_files(mutations: object) -> list[dict]:
    changed_files = []
    if isinstance(mutations, list):
        for item in mutations:
            if not isinstance(item, dict):
                continue
            changed_files.append({
                "transaction_id": item.get("transaction_id"),
                "path": item.get("path"),
                "operation": item.get("tool"),
                "status": item.get("status"),
                "sha256": item.get("sha256"),
                "warning": item.get("warning"),
            })
    return changed_files


def _contract_summary(contract: dict) -> dict:
    return {
        "version": 1,
        "intent": contract.get("intent"),
        "sha256": contract.get("contract_hash"),
        "allowed_tools": contract.get("allowed_tools", []),
        "read_paths": contract.get("read_paths", []),
        "write_paths": contract.get("write_paths", []),
        "deny_paths": contract.get("deny_paths", []),
        "limits": {
            "max_tool_calls": contract.get("max_tool_calls"),
            "max_mutation_bytes": contract.get("max_mutation_bytes"),
        },
    }


def _bash_summary(result: dict) -> dict:
    bash = result.get("bash", {})
    if not isinstance(bash, dict):
        bash = {}
    return {
        "calls": bash.get("calls", 0),
        "executed_calls": bash.get("executed_calls"),
        "failure_count": bash.get("failure_count", 0),
        "last_status": bash.get("last_status"),
        "failures": bash.get("failures", []),
    }


def build_handoff(result: object) -> dict | None:
    if not isinstance(result, dict) or not isinstance(result.get("task_contract"), dict):
        return None
    contract = result["task_contract"]
    mutations = result.get("mutations", [])
    bash = _bash_summary(result)
    verification = verify_execution_record(
        contract, mutations, bash["executed_calls"],
        result.get("tool_calls"), result.get("mutation_bytes_reserved"),
        result.get("workspace_evidence"),
    )
    return {
        "schema_version": 1,
        "execution_status": "completed",
        "verification_status": verification["overall_status"],
        "contract": _contract_summary(contract),
        "changed_files": _changed_files(mutations),
        "workspace_evidence": result.get("workspace_evidence"),
        "commands_run": [],
        "bash_summary": bash,
        "verification": verification,
        "metrics": {
            "turns_used": result.get("turns_used"),
            "tool_calls": result.get("tool_calls"),
            "mutation_bytes_reserved": result.get("mutation_bytes_reserved"),
            "tokens": result.get("tokens"),
            "duration_seconds": result.get("duration_seconds"),
        },
        "needs_review": verification["needs_review"],
    }
