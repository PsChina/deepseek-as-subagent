"""Fail-closed Linux container runner for trusted hidden functional tests.

Only a trusted host-authored plan can select source files. The test command is
fixed to Python unittest; no shell, model command, host credentials, or network
is passed into the container.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .file_io import read_workspace_text
from .policy_enforcement import bind_file_policy, path_is_allowed
from .workspace_guard import bind_workspace_identity

_MAX_PLAN_BYTES = 32_768
_MAX_SOURCE_FILES = 128
_MAX_SOURCE_BYTES = 20 * 1024 * 1024
_MAX_TEST_FILES = 128
_MAX_TEST_BYTES = 8 * 1024 * 1024
_MAX_TEST_FILE_BYTES = 1024 * 1024
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_TIMEOUT_SECONDS = 300
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}@sha256:[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")


class SandboxUnavailableError(RuntimeError):
    """The required Linux Docker isolation boundary is unavailable."""


@dataclass(frozen=True)
class SandboxSource:
    path: str
    sha256: str


@dataclass(frozen=True)
class SandboxTestPlan:
    contract_sha256: str
    test_suite_sha256: str
    source_files: tuple[SandboxSource, ...]

    @classmethod
    def from_dict(cls, raw: object) -> "SandboxTestPlan":
        if not isinstance(raw, dict) or set(raw) != {
            "version", "contract_sha256", "test_suite_sha256", "source_files"
        }:
            raise ValueError("sandbox test plan has an invalid schema")
        if type(raw["version"]) is not int or raw["version"] != 1:
            raise ValueError("unsupported sandbox test plan version")
        try:
            encoded = json.dumps(raw, allow_nan=False, ensure_ascii=True)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("sandbox test plan must be bounded JSON") from None
        if len(encoded.encode("utf-8")) > _MAX_PLAN_BYTES:
            raise ValueError("sandbox test plan exceeds 32 KiB")
        contract_hash = _digest(raw["contract_sha256"])
        suite_hash = _digest(raw["test_suite_sha256"])
        sources = _sources(raw["source_files"])
        return cls(contract_hash, suite_hash, sources)


def _digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("expected SHA-256 must be 64 lowercase hexadecimal digits")
    return value


def _path(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise ValueError("sandbox path must be a bounded relative string")
    parts = value.split("/")
    if ("\\" in value or ":" in value or value.startswith("/")
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or any(not part or part in {".", ".."} for part in parts)
            or any(part.endswith((" ", ".")) for part in parts)):
        raise ValueError("sandbox path is not normalized and portable")
    return value


def _sources(values: object) -> tuple[SandboxSource, ...]:
    if not isinstance(values, list) or not 1 <= len(values) <= _MAX_SOURCE_FILES:
        raise ValueError("sandbox plan requires 1–128 source files")
    sources: list[SandboxSource] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError("sandbox source entry has an invalid schema")
        path = _path(item["path"])
        if path.casefold() in seen:
            raise ValueError("sandbox source paths must be unique")
        seen.add(path.casefold())
        sources.append(SandboxSource(path, _digest(item["sha256"])))
    return tuple(sources)


def _read_test_files(root: Path) -> list[tuple[str, bytes]]:
    if root.is_symlink():
        raise ValueError("hidden test root must not be a symlink")
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("hidden test root must be a directory")
    files: list[tuple[str, bytes]] = []
    total = 0
    for current, directories, names in os.walk(resolved, followlinks=False):
        parent = Path(current)
        for name in directories:
            if (parent / name).is_symlink():
                raise ValueError("hidden test suite contains a symlink directory")
        for name in names:
            target = parent / name
            if target.is_symlink() or not target.is_file() or target.suffix not in {".py", ".json", ".txt"}:
                raise ValueError("hidden test suite contains a symlink or unsupported file")
            relative = _path(target.relative_to(resolved).as_posix())
            info = target.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_TEST_FILE_BYTES:
                raise ValueError("hidden test file is not a bounded regular file")
            payload = target.read_bytes()
            if len(payload) != info.st_size:
                raise ValueError("hidden test file changed while reading")
            total += len(payload)
            if total > _MAX_TEST_BYTES or len(files) >= _MAX_TEST_FILES:
                raise ValueError("hidden test suite exceeds its resource limits")
            files.append((relative, payload))
    if not files or not any(name.endswith(".py") for name, _ in files):
        raise ValueError("hidden test suite must contain Python tests")
    return sorted(files)


def _suite_digest(files: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, payload in sorted(files):
        path_bytes = name.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(4, "big"))
        digest.update(path_bytes)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


def compute_test_suite_digest(tests_directory: Path) -> str:
    """Compute a stable digest for a trusted, host-owned hidden test suite."""
    return _suite_digest(_read_test_files(Path(tests_directory)))


def _write_snapshot_file(root: Path, relative: str, payload: bytes) -> None:
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(payload)
    target.chmod(0o444)


def _stage_sources(config, plan: SandboxTestPlan, root: Path) -> int:
    policy = config.effective_policy
    total = 0
    with bind_workspace_identity(config.expected_workspace_identity):
        with bind_file_policy(policy, "Read"):
            for item in plan.source_files:
                if not path_is_allowed(item.path, policy.read_paths, policy.deny_paths):
                    raise ValueError("sandbox source is outside the contract read scope")
                content, identity = read_workspace_text(
                    config.workspace, item.path, strict_utf8=True,
                    reject_binary=True, require_canonical=True,
                )
                if identity.digest is None or identity.digest.hex() != item.sha256:
                    raise ValueError("sandbox source digest does not match the trusted plan")
                payload = content.encode("utf-8")
                total += len(payload)
                if total > _MAX_SOURCE_BYTES:
                    raise ValueError("sandbox source snapshot exceeds 20 MiB")
                _write_snapshot_file(root, item.path, payload)
    return total


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _docker_binary() -> str:
    if os.name != "posix" or not sys.platform.startswith("linux"):
        raise SandboxUnavailableError("sandbox tests require a Linux host")
    found = shutil.which("docker")
    if found is None:
        raise SandboxUnavailableError("Docker is unavailable; tests were not executed")
    resolved = Path(found).resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise SandboxUnavailableError("Docker executable is not trusted or executable")
    return str(resolved)


def _container_argv(docker: str, image: str, source: Path, tests: Path, cidfile: Path) -> list[str]:
    return [
        docker, "run", "--rm", "--pull=never", "--init", "--cidfile", str(cidfile),
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true", "--pids-limit=64",
        "--memory=512m", "--memory-swap=512m", "--cpus=1",
        "--user=65534:65534", "--ipc=none", "--stop-timeout=2",
        "--ulimit=core=0", "--ulimit=nofile=128:128",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--mount", f"type=bind,src={source},dst=/workspace,readonly",
        "--mount", f"type=bind,src={tests},dst=/tests,readonly",
        "--workdir=/workspace", "--env=HOME=/tmp",
        "--env=TMPDIR=/tmp", "--env=PYTHONDONTWRITEBYTECODE=1",
        image, "python", "-I", "-m", "unittest", "discover",
        "-s", "/tests", "-p", "test_*.py", "-v",
    ]


def _drain_output(stream, state: dict, overflow: threading.Event) -> None:
    while True:
        chunk = stream.read(8192)
        if not chunk:
            return
        remaining = _MAX_OUTPUT_BYTES - state["size"]
        if remaining > 0:
            state["chunks"].append(chunk[:remaining])
            state["size"] += min(len(chunk), remaining)
        if len(chunk) > remaining:
            overflow.set()


def _control_container(docker: str, cidfile: Path, env: dict[str, str]) -> None:
    try:
        container_id = cidfile.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return
    if _CONTAINER_ID.fullmatch(container_id) is None:
        return
    for action in ("kill", "rm"):
        args = [docker, action, "-f", container_id] if action == "rm" else [docker, action, container_id]
        try:
            subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, env=env, shell=False, timeout=3,
                           check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _stop_client(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _invoke_container(docker: str, image: str, source: Path, tests: Path,
                      temp_root: Path, timeout_seconds: int) -> dict:
    cidfile = temp_root / "container.id"
    docker_home = temp_root / "docker-home"
    docker_home.mkdir()
    env = {"PATH": os.defpath, "HOME": str(docker_home),
           "DOCKER_CONFIG": str(docker_home)}
    argv = _container_argv(docker, image, source, tests, cidfile)
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=env, cwd=temp_root, shell=False,
            close_fds=True, start_new_session=True,
        )
    except OSError:
        return {"status": "runner_error", "exit_code": None, "output": "",
                "duration_ms": int((time.monotonic() - started) * 1000)}
    state = {"chunks": [], "size": 0}
    overflow = threading.Event()
    reader = threading.Thread(target=_drain_output, args=(process.stdout, state, overflow),
                              daemon=True, name="sandbox-output-reader")
    reader.start()
    deadline = started + timeout_seconds
    timed_out = False
    while process.poll() is None:
        if overflow.is_set():
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.02)
    if timed_out or overflow.is_set():
        _control_container(docker, cidfile, env)
        _stop_client(process)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    reader.join(timeout=2)
    output = b"".join(state["chunks"]).decode("utf-8", errors="replace")
    elapsed = int((time.monotonic() - started) * 1000)
    status = "timed_out" if timed_out else "output_limit" if overflow.is_set() else (
        "passed" if process.returncode == 0 else "failed"
    )
    return {"status": status, "exit_code": process.returncode,
            "output": output[:8192], "output_bytes_captured": state["size"],
            "duration_ms": elapsed}


def execute_sandboxed_tests(config, raw_plan: object, tests_directory: Path, *,
                            image: str, timeout_seconds: int = 120) -> dict:
    """Run trusted hidden tests in a networkless, read-only Linux container.

    The source snapshot contains only exact, policy-readable files from the
    trusted plan. The hidden test directory is host-owned and never enters the
    Agent workspace or provider prompt. Model-supplied commands are not accepted.
    """
    plan = raw_plan if isinstance(raw_plan, SandboxTestPlan) else SandboxTestPlan.from_dict(raw_plan)
    policy = getattr(config, "effective_policy", None)
    if policy is None or plan.contract_sha256 != policy.contract_hash:
        raise ValueError("sandbox test plan does not match the active Task Contract")
    if not isinstance(image, str) or _IMAGE.fullmatch(image) is None:
        raise ValueError("sandbox image must be pinned by an immutable SHA-256 digest")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        raise ValueError("sandbox test timeout must be between 1 and 300 seconds")
    docker = _docker_binary()
    workspace = config.workspace.resolve(strict=True)
    tests_root = Path(tests_directory).resolve(strict=True)
    if _overlaps(workspace, tests_root):
        raise ValueError("hidden test suite and Agent workspace must be separate")
    test_files = _read_test_files(Path(tests_directory))
    actual_suite_hash = _suite_digest(test_files)
    if actual_suite_hash != plan.test_suite_sha256:
        raise ValueError("hidden test suite digest does not match the trusted plan")
    with tempfile.TemporaryDirectory(prefix="deepseek-test-sandbox-") as temporary:
        root = Path(temporary).resolve()
        source_root, test_root = root / "workspace", root / "tests"
        source_root.mkdir(mode=0o755)
        test_root.mkdir(mode=0o755)
        source_bytes = _stage_sources(config, plan, source_root)
        for name, payload in test_files:
            _write_snapshot_file(test_root, name, payload)
        result = _invoke_container(docker, image, source_root, test_root,
                                   root, timeout_seconds)
    result.update({"test_suite_sha256": actual_suite_hash,
                   "source_files": len(plan.source_files),
                   "source_bytes": source_bytes, "network": "disabled",
                   "workspace_mount": "read_only", "hidden_tests": "not_exposed_to_agent"})
    return result
