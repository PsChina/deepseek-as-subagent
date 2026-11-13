"""Deterministic verification of the host-side execution record."""
from __future__ import annotations

import re

from .policy_enforcement import path_is_allowed

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MUTATION_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _verify_mutation(item: object, allowed_tools: set[str], write_paths: tuple[str, ...], deny_paths: tuple[str, ...]) -> tuple[str, str, list[str]]:
    if not isinstance(item, dict):
        return "failed", "failed", ["malformed mutation record"]
    path, tool, status, digest = (
        item.get("path"), item.get("tool"), item.get("status"), item.get("sha256")
    )
    policy_status, integrity_status = "passed", "not_applicable"
    risks: list[str] = []
    if tool not in _MUTATION_TOOLS or tool not in allowed_tools:
        policy_status = "failed"
        risks.append("mutation record contains an operation outside the effective tool set")
    if not isinstance(path, str) or not path:
        policy_status = "not_verifiable" if policy_status == "passed" else policy_status
        integrity_status = "not_verifiable"
        risks.append("mutation path is missing")
    elif not path_is_allowed(path, write_paths, deny_paths):
        policy_status = "failed"
        risks.append(f"mutation path is outside the effective write scope: {path}")
    if status != "committed":
        integrity_status = "uncertain"
        risks.append(f"mutation outcome is uncertain: {path or 'unknown path'}")
    elif not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        integrity_status = "not_verifiable"
        risks.append(f"mutation digest is missing or invalid: {path or 'unknown path'}")
    else:
        integrity_status = "verified_at_commit"
    return policy_status, integrity_status, risks


def _verify_mutations(contract: dict, mutations: object, bash_executions: object) -> tuple[str, str, list[str]]:
    if not isinstance(mutations, list):
        return "not_verifiable", "not_verifiable", ["mutation record list is invalid"]
    write_paths = tuple(_string_items(contract.get("write_paths")))
    deny_paths = tuple(_string_items(contract.get("deny_paths")))
    allowed_tools = set(_string_items(contract.get("allowed_tools")))
    valid_bash_evidence = (
        not isinstance(bash_executions, bool)
        and isinstance(bash_executions, int)
        and bash_executions >= 0
    )
    if not valid_bash_evidence:
        policy_statuses = ["not_verifiable"]
        risks = ["Bash execution evidence is missing or invalid"]
    else:
        policy_statuses = ["failed" if bash_executions else "passed"]
        risks = []
    integrity_statuses: list[str] = []
    for item in mutations:
        policy, integrity, item_risks = _verify_mutation(item, allowed_tools, write_paths, deny_paths)
        policy_statuses.append(policy)
        integrity_statuses.append(integrity)
        risks.extend(item_risks)
    policy_status = next(status for status in ("failed", "not_verifiable", "passed") if status in policy_statuses)
    integrity_status = next((status for status in ("failed", "uncertain", "not_verifiable", "verified_at_commit") if status in integrity_statuses), "not_applicable")
    if valid_bash_evidence and bash_executions:
        risks.append("Bash was executed during a contract-bound delegation")
    return policy_status, integrity_status, risks


def verify_execution_record(contract: dict, mutations: object, bash_executions: object = 0) -> dict:
    """Verify path-policy compliance and mutation evidence; never infer test success."""
    policy_status, integrity_status, risks = _verify_mutations(contract, mutations, bash_executions)
    checks = [{"name": name, "status": "not_run", "evidence": None}
              for name in _string_items(contract.get("success_checks"))]
    semantic = [{"constraint": name, "status": "not_verified"}
                for name in _string_items(contract.get("must_not_change"))]
    outputs = [{"name": name, "status": "not_validated"}
               for name in _string_items(contract.get("expected_outputs"))]
    if checks:
        risks.append("declared success checks have not been executed")
    if semantic:
        risks.append("semantic constraints require independent review")
    if outputs:
        risks.append("expected outputs have not been independently validated")
    risks.append("functional correctness has not been independently verified")
    if policy_status == "failed":
        overall = "failed"
    elif policy_status != "passed" or integrity_status in {"failed", "uncertain", "not_verifiable"} or checks or semantic or outputs:
        overall = "partial"
    else:
        overall = "policy_verified"
    return {
        "policy_compliance": policy_status,
        "mutation_integrity": integrity_status,
        "success_checks": checks,
        "semantic_constraints": semantic,
        "expected_outputs": outputs,
        "overall_status": overall,
        "risks": risks,
        "functional_correctness": "not_independently_verified",
        "needs_review": True,
    }
