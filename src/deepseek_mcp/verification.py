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


def _verify_mutations(contract: dict, mutations: object) -> tuple[str, str, list[str]]:
    if not isinstance(mutations, list):
        return "not_verifiable", "not_verifiable", ["mutation record list is invalid"]
    write_paths = tuple(_string_items(contract.get("write_paths")))
    deny_paths = tuple(_string_items(contract.get("deny_paths")))
    policy_status, integrity_status = "passed", "not_applicable"
    risks: list[str] = []
    for item in mutations:
        if not isinstance(item, dict):
            policy_status = integrity_status = "failed"
            risks.append("malformed mutation record")
            continue
        path, tool, status, digest = (
            item.get("path"), item.get("tool"), item.get("status"), item.get("sha256")
        )
        if tool not in _MUTATION_TOOLS:
            policy_status = "failed"
            risks.append("mutation record contains an unknown operation")
        if not isinstance(path, str) or not path:
            policy_status, integrity_status = "not_verifiable", "not_verifiable"
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
        elif integrity_status == "not_applicable":
            integrity_status = "verified_at_commit"
    return policy_status, integrity_status, risks


def verify_execution_record(contract: dict, mutations: object) -> dict:
    """Verify path-policy compliance and mutation evidence; never infer test success."""
    policy_status, integrity_status, risks = _verify_mutations(contract, mutations)
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
    if policy_status == "failed":
        overall = "failed"
    elif policy_status != "passed" or integrity_status in {"uncertain", "not_verifiable"} or checks or semantic or outputs:
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
        "needs_review": overall != "policy_verified",
    }
