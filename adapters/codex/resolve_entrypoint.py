#!/usr/bin/env python3
"""Print the console-script entrypoint pip actually created in a venv bin dir."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

NAME = "deepseek-mcp"
REPARSE_ATTRIBUTE = 0x400


def candidate_suffixes() -> tuple[str, ...]:
    """Platform-native spelling first, the other spelling as a fallback."""
    return (".exe", "") if os.name == "nt" else ("", ".exe")


def _is_private_regular_file(path: Path, info: os.stat_result) -> bool:
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        return False
    if int(getattr(info, "st_file_attributes", 0) or 0) & REPARSE_ATTRIBUTE:
        return False
    if os.name == "posix":
        writable_by_others = stat.S_IMODE(info.st_mode) & 0o022
        if info.st_uid not in {os.getuid(), 0} or writable_by_others:
            return False
    return os.access(path, os.X_OK)


def resolve(directory: Path) -> Path | None:
    """Return the real entrypoint, or None when the install did not create one.

    MSYS `test -e` resolves `deepseek-mcp` to `deepseek-mcp.exe`, while
    `Path.is_file()` does not, so the installer must not pick the spelling
    itself. This runs under the venv interpreter that will execute the result.
    """
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            return None
    except OSError:
        return None
    for suffix in candidate_suffixes():
        candidate = directory / f"{NAME}{suffix}"
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if _is_private_regular_file(candidate, info):
            return candidate
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <venv-bin-directory>", file=sys.stderr)
        return 2
    resolved = resolve(Path(argv[1]))
    if resolved is None:
        print(f"{NAME} entrypoint was not created in {argv[1]}", file=sys.stderr)
        return 1
    print(resolved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
