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


def _verify_mutation_policy(
    path: object, tool: object, allowed_tools: set[str],
    read_paths: tuple[str, ...], write_paths: tuple[str, ...],
    deny_paths: tuple[str, ...],
) -> tuple[str, list[str]]:
    if not isinstance(tool, str) or tool not in _MUTATION_TOOLS or tool not in allowed_tools:
        return "failed", ["mutation record contains an operation outside the effective tool set"]
    if not isinstance(path, str) or not path:
        return "not_verifiable", ["mutation path is missing"]
    if not path_is_allowed(path, write_paths, deny_paths):
        return "failed", [f"mutation path is outside the effective write scope: {path}"]
    if tool in {"Edit", "NotebookEdit"} and not path_is_allowed(path, read_paths, deny_paths):
        return "failed", [f"edit path is outside the effective read scope: {path}"]
    return "passed", []


def _verify_mutation_integrity(
    path: object, status: object, digest: object,
) -> tuple[str, list[str]]:
    label = path if isinstance(path, str) and path else "unknown path"
    if status != "committed":
        return "uncertain", [f"mutation outcome is uncertain: {label}"]
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        return "not_verifiable", [f"mutation digest is missing or invalid: {label}"]
    return "verified_at_commit", []


def _verify_mutation(
    item: object, allowed_tools: set[str], read_paths: tuple[str, ...],
    write_paths: tuple[str, ...], deny_paths: tuple[str, ...],
) -> tuple[str, str, list[str]]:
    if not isinstance(item, dict):
        return "failed", "failed", ["malformed mutation record"]
    path, tool, status, digest = (
        item.get("path"), item.get("tool"), item.get("status"), item.get("sha256")
    )
    policy_status, policy_risks = _verify_mutation_policy(
        path, tool, allowed_tools, read_paths, write_paths, deny_paths
    )
    integrity_status, integrity_risks = _verify_mutation_integrity(
        path, status, digest
    )
    return policy_status, integrity_status, policy_risks + integrity_risks


def _verify_mutations(contract: dict, mutations: object, bash_executions: object) -> tuple[str, str, list[str]]:
    if not isinstance(mutations, list):
        return "not_verifiable", "not_verifiable", ["mutation record list is invalid"]
    read_paths = tuple(_string_items(contract.get("read_paths")))
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
        policy, integrity, item_risks = _verify_mutation(item, allowed_tools, read_paths, write_paths, deny_paths)
        policy_statuses.append(policy)
        integrity_statuses.append(integrity)
        risks.extend(item_risks)
    policy_status = next(status for status in ("failed", "not_verifiable", "passed") if status in policy_statuses)
    integrity_status = next((status for status in ("failed", "uncertain", "not_verifiable", "verified_at_commit") if status in integrity_statuses), "not_applicable")
    if valid_bash_evidence and bash_executions:
        risks.append("Bash was executed during a contract-bound delegation")
    return policy_status, integrity_status, risks




def _last_mutation_digests(mutations: list) -> dict[str, object] | None:
    latest: dict[str, object] = {}
    for record in mutations:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            return None
        latest[record["path"]] = record.get("sha256")
    return latest


def _observation_state(item: dict, expected: object) -> str:
    if item.get("expected_sha256") != expected:
        return "failed"
    status, observed = item.get("status"), item.get("observed_sha256")
    if status == "not_verifiable" and observed is None:
        return "not_verifiable"
    if not isinstance(observed, str) or _SHA256.fullmatch(observed) is None:
        return "invalid"
    if status == "matched" and observed == expected:
        return "matched"
    if status == "changed" and observed != expected:
        return "changed"
    return "invalid"


def _reconcile_readback(
    latest: dict[str, object], evidence: list,
) -> tuple[dict[str, str], str | None]:
    observed: dict[str, str] = {}
    for item in evidence:
        path = item.get("path") if isinstance(item, dict) else None
        if not isinstance(path, str) or path not in latest or path in observed:
            return observed, "unexpected or duplicate workspace evidence path"
        state = _observation_state(item, latest[path])
        if state == "failed":
            return observed, "workspace readback digest differs from mutation record"
        if state == "invalid":
            return observed, "workspace readback observation is invalid"
        observed[path] = state
    return observed, None


