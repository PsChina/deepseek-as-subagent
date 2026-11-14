"""Independent, read-only artifact verification by the trusted host.

This does not run arbitrary commands and does not certify functional correctness.
"""
from __future__ import annotations

from .file_io import read_workspace_text
from .policy_enforcement import bind_file_policy, path_is_allowed
from .safety import SandboxViolation
from .verification_plan import ArtifactExpectation, VerificationPlan
from .workspace_guard import bind_workspace_identity


def _verify_one(config, expected: ArtifactExpectation) -> dict:
    policy = config.effective_policy
    scopes = policy.write_paths if expected.role == "output" else policy.read_paths
    outcome = {"path": expected.path, "role": expected.role,
               "expected_sha256": expected.sha256, "status": "not_verifiable"}
    if not path_is_allowed(expected.path, scopes, policy.deny_paths):
        outcome["status"] = "denied"
        return outcome
    operation = "Write" if expected.role == "output" else "Read"
    try:
        with bind_workspace_identity(config.expected_workspace_identity):
            with bind_file_policy(policy, operation):
                _content, identity = read_workspace_text(
                    config.workspace, expected.path, require_canonical=True
                )
        actual = identity.digest.hex() if identity.digest is not None else None
    except (OSError, RuntimeError, ValueError, SandboxViolation):
        return outcome
    if actual is None:
        return outcome
    outcome["observed_sha256"] = actual
    outcome["status"] = "matched" if actual == expected.sha256 else "changed"
    return outcome


def verify_artifacts(config, plan: VerificationPlan | dict) -> dict:
    """Validate exact outputs and untouched baselines without executing code.

    Only a trusted caller should supply plan; model-authored JSON or success
    text is not a source of independent acceptance evidence.
    """
    if isinstance(plan, dict):
        plan = VerificationPlan.from_dict(plan)
    if not isinstance(plan, VerificationPlan):
        raise ValueError("verification plan is invalid")
    policy = getattr(config, "effective_policy", None)
    if policy is None or plan.contract_sha256 != policy.contract_hash:
        return {"status": "invalid_plan", "artifacts": [], "success_checks": [],
                "functional_correctness": "not_independently_verified"}
    if any(name not in policy.success_checks for name in plan.success_checks):
        return {"status": "invalid_plan", "artifacts": [], "success_checks": [],
                "functional_correctness": "not_independently_verified"}
    artifacts = [_verify_one(config, item) for item in plan.artifacts]
    checks = [{"name": name, "status": "not_run"} for name in plan.success_checks]
    states = {item["status"] for item in artifacts}
    if states.intersection({"denied", "changed"}):
        status = "failed"
    elif "not_verifiable" in states or checks:
        status = "incomplete"
    else:
        status = "artifact_verified"
    return {"status": status, "artifacts": artifacts, "success_checks": checks,
            "functional_correctness": "not_independently_verified"}
