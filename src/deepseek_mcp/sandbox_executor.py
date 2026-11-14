"""Fail-closed candidate-code container with host-only hidden acceptance tests.

Only trusted host code sees the hidden suite. The candidate container receives
the exact source snapshot and function inputs, never the test files or expected
outputs. No model-supplied shell command is accepted.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
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
_MAX_REQUEST_BYTES = 32 * 1024
_MAX_CALLS = 100
_MAX_TIMEOUT_SECONDS = 300
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:+-]{0,255}@sha256:[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FUNCTION = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")
_PROTOCOL_OVERFLOW = object()

_CANDIDATE_BOOTSTRAP = r"""
import contextlib, importlib.util, json, os, socket, sys
from pathlib import Path
protocol = sys.stdout
source = Path("/workspace/src/task.py")
spec = importlib.util.spec_from_file_location("candidate_task", source)
if spec is None or spec.loader is None:
    raise RuntimeError("candidate entrypoint cannot be loaded")
module = importlib.util.module_from_spec(spec)
with contextlib.redirect_stdout(sys.stderr):
    spec.loader.exec_module(module)
def send(value):
    protocol.write(json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n")
    protocol.flush()
def probe():
    write_denied = False
    try:
        with source.open("ab") as stream:
            stream.write(b"x")
    except OSError:
        write_denied = True
    root_denied = False
    try:
        Path("/.deepseek-probe").write_text("x")
        Path("/.deepseek-probe").unlink()
    except OSError:
        root_denied = True
    tmp_writable = False
    try:
        marker = Path("/tmp/.deepseek-probe-" + str(os.getpid()))
        marker.write_text("x")
        marker.unlink()
        tmp_writable = True
    except OSError:
        pass
    network_blocked = False
    try:
        connection = socket.create_connection(("1.1.1.1", 443), timeout=0.5)
        connection.close()
    except OSError:
        network_blocked = True
    return {
        "uid": os.getuid(),
        "provider_credentials": any(os.environ.get(k) for k in (
            "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN"
        )),
        "docker_socket": Path("/var/run/docker.sock").exists(),
        "workspace_write_denied": write_denied,
        "root_write_denied": root_denied,
        "tmp_writable": tmp_writable,
        "network_blocked": network_blocked,
    }
send({"ready": True})
for line in sys.stdin:
    try:
        request = json.loads(line)
        if request == {"op": "probe"}:
            send({"ok": True, "probe": probe()})
        elif (isinstance(request, dict) and set(request) == {"op", "name", "args", "kwargs"}
              and request["op"] == "call" and isinstance(request["name"], str)
              and isinstance(request["args"], list) and isinstance(request["kwargs"], dict)):
            function = getattr(module, request["name"], None)
            if not callable(function):
                raise AttributeError("candidate function is unavailable")
            with contextlib.redirect_stdout(sys.stderr):
                result = function(*request["args"], **request["kwargs"])
            send({"ok": True, "result": result})
        else:
            send({"ok": False, "error": "InvalidRequest"})
    except BaseException as error:
        try:
            send({"ok": False, "error": type(error).__name__})
        except BaseException:
            break
"""

class SandboxUnavailableError(RuntimeError):
    """The required Linux Docker isolation boundary is unavailable."""


class SandboxExecutionError(RuntimeError):
    """The candidate container failed or violated its protocol."""


class SandboxTimeoutError(SandboxExecutionError):
    """The candidate did not respond before the trusted deadline."""


class SandboxOutputLimitError(SandboxExecutionError):
    """The candidate exceeded the bounded protocol output."""


class SandboxCandidateError(SandboxExecutionError):
    """The candidate function raised an exception."""


@dataclass(frozen=True)
class SandboxSource:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        _path(self.path)
        _digest(self.sha256)


@dataclass(frozen=True)
class SandboxTestPlan:
    contract_sha256: str
    test_suite_sha256: str
    source_files: tuple[SandboxSource, ...]

    def __post_init__(self) -> None:
        _digest(self.contract_sha256)
        _digest(self.test_suite_sha256)
        if not isinstance(self.source_files, tuple) or len(self.source_files) != 1:
            raise ValueError("sandbox plan v1 requires exactly one entrypoint")
        if self.source_files[0].path != "src/task.py":
            raise ValueError("sandbox plan v1 entrypoint must be src/task.py")

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
        return cls(_digest(raw["contract_sha256"]), _digest(raw["test_suite_sha256"]),
                   _sources(raw["source_files"]))


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
    if not isinstance(values, list) or len(values) != 1:
        raise ValueError("sandbox plan v1 requires exactly one source file")
    sources = []
    for item in values:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError("sandbox source entry has an invalid schema")
        sources.append(SandboxSource(_path(item["path"]), _digest(item["sha256"])))
    return tuple(sources)


def _read_test_files(root: Path) -> list[tuple[str, bytes]]:
    if root.is_symlink():
        raise ValueError("hidden test root must not be a symlink")
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("hidden test root must be a directory")
    files, total = [], 0
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
    if sum(name.endswith(".py") for name, _ in files) != 1:
        raise ValueError("hidden suite must contain exactly one Python run(candidate) module")
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
    """Compute a stable digest for a trusted, host-only hidden test suite."""
    return _suite_digest(_read_test_files(Path(tests_directory)))


def _write_snapshot_file(root: Path, relative: str, payload: bytes) -> None:
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(payload)
    target.chmod(0o444)


def _stage_sources(config, plan: SandboxTestPlan, root: Path) -> int:
    policy, total = config.effective_policy, 0
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
    configured = os.environ.get("DEEPSEEK_DOCKER_BIN")
    found = configured or shutil.which("docker")
    if not found:
        raise SandboxUnavailableError("Docker is unavailable; tests were not executed")
    resolved = Path(found).resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise SandboxUnavailableError("Docker executable is not trusted or executable")
    return str(resolved)


def _container_argv(docker: str, image: str, source: Path,
                    cidfile: Path, name: str) -> list[str]:
    return [
        docker, "run", "--rm", "--pull=never", "--interactive", "--init",
        "--cidfile", str(cidfile), "--name", name, "--network=none",
        "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true", "--security-opt=seccomp=default",
        "--pids-limit=64", "--memory=512m", "--memory-swap=512m", "--cpus=1",
        "--user=65534:65534", "--ipc=none", "--stop-timeout=2",
        "--ulimit=core=0", "--ulimit=nofile=128:128",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--mount", f"type=bind,src={source},dst=/workspace,readonly",
        "--workdir=/workspace", "--env=HOME=/tmp", "--env=TMPDIR=/tmp",
        image, "python", "-I", "-B", "-c", _CANDIDATE_BOOTSTRAP,
    ]


def _read_protocol(stream, messages: queue.Queue) -> None:
    while True:
        line = stream.readline(_MAX_OUTPUT_BYTES + 1)
        if not line:
            messages.put(None)
            return
        if len(line) > _MAX_OUTPUT_BYTES or not line.endswith(b"\n"):
            messages.put(_PROTOCOL_OVERFLOW)
            return
        messages.put(line)


def _control_container(docker: str, cidfile: Path, env: dict[str, str], name: str) -> None:
    try:
        container_id = cidfile.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        container_id = name
    if _CONTAINER_ID.fullmatch(container_id) is None and re.fullmatch(
        r"deepseek-test-[0-9a-f]{32}", container_id
    ) is None:
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


class CandidateSession:
    """JSON-line RPC to candidate code; no hidden test files are mounted."""

    def __init__(self, docker: str, image: str, source: Path,
                 temp_root: Path, timeout_seconds: int):
        self.docker, self.image, self.source = docker, image, source
        self.temp_root, self.timeout_seconds = temp_root, timeout_seconds
        self.process = None
        self.messages: queue.Queue = queue.Queue(maxsize=1)
        self.reader = None
        self.name = "deepseek-test-" + uuid.uuid4().hex
        self.cidfile = temp_root / "container.id"
        self.env: dict[str, str] = {}
        self.deadline = 0.0
        self.calls = 0

    def __enter__(self):
        if "," in str(self.source) or "," in str(self.cidfile):
            raise SandboxUnavailableError("sandbox mount path contains an unsupported comma")
        docker_home = self.temp_root / "docker-home"
        docker_home.mkdir()
        self.env = {"PATH": os.defpath, "HOME": str(docker_home),
                    "DOCKER_CONFIG": str(docker_home)}
        argv = _container_argv(self.docker, self.image, self.source,
                               self.cidfile, self.name)
        self.deadline = time.monotonic() + self.timeout_seconds
        try:
            self.process = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=self.env, cwd=self.temp_root,
                shell=False, close_fds=True, start_new_session=True,
            )
        except OSError:
            raise SandboxUnavailableError("Docker candidate container could not start") from None
        self.reader = threading.Thread(
            target=_read_protocol, args=(self.process.stdout, self.messages),
            daemon=True, name="sandbox-candidate-reader",
        )
        self.reader.start()
        ready = self._receive(min(10, self.timeout_seconds))
        if ready != {"ready": True}:
            self._abort()
            raise SandboxExecutionError("candidate worker failed its startup handshake")
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self.process is None:
            return False
        try:
            if self.process.stdin:
                self.process.stdin.close()
            self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self._abort()
        if exc_type is None and self.process.returncode not in (0, None):
            raise SandboxExecutionError("candidate worker exited unexpectedly")
        return False

    def _abort(self) -> None:
        if self.process is None:
            return
        _control_container(self.docker, self.cidfile, self.env, self.name)
        _stop_client(self.process)

    def _receive(self, timeout: float) -> dict:
        remaining = min(timeout, self.deadline - time.monotonic())
        if remaining <= 0:
            self._abort()
            raise SandboxTimeoutError("candidate execution timed out")
        try:
            line = self.messages.get(timeout=remaining)
        except queue.Empty:
            self._abort()
            raise SandboxTimeoutError("candidate execution timed out") from None
        if line is _PROTOCOL_OVERFLOW:
            self._abort()
            raise SandboxOutputLimitError("candidate protocol output exceeded 64 KiB")
        if line is None:
            raise SandboxExecutionError("candidate worker exited without a response")
        try:
            response = json.loads(line)
        except (UnicodeError, ValueError):
            raise SandboxExecutionError("candidate returned invalid protocol JSON") from None
        if not isinstance(response, dict):
            raise SandboxExecutionError("candidate response must be a JSON object")
        return response

    def _request(self, payload: dict) -> dict:
        if self.process is None or self.process.stdin is None or self.calls >= _MAX_CALLS:
            raise SandboxExecutionError("candidate session is unavailable or call limit reached")
        encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8") + b"\n"
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise ValueError("candidate request exceeds 32 KiB")
        try:
            self.process.stdin.write(encoded)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise SandboxExecutionError("candidate input channel closed") from None
        self.calls += 1
        return self._receive(self.deadline - time.monotonic())

    def probe(self) -> dict:
        response = self._request({"op": "probe"})
        if set(response) != {"ok", "probe"} or response["ok"] is not True:
            raise SandboxExecutionError("candidate isolation probe failed")
        return response["probe"]

    def call(self, function: str, *args, **kwargs):
        if not isinstance(function, str) or _FUNCTION.fullmatch(function) is None:
            raise ValueError("candidate function name is invalid")
        if not isinstance(kwargs, dict) or not all(isinstance(k, str) for k in kwargs):
            raise ValueError("candidate keyword arguments must be a string-keyed object")
        try:
            json.dumps([args, kwargs], allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("candidate arguments must be JSON serializable") from None
        response = self._request({
            "op": "call", "name": function, "args": list(args), "kwargs": kwargs,
        })
        if response.get("ok") is not True:
            raise SandboxCandidateError(
                f"candidate function raised {response.get('error', 'UnknownError')}"
            )
        if set(response) != {"ok", "result"}:
            raise SandboxExecutionError("candidate returned an invalid call response")
        return response["result"]


def _probe_passed(probe: object) -> bool:
    if not isinstance(probe, dict):
        return False
    return (
        type(probe.get("uid")) is int and probe["uid"] != 0
        and probe.get("provider_credentials") is False
        and probe.get("docker_socket") is False
        and probe.get("workspace_write_denied") is True
        and probe.get("root_write_denied") is True
        and probe.get("tmp_writable") is True
        and probe.get("network_blocked") is True
    )


def _run_hidden_suite(files: list[tuple[str, bytes]], candidate: CandidateSession) -> int:
    python_files = [(name, payload) for name, payload in files if name.endswith(".py")]
    if len(python_files) != 1:
        raise ValueError("hidden suite must contain exactly one Python test module")
    name, payload = python_files[0]
    namespace = {"__name__": "_deepseek_hidden_acceptance", "__file__": name}
    code = compile(payload.decode("utf-8"), f"<hidden:{name}>", "exec")
    exec(code, namespace, namespace)
    runner = namespace.get("run")
    if not callable(runner):
        raise ValueError("hidden test module must define run(candidate, fixtures)")
    fixtures = {path: value for path, value in files if not path.endswith(".py")}
    checks = runner(candidate, fixtures)
    if checks is None:
        return 1
    if type(checks) is not int or not 1 <= checks <= 1000:
        raise ValueError("hidden test runner must return an assertion count from 1 to 1000")
    return checks


def execute_sandboxed_tests(config, raw_plan: object, tests_directory: Path, *,
                            image: str, timeout_seconds: int = 120) -> dict:
    """Run host-only hidden tests against a networkless candidate worker.

    Hidden test bytes are hashed and executed by the no-secret host verifier.
    Only the exact source snapshot is mounted into Docker; the candidate worker
    receives test inputs over JSON RPC but never the test files or expectations.
    """
    if os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        raise SandboxUnavailableError("hidden-test verifier must not receive provider credentials")
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
    docker = _docker_binary()
    workspace = config.workspace.resolve(strict=True)
    tests_root = Path(tests_directory).resolve(strict=True)
    if _overlaps(workspace, tests_root):
        raise ValueError("hidden test suite and Agent workspace must be separate")
    test_files = _read_test_files(Path(tests_directory))
    suite_hash = _suite_digest(test_files)
    if suite_hash != plan.test_suite_sha256:
        raise ValueError("hidden test suite digest does not match the trusted plan")
    with tempfile.TemporaryDirectory(prefix="deepseek-test-sandbox-") as temporary:
        root = Path(temporary).resolve()
        source_root = root / "workspace"
        source_root.mkdir(mode=0o755)
        source_bytes = _stage_sources(config, plan, source_root)
        started = time.monotonic()
        try:
            with CandidateSession(docker, image, source_root, root, timeout_seconds) as candidate:
                probe = candidate.probe()
                if not _probe_passed(probe):
                    raise SandboxUnavailableError("candidate isolation probe did not meet policy")
                assertions = _run_hidden_suite(test_files, candidate)
        except AssertionError as error:
            return {"status": "failed", "exit_code": 1, "error_type": type(error).__name__,
                    "duration_ms": int((time.monotonic() - started) * 1000)}
        except SandboxTimeoutError:
            return {"status": "timed_out", "exit_code": None,
                    "duration_ms": int((time.monotonic() - started) * 1000)}
        except SandboxOutputLimitError:
            return {"status": "output_limit", "exit_code": None,
                    "duration_ms": int((time.monotonic() - started) * 1000)}
        except SandboxCandidateError:
            return {"status": "failed", "exit_code": 1,
                    "duration_ms": int((time.monotonic() - started) * 1000)}
        except SandboxExecutionError:
            return {"status": "runner_error", "exit_code": None,
                    "duration_ms": int((time.monotonic() - started) * 1000)}
    return {"status": "passed", "exit_code": 0, "assertions": assertions,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "test_suite_sha256": suite_hash, "source_files": len(plan.source_files),
            "source_bytes": source_bytes, "network": "disabled",
            "workspace_mount": "read_only", "hidden_tests": "host_only_not_mounted"}
