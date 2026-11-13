"""Bounded host-side readback of task mutation results.

This is a post-run filesystem observation, not an authenticated audit log and
not proof that the filesystem will remain unchanged.
"""
from __future__ import annotations

import re

from .file_io import read_workspace_text
from .policy_enforcement import path_is_allowed
from .workspace_guard import bind_workspace_identity

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def collect_workspace_evidence(config, mutations: list[dict]) -> list[dict]:
    """Re-read the last committed version of each changed path via safe file I/O.

    Earlier versions of a repeatedly edited file are not compared with its
    final contents. An invalid or inaccessible path is never read.
    """
    policy = getattr(config, "effective_policy", None)
    if policy is None:
        return []
    latest: dict[str, dict] = {}
    for item in mutations:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if isinstance(path, str) and path:
            latest[path] = item

    evidence = []
    for path, record in latest.items():
        expected = record.get("sha256")
        observation = {"path": path, "expected_sha256": expected, "status": "not_verifiable"}
        if (
            record.get("status") != "committed"
            or not isinstance(expected, str)
            or _SHA256.fullmatch(expected) is None
            or not path_is_allowed(path, policy.write_paths, policy.deny_paths)
        ):
            evidence.append(observation)
            continue
        try:
            with bind_workspace_identity(config.expected_workspace_identity):
                _, identity = read_workspace_text(config.workspace, path)
            actual = identity.digest.hex() if identity.digest is not None else None
        except Exception:
            # Missing, inaccessible, concurrently replaced or otherwise unsafe
            # file states require review. Do not include filesystem error text.
            evidence.append(observation)
            continue
        observation["observed_sha256"] = actual
        observation["status"] = "matched" if actual == expected else "changed"
        evidence.append(observation)
    return evidence