def _verify_workspace_evidence(
    mutations: object, evidence: object,
) -> tuple[str, list[str]]:
    """Cross-check final file readback without claiming cryptographic attestation."""
    if not isinstance(mutations, list) or not isinstance(evidence, list):
        return "not_verifiable", ["workspace readback evidence is unavailable"]
    latest = _last_mutation_digests(mutations)
    if latest is None:
        return "not_verifiable", ["workspace mutation paths are incomplete"]
    if not latest:
        return ("not_applicable", []) if not evidence else (
            "not_verifiable", ["unexpected workspace readback evidence"]
        )
    observations, error = _reconcile_readback(latest, evidence)
    if error:
        return ("failed" if "differs" in error else "not_verifiable"), [error]
    if len(observations) != len(latest):
        return "not_verifiable", ["workspace readback is incomplete"]
    if "changed" in observations.values():
        return "changed", ["workspace file differs from last committed digest"]
    if "not_verifiable" in observations.values():
        return "not_verifiable", ["one or more workspace files could not be checked"]
    return "matched", []


def _resource_limit_status(observed: object, maximum: object, label: str) -> tuple[str, str | None]:
    valid_maximum = not isinstance(maximum, bool) and isinstance(maximum, int) and maximum >= 1
    valid_observed = not isinstance(observed, bool) and isinstance(observed, int) and observed >= 0
    if not valid_maximum or not valid_observed:
        return "not_verifiable", f"{label} evidence is missing or invalid"
    if observed > maximum:
        return "failed", f"{label} exceeded the task contract limit"
    return "passed", None


def _verify_resource_limits(
    contract: dict, tool_calls: object, mutation_bytes_reserved: object
) -> tuple[str, list[str]]:
    tool_status, tool_risk = _resource_limit_status(
        tool_calls, contract.get("max_tool_calls"), "tool call count"
    )
    byte_status, byte_risk = _resource_limit_status(
        mutation_bytes_reserved, contract.get("max_mutation_bytes"),
        "reserved mutation bytes",
    )
    statuses = (tool_status, byte_status)
    overall = next(
        status for status in ("failed", "not_verifiable", "passed")
        if status in statuses
    )
    return overall, [risk for risk in (tool_risk, byte_risk) if risk is not None]


def verify_execution_record(contract: dict, mutations: object, bash_executions: object = 0, tool_calls: object = None, mutation_bytes_reserved: object = None, workspace_evidence: object = None) -> dict:
    """Verify path-policy compliance and mutation evidence; never infer test success."""
    policy_status, integrity_status, risks = _verify_mutations(contract, mutations, bash_executions)
    resource_status, resource_risks = _verify_resource_limits(
        contract, tool_calls, mutation_bytes_reserved
    )
    risks.extend(resource_risks)
    workspace_status, workspace_risks = _verify_workspace_evidence(mutations, workspace_evidence)
    risks.extend(workspace_risks)
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
    if policy_status == "failed" or resource_status == "failed" or workspace_status == "failed":
        overall = "failed"
    elif policy_status != "passed" or resource_status != "passed" or workspace_status in {"changed", "not_verifiable"} or integrity_status in {"failed", "uncertain", "not_verifiable"} or checks or semantic or outputs:
        overall = "partial"
    else:
        overall = "policy_verified"
    return {
        "policy_compliance": policy_status,
        "mutation_integrity": integrity_status,
        "workspace_readback": workspace_status,
        "resource_compliance": resource_status,
        "success_checks": checks,
        "semantic_constraints": semantic,
        "expected_outputs": outputs,
        "overall_status": overall,
        "risks": risks,
        "functional_correctness": "not_independently_verified",
        "needs_review": True,
    }
