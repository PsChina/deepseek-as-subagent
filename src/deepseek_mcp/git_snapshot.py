"""Build history-free standalone Git metadata for a disposable snapshot."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from .trusted_executable import TrustedExecutableError, validate_trusted_executable
from .hard_deadline import Deadline, remaining

_ENGINE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
_QUERY_BYTES = 4096
_METADATA_BASE_BYTES = 4 * 1024 * 1024


class GitSnapshotError(RuntimeError):
    pass


def validate_git_marker_info(info: os.stat_result) -> bool:
    """Validate root .git metadata already obtained from an anchored directory."""
    if stat.S_ISLNK(info.st_mode):
        raise GitSnapshotError("Workspace Git marker must not be a symlink")
    if stat.S_ISDIR(info.st_mode):
        if os.name == "posix" and info.st_uid != os.getuid():
            raise GitSnapshotError("Workspace Git directory has unsafe ownership")
        return True
    if not stat.S_ISREG(info.st_mode):
        raise GitSnapshotError("Workspace Git marker has an unsupported type")
    if info.st_size > _QUERY_BYTES:
        raise GitSnapshotError("Linked-worktree marker is too large")
    if os.name == "posix" and info.st_uid != os.getuid():
        raise GitSnapshotError("Linked-worktree marker has unsafe ownership")
    return True


def _git_binary(workspace: Path) -> str:
    found = shutil.which("git", path=_ENGINE_PATH)
    if found is None:
        raise GitSnapshotError("Git metadata exists but trusted Git was not found")
    try:
        return str(validate_trusted_executable(found, workspace))
    except TrustedExecutableError as error:
        raise GitSnapshotError(f"Git host executable is not trusted: {error}") from None


def _environment(root: Path) -> dict[str, str]:
    return {
        "PATH": _ENGINE_PATH,
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(root.resolve(strict=True)),
        "GIT_CONFIG_KEY_1": "gc.auto",
        "GIT_CONFIG_VALUE_1": "0",
        "GIT_CONFIG_KEY_2": "maintenance.auto",
        "GIT_CONFIG_VALUE_2": "false",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "deepseek-mcp snapshot",
        "GIT_AUTHOR_EMAIL": "snapshot@localhost",
        "GIT_AUTHOR_DATE": "@0 +0000",
        "GIT_COMMITTER_NAME": "deepseek-mcp snapshot",
        "GIT_COMMITTER_EMAIL": "snapshot@localhost",
        "GIT_COMMITTER_DATE": "@0 +0000",
        "LC_ALL": "C",
        "LANG": "C",
    }


def _limited_command(command: list[str], file_limit: int | None) -> list[str]:
    if file_limit is None or os.name != "posix":
        return command
    blocks = max(1, file_limit // 512)
    return [
        "/bin/sh",
        "-c",
        'ulimit -f "$1"; shift; exec "$@"',
        "deepseek-git-limit",
        str(blocks),
        *command,
    ]


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        pass
    process.communicate()


def _run(
    git: str,
    root: Path,
    arguments: list[str],
    deadline: Deadline,
    *,
    input_data: bytes | None = None,
    output: bool = False,
    file_limit: int | None = None,
    allowed: tuple[int, ...] = (0,),
) -> tuple[int, str]:
    seconds = remaining(deadline)
    if seconds <= 0:
        raise GitSnapshotError("Git snapshot exceeded its time limit")
    command = [git, "-C", str(root), *arguments]
    process = subprocess.Popen(
        _limited_command(command, file_limit),
        stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE if output else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_environment(root),
        shell=False,
        # Inherit the supervised tool process group so its hard wall also
        # terminates Git if the outer tool is cancelled during snapshotting.
        start_new_session=False,
    )
    try:
        stdout, _ = process.communicate(input_data, timeout=seconds)
    except subprocess.TimeoutExpired:
        _kill_process(process)
        raise GitSnapshotError("Git snapshot exceeded its time limit") from None
    except BaseException:
        _kill_process(process)
        raise
    if process.returncode not in allowed:
        raise GitSnapshotError("Git could not materialize safe snapshot metadata")
    data = stdout or b""
    if len(data) > _QUERY_BYTES:
        raise GitSnapshotError("Git metadata query exceeded its output limit")
    try:
        return process.returncode, data.decode("ascii").strip()
    except UnicodeDecodeError:
        raise GitSnapshotError("Git metadata query returned invalid text") from None


def _freeze_and_account(path: Path, budget) -> None:
    budget.add_directory()
    root_depth = len(path.parts)
    for current, directories, files in os.walk(path, topdown=False, followlinks=False):
        depth = len(Path(current).parts) - root_depth + 2
        for name in files:
            entry = Path(current) / name
            info = entry.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise GitSnapshotError("Generated Git metadata is not regular-file-only")
            budget.add_entry(depth, name)
            budget.add_bytes(info.st_size)
            entry.chmod(0o444)
        for name in directories:
            entry = Path(current) / name
            info = entry.lstat()
            if not stat.S_ISDIR(info.st_mode) or entry.is_symlink():
                raise GitSnapshotError("Generated Git metadata contains a linked directory")
            budget.add_entry(depth, name)
            budget.add_directory()
            entry.chmod(0o555)
    path.chmod(0o555)


def _worst_case_metadata_bytes(budget) -> int:
    """Conservative logical/allocation bound before Git creates any metadata."""
    blob_bytes = (
        budget.bytes_copied
        + (budget.bytes_copied // 4096)
        + (budget.bytes_copied // 16384)
        + (budget.files * 128)
    )
    index_bytes = budget.path_bytes + (budget.files * 160) + 4096
    tree_bytes = budget.tree_name_bytes + (budget.entries * 96)
    allocation_slack = budget.allocation_unit * (
        (2 * budget.entries) + (2 * budget.directories) + 512
    )
    return (
        blob_bytes
        + (2 * index_bytes)
        + tree_bytes
        + allocation_slack
        + _METADATA_BASE_BYTES
    )


def materialize_git_snapshot(
    workspace: Path,
    snapshot: Path,
    budget,
    *,
    max_bytes: int,
    deadline: Deadline,
) -> None:
    """Create a clean root commit solely from the already-safe copied files."""
    if not budget.git_repository:
        return
    git = _git_binary(snapshot)
    remaining = max_bytes - budget.bytes_copied
    if remaining <= _QUERY_BYTES:
        raise GitSnapshotError("Git snapshot has no remaining byte budget")
    if _worst_case_metadata_bytes(budget) > remaining:
        raise GitSnapshotError(
            "Git snapshot metadata could exceed the aggregate disk budget"
        )
    _run(git, snapshot, ["init", "--quiet", "--template="], deadline)
    _run(git, snapshot, ["add", "--all"], deadline, file_limit=remaining)
    _run(
        git,
        snapshot,
        [
            "commit", "--quiet", "--allow-empty", "--no-gpg-sign",
            "-m", "Current workspace baseline",
        ],
        deadline,
        file_limit=remaining,
    )
    _freeze_and_account(snapshot / ".git", budget)
