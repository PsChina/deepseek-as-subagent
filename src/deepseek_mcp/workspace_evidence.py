"""Bounded host-side readback of task mutation results.

This is a post-run filesystem observation, not an authenticated audit log and
not proof that the filesystem will remain unchanged.
"""
from __future__ import annotations

import re
import os

from . import file_io
from .policy_enforcement import bind_file_policy, path_is_allowed
from .workspace_guard import bind_workspace_identity

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _readback_one(config, path: str, record: dict) -> dict:
    policy = config.effective_policy
    expected = record.get("sha256")
    result = {"path": path, "expected_sha256": expected, "status": "not_verifiable"}
    if (
        record.get("status") != "committed"
        or not isinstance(expected, str)
        or _SHA256.fullmatch(expected) is None
        or not path_is_allowed(path, policy.write_paths, policy.deny_paths)
    ):
        return result
    try:
        with bind_workspace_identity(config.expected_workspace_identity), bind_file_policy(policy, "Write"):
            root, relative = file_io._location(
                config.workspace, path, require_canonical=True
            )
            if relative.as_posix() != path:
                return result
            if os.name == "nt":
                _, identity = file_io._read_windows(
                    root, relative, strict_utf8=False, reject_binary=False
                )
            else:
                _, identity = file_io._read_posix(
                    root, relative, strict_utf8=False, reject_binary=False
                )
        actual = identity.digest.hex() if identity.digest is not None else None
    except Exception:
        # Missing, unsafe or concurrently replaced paths require review.
        return result
    result["observed_sha256"] = actual
    result["status"] = "matched" if actual == expected else "changed"
    return result


def collect_workspace_evidence(config, mutations: list[dict]) -> list[dict]:
    """Observe the last recorded version of each file without returning content."""
    if getattr(config, "effective_policy", None) is None:
        return []
    latest: dict[str, dict] = {}
    for item in mutations:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if isinstance(path, str) and path:
            latest[path] = item
    return [_readback_one(config, path, record) for path, record in latest.items()]
