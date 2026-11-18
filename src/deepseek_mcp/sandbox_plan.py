"""Trusted sandbox plans and bounded input snapshots."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .file_identity import validate_readable_file
from .file_io import read_workspace_text
from .policy_enforcement import bind_file_policy, path_is_allowed
from .workspace_guard import bind_workspace_identity

_ENTRYPOINT = "src/task.py"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_TEST_ENTRIES = 256
_MAX_TEST_FILES = 128
_MAX_TEST_BYTES = 8 * 1024 * 1024
_MAX_TEST_FILE_BYTES = 1024 * 1024


def _validate_digest(value: object) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("expected SHA-256 must be 64 lowercase hexadecimal digits")


@dataclass(frozen=True)
class SandboxSource:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or self.path != _ENTRYPOINT:
            raise ValueError(f"sandbox plan v1 entrypoint must be {_ENTRYPOINT}")
        _validate_digest(self.sha256)


@dataclass(frozen=True)
class SandboxTestPlan:
    contract_sha256: str
    test_suite_sha256: str
    source_files: tuple[SandboxSource, ...]

    def __post_init__(self) -> None:
        _validate_digest(self.contract_sha256)
        _validate_digest(self.test_suite_sha256)
        if (
            not isinstance(self.source_files, tuple)
            or len(self.source_files) != 1
            or not isinstance(self.source_files[0], SandboxSource)
        ):
            raise ValueError("sandbox plan v1 requires exactly one source file")

    @classmethod
    def from_dict(cls, raw: object) -> SandboxTestPlan:
        if not isinstance(raw, dict) or len(raw) != 4 or set(raw) != {
            "version", "contract_sha256", "test_suite_sha256", "source_files"
        }:
            raise ValueError("sandbox test plan has an invalid schema")
        if type(raw["version"]) is not int or raw["version"] != 1:
            raise ValueError("unsupported sandbox test plan version")
        sources = raw["source_files"]
        if not isinstance(sources, list) or len(sources) != 1:
            raise ValueError("sandbox plan v1 requires exactly one source file")
        item = sources[0]
        if not isinstance(item, dict) or len(item) != 2 or set(item) != {"path", "sha256"}:
            raise ValueError("sandbox source entry has an invalid schema")
        source = SandboxSource(item["path"], item["sha256"])
        return cls(raw["contract_sha256"], raw["test_suite_sha256"], (source,))


def stage_source(config, plan: SandboxTestPlan, root: Path) -> int:
    policy = config.effective_policy
    item = plan.source_files[0]
    if not path_is_allowed(item.path, policy.read_paths, policy.deny_paths):
        raise ValueError("sandbox source is outside the contract read scope")
    with bind_workspace_identity(config.expected_workspace_identity), bind_file_policy(policy, "Read"):
        content, identity = read_workspace_text(
            config.workspace, item.path, strict_utf8=True,
            reject_binary=True, require_canonical=True,
        )
    if identity.digest is None or identity.digest.hex() != item.sha256:
        raise ValueError("sandbox source digest does not match the trusted plan")
    payload = content.encode("utf-8")
    target = root / item.path
    target.parent.mkdir(mode=0o755)
    target.parent.chmod(0o755)
    with target.open("xb") as handle:
        handle.write(payload)
    target.chmod(0o444)
    return len(payload)


def _is_link(info: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)


def _suite_entries(root: Path) -> Iterator[tuple[Path, os.stat_result]]:
    pending = [root]
    count = 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_TEST_ENTRIES:
                    raise ValueError("hidden test suite exceeds its entry limit")
                path = Path(entry.path)
                info = path.lstat()
                if _is_link(info):
                    raise ValueError("hidden test suite contains a symlink or reparse point")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                else:
                    yield path, info


def _read_suite_file(path: Path, info: os.stat_result) -> bytes:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_size > _MAX_TEST_FILE_BYTES
        or path.suffix not in {".py", ".json", ".txt"}
    ):
        raise ValueError("hidden test suite contains an unsupported or oversized file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        before = os.fstat(handle.fileno())
        validate_readable_file(before, previous=info)
        contents = handle.read(_MAX_TEST_FILE_BYTES + 1)
        validate_readable_file(os.fstat(handle.fileno()), previous=before)
    if len(contents) != before.st_size or len(contents) > _MAX_TEST_FILE_BYTES:
        raise ValueError("hidden test file changed while reading")
    return contents


def read_hidden_suite(root: Path) -> list[tuple[str, bytes]]:
    info = root.lstat()
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("hidden test root must be a real directory, without symlinks")
    resolved = root.resolve(strict=True)
    files: list[tuple[str, bytes]] = []
    total = 0
    for path, entry_info in _suite_entries(resolved):
        payload = _read_suite_file(path, entry_info)
        total += len(payload)
        if total > _MAX_TEST_BYTES or len(files) >= _MAX_TEST_FILES:
            raise ValueError("hidden test suite exceeds its resource limits")
        files.append((path.relative_to(resolved).as_posix(), payload))
    if sum(name.endswith(".py") for name, _ in files) != 1:
        raise ValueError("hidden suite must contain exactly one Python run(candidate, fixtures) module")
    return sorted(files)


def suite_digest(files: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, payload in files:
        path_bytes = name.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(4, "big"))
        digest.update(path_bytes)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


def compute_test_suite_digest(tests_directory: Path) -> str:
    """Compute a stable digest for a trusted, host-only hidden test suite."""
    return suite_digest(read_hidden_suite(Path(tests_directory)))
