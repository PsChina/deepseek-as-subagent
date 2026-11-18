"""Bounded JSON exchange with a disposable, networkless Docker worker."""
from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_REQUEST_BYTES = 32 * 1024
_MAX_CALLS = 100
_FUNCTION = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")

_CANDIDATE_BOOTSTRAP = r"""
import contextlib
import importlib.util
import json
import os
import socket
import sys
from pathlib import Path

source = Path("/workspace/src/task.py")
protocol = sys.stdout

def send(value):
    protocol.write(json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n")
    protocol.flush()

def readonly(path):
    return bool(os.statvfs(path).f_flag & os.ST_RDONLY)

def probe():
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    tmp_writable = False
    try:
        marker = Path("/tmp/.deepseek-probe-" + str(os.getpid()))
        marker.write_text("probe")
        marker.unlink()
        tmp_writable = True
    except OSError:
        pass
    return {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "provider_credentials": any(os.environ.get(k) for k in (
            "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"
        )),
        "docker_socket": Path("/var/run/docker.sock").exists(),
        "workspace_readonly": readonly(source.parent),
        "root_readonly": readonly("/"),
        "tmp_writable": tmp_writable,
        "network_blocked": {name for _, name in socket.if_nameindex()} == {"lo"},
        "seccomp": status.get("Seccomp", "").strip() == "2",
        "no_new_privileges": status.get("NoNewPrivs", "").strip() == "1",
        "no_effective_caps": int(status.get("CapEff", "1"), 16) == 0,
    }

def load_candidate():
    spec = importlib.util.spec_from_file_location("candidate_task", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("candidate entrypoint cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(sys.stderr):
        spec.loader.exec_module(module)
    return module

# The host checks isolation before sending any candidate function request.
send({"ready": True, "isolation": probe()})
module = None
for line in sys.stdin:
    request = json.loads(line)
    request_id = request["id"]
    try:
        if module is None:
            module = load_candidate()
        function = getattr(module, request["name"])
        with contextlib.redirect_stdout(sys.stderr):
            result = function(*request["args"], **request["kwargs"])
        send({"id": request_id, "ok": True, "result": result})
    except Exception as error:
        send({"id": request_id, "ok": False, "error": type(error).__name__})
"""


class SandboxUnavailableError(RuntimeError):
    """The required isolation infrastructure is unavailable."""


class SandboxExecutionError(RuntimeError):
    """The candidate worker did not complete the protocol cleanly."""


class SandboxTimeoutError(SandboxExecutionError):
    """The candidate exceeded its wall-clock budget."""


class SandboxOutputLimitError(SandboxExecutionError):
    """The candidate exceeded its output budget."""


class SandboxCandidateError(SandboxExecutionError):
    """The candidate could not evaluate a test request."""


def docker_binary() -> str:
    if os.name != "posix" or not sys.platform.startswith("linux"):
        raise SandboxUnavailableError("sandbox tests require a Linux host")
    found = os.environ.get("DEEPSEEK_DOCKER_BIN") or shutil.which("docker")
    if not found:
        raise SandboxUnavailableError("Docker is unavailable; tests were not executed")
    try:
        resolved = Path(found).resolve(strict=True)
    except OSError:
        raise SandboxUnavailableError("Docker executable is unavailable") from None
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise SandboxUnavailableError("Docker executable is not a regular executable file")
    return str(resolved)


def container_argv(docker: str, image: str, source: Path, name: str) -> list[str]:
    # Docker applies its default seccomp policy with no seccomp override.
    return [
        docker, "run", "--rm", "--pull=never", "--interactive", "--init",
        "--name", name, "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true", "--log-driver=none",
        "--pids-limit=64", "--memory=512m", "--memory-swap=512m", "--cpus=1",
        "--user=65534:65534", "--ipc=none", "--stop-timeout=2",
        "--ulimit=core=0", "--ulimit=nofile=128:128",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--mount", f"type=bind,src={source},dst=/workspace,readonly",
        "--workdir=/workspace", "--env=HOME=/tmp", "--env=TMPDIR=/tmp",
        "--entrypoint=python", image, "-I", "-B", "-c", _CANDIDATE_BOOTSTRAP,
    ]


def _wait_io(descriptor: int, event: int, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SandboxTimeoutError("candidate execution timed out")
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, event)
        if not selector.select(remaining):
            raise SandboxTimeoutError("candidate execution timed out")


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate candidate response key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"nonfinite candidate response: {value}")


