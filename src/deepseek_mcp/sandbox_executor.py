"""Run trusted host acceptance tests against a networkless Docker candidate."""
from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path

from .sandbox_plan import (
    SandboxSource,
    SandboxTestPlan,
    compute_test_suite_digest,
    read_hidden_suite,
    stage_source,
    suite_digest,
)
from .sandbox_worker import (
    CandidateSession,
    SandboxCandidateError,
    SandboxExecutionError,
    SandboxOutputLimitError,
    SandboxTimeoutError,
    SandboxUnavailableError,
    docker_binary as _docker_binary,
)

__all__ = [
    "SandboxSource", "SandboxTestPlan", "SandboxUnavailableError",
    "compute_test_suite_digest", "execute_sandboxed_tests",
]
_MAX_TIMEOUT_SECONDS = 300
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:+-]{0,255}@sha256:[0-9a-f]{64}\Z")


def _validated_plan(config, raw_plan: object, image: str, timeout_seconds: int) -> SandboxTestPlan:
    if any(os.environ.get(key) for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN")):
        raise SandboxUnavailableError("hidden-test verifier must not receive provider or GitHub credentials")
    if getattr(config, "api_key", ""):
        raise SandboxUnavailableError("sandbox Config must not contain provider credentials")
    plan = raw_plan if isinstance(raw_plan, SandboxTestPlan) else SandboxTestPlan.from_dict(raw_plan)
    policy = getattr(config, "effective_policy", None)
    if policy is None or plan.contract_sha256 != policy.contract_hash:
        raise ValueError("sandbox test plan does not match the active Task Contract")
    if not isinstance(image, str) or _IMAGE.fullmatch(image) is None:
        raise ValueError("sandbox image must be pinned by an immutable SHA-256 digest")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        raise ValueError("sandbox test timeout must be between 1 and 300 seconds")
    return plan


def _load_suite(config, plan: SandboxTestPlan, root: Path) -> list[tuple[str, bytes]]:
    workspace = config.workspace.resolve(strict=True)
    suite_root = root.resolve(strict=True)
    if workspace == suite_root or suite_root in workspace.parents or workspace in suite_root.parents:
        raise ValueError("hidden test suite and Agent workspace must be separate")
    suite = read_hidden_suite(root)
    if suite_digest(suite) != plan.test_suite_sha256:
        raise ValueError("hidden test suite digest does not match the trusted plan")
    return suite


def _run_hidden_suite(files: list[tuple[str, bytes]], candidate: CandidateSession) -> int:
    name, payload = next((name, payload) for name, payload in files if name.endswith(".py"))
    namespace = {"__name__": "_deepseek_hidden_acceptance", "__file__": name}
    code = compile(payload, f"<hidden:{name}>", "exec", dont_inherit=True, optimize=0)
    exec(code, namespace, namespace)
    runner = namespace.get("run")
    if not callable(runner):
        raise ValueError("hidden test module must define run(candidate, fixtures)")
    fixtures = {path: value for path, value in files if path != name}
    checks = runner(candidate, fixtures)
    if type(checks) is not int or not 1 <= checks <= 1000:
        raise ValueError("hidden test runner must return an assertion count from 1 to 1000")
    return checks


def _evaluate(candidate: CandidateSession, suite: list[tuple[str, bytes]]) -> dict:
    started = time.monotonic()
    result: dict
    try:
        with candidate:
            assertions = _run_hidden_suite(suite, candidate)
    except (AssertionError, SandboxCandidateError):
        result = {"status": "failed", "exit_code": 1}
    except SandboxTimeoutError:
        result = {"status": "timed_out", "exit_code": None}
    except SandboxOutputLimitError:
        result = {"status": "output_limit", "exit_code": None}
    except SandboxExecutionError:
        result = {"status": "runner_error", "exit_code": None}
    else:
        result = {"status": "passed", "exit_code": 0, "assertions": assertions}
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    result["output_bytes_captured"] = candidate.output_bytes
    return result


def execute_sandboxed_tests(
    config, raw_plan: object, tests_directory: Path, *, image: str, timeout_seconds: int = 120
) -> dict:
    """Verify the trusted suite and execute only its candidate calls in Docker."""
    plan = _validated_plan(config, raw_plan, image, timeout_seconds)
    suite = _load_suite(config, plan, Path(tests_directory))
    docker = _docker_binary()
    with tempfile.TemporaryDirectory(prefix="deepseek-test-sandbox-") as temporary:
        root = Path(temporary).resolve()
        source_root = root / "workspace"
        source_root.mkdir(mode=0o755)
        source_root.chmod(0o755)
        source_bytes = stage_source(config, plan, source_root)
        candidate = CandidateSession(docker, image, source_root, root, timeout_seconds)
        result = _evaluate(candidate, suite)
    if result["status"] == "passed":
        result.update(
            test_suite_sha256=plan.test_suite_sha256,
            source_files=len(plan.source_files),
            source_bytes=source_bytes,
            network="disabled",
            workspace_mount="read_only",
            hidden_tests="host_only_not_mounted",
        )
    return result