class _Protocol:
    def __init__(self, input_fd: int, output_fd: int) -> None:
        self.input_fd = input_fd
        self.output_fd = output_fd
        self.bytes_received = 0
        self.buffer = bytearray()
        os.set_blocking(self.input_fd, False)
        os.set_blocking(self.output_fd, False)

    def send(self, payload: dict, deadline: float) -> None:
        try:
            encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
        except (TypeError, ValueError, RecursionError):
            raise ValueError("candidate arguments must be JSON serializable") from None
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise ValueError("candidate request exceeds 32 KiB")
        pending = memoryview(encoded)
        while pending:
            _wait_io(self.input_fd, selectors.EVENT_WRITE, deadline)
            try:
                written = os.write(self.input_fd, pending)
            except BlockingIOError:
                continue
            except OSError:
                raise SandboxExecutionError("candidate input channel closed") from None
            pending = pending[written:]

    def receive(self, deadline: float) -> dict:
        while (end := self.buffer.find(b"\n")) < 0:
            self._read_chunk(deadline)
        line = self.buffer[:end]
        del self.buffer[:end + 1]
        try:
            response = json.loads(
                line.decode("utf-8"), object_pairs_hook=_unique_keys,
                parse_constant=_reject_constant,
            )
        except (UnicodeError, ValueError, RecursionError):
            raise SandboxExecutionError("candidate returned invalid protocol JSON") from None
        if not isinstance(response, dict):
            raise SandboxExecutionError("candidate response must be a JSON object")
        return response

    def _read_chunk(self, deadline: float) -> None:
        _wait_io(self.output_fd, selectors.EVENT_READ, deadline)
        try:
            chunk = os.read(self.output_fd, min(4096, _MAX_OUTPUT_BYTES + 1 - self.bytes_received))
        except BlockingIOError:
            return
        except OSError:
            raise SandboxExecutionError("candidate output channel closed") from None
        if not chunk:
            raise SandboxExecutionError("candidate worker exited without a response")
        self.bytes_received += len(chunk)
        if self.bytes_received > _MAX_OUTPUT_BYTES:
            raise SandboxOutputLimitError("candidate protocol output exceeded 64 KiB")
        self.buffer.extend(chunk)


def isolation_passed(probe: object) -> bool:
    if not isinstance(probe, dict):
        return False
    expected = {
        "uid": 65534, "gid": 65534, "provider_credentials": False, "docker_socket": False,
        "workspace_readonly": True, "root_readonly": True, "tmp_writable": True,
        "network_blocked": True, "seccomp": True, "no_new_privileges": True,
        "no_effective_caps": True,
    }
    return set(probe) == set(expected) and all(
        type(probe[name]) is type(value) and probe[name] == value for name, value in expected.items()
    )


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
    """Run one container and reject incomplete or uncorrelated responses."""

    def __init__(
        self, docker: str, image: str, source: Path, temp_root: Path, timeout_seconds: int
    ) -> None:
        self.docker, self.image, self.source = docker, image, source
        self.temp_root, self.timeout_seconds = temp_root, timeout_seconds
        self.name = "deepseek-test-" + uuid.uuid4().hex
        self.env: dict[str, str] = {}
        self.process: subprocess.Popen | None = None
        self.protocol: _Protocol | None = None
        self.deadline = 0.0
        self.calls = 0

    @property
    def output_bytes(self) -> int:
        return self.protocol.bytes_received if self.protocol is not None else 0

    def __enter__(self) -> CandidateSession:
        if "," in str(self.source):
            raise SandboxUnavailableError("sandbox mount path contains an unsupported comma")
        docker_home = self.temp_root / "docker-home"
        docker_home.mkdir()
        self.env = {"PATH": os.defpath, "HOME": str(docker_home), "DOCKER_CONFIG": str(docker_home)}
        argv = container_argv(self.docker, self.image, self.source, self.name)
        self.deadline = time.monotonic() + self.timeout_seconds
        try:
            self.process = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=self.env, cwd=self.temp_root,
                shell=False, close_fds=True, start_new_session=True, bufsize=0,
            )
        except OSError:
            raise SandboxUnavailableError("Docker candidate container could not start") from None
        try:
            self._initialize_protocol()
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def _initialize_protocol(self) -> None:
        assert self.process is not None
        assert self.process.stdin is not None and self.process.stdout is not None
        self.protocol = _Protocol(self.process.stdin.fileno(), self.process.stdout.fileno())
        ready = self.protocol.receive(min(self.deadline, time.monotonic() + 10))
        if set(ready) != {"ready", "isolation"} or ready["ready"] is not True:
            raise SandboxExecutionError("candidate worker failed its startup handshake")
        if not isolation_passed(ready["isolation"]):
            raise SandboxUnavailableError("candidate isolation probe did not meet policy")

    def __exit__(self, exc_type, exc, traceback) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            if exc_type is not None:
                self._abort()
            else:
                process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self._abort()
            if exc_type is None:
                raise SandboxExecutionError("candidate worker could not be closed cleanly") from None
        finally:
            if process.stdout is not None:
                process.stdout.close()
        if exc_type is None and process.returncode != 0:
            raise SandboxExecutionError("candidate worker exited unexpectedly")

    def _abort(self) -> None:
        assert self.process is not None
        _stop_client(self.process)
        try:
            subprocess.run(
                [self.docker, "rm", "-f", self.name], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.env,
                shell=False, timeout=3, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def call(self, function: str, *args, **kwargs):
        if not isinstance(function, str) or _FUNCTION.fullmatch(function) is None:
            raise ValueError("candidate function name is invalid")
        if self.protocol is None or self.calls >= _MAX_CALLS:
            raise SandboxExecutionError("candidate session is unavailable or call limit reached")
        self.calls += 1
        self.protocol.send({
            "id": self.calls, "name": function, "args": list(args), "kwargs": kwargs,
        }, self.deadline)
        reply = self.protocol.receive(self.deadline)
        if type(reply.get("id")) is not int or reply["id"] != self.calls:
            raise SandboxExecutionError("candidate returned an unexpected response ID")
        if reply.get("ok") is False and set(reply) == {"id", "ok", "error"}:
            raise SandboxCandidateError("candidate function raised an exception")
        if reply.get("ok") is not True or set(reply) != {"id", "ok", "result"}:
            raise SandboxExecutionError("candidate returned an invalid call response")
        return reply["result"]
