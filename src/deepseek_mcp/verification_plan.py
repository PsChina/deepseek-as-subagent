"""Strict, trusted-host-authored plans for independent artifact verification.

Plans are not model tool requests. They deliberately contain no executable code.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

_PLAN_LIMIT = 32_768
_ARTIFACT_LIMIT = 64
_CHECK_LIMIT = 16
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _fields(value: object, required: set[str], optional: set[str], name: str) -> dict:
    if (
        not isinstance(value, dict)
        or len(value) > len(required) + len(optional)
        or not all(isinstance(key, str) for key in value)
    ):
        raise ValueError(f"{name} must be a JSON object")
    if not required.issubset(value) or set(value) - required - optional:
        raise ValueError(f"{name} has missing or unexpected fields")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("expected SHA-256 must be 64 lowercase hexadecimal digits")
    return value


def _path(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise ValueError("artifact path must be a bounded relative string")
    parts = value.split("/")
    if (
        "\\" in value or ":" in value
        or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
        or any(not part or part in {".", ".."} for part in parts)
        or any(part.endswith((" ", ".")) for part in parts)
    ):
        raise ValueError("artifact path is not normalized and portable")
    return value


def _name(value: object) -> str:
    if (
        not isinstance(value, str) or not 1 <= len(value) <= 256
        or value != value.strip()
        or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
    ):
        raise ValueError("test check name is invalid")
    return value


@dataclass(frozen=True)
class ArtifactExpectation:
    path: str
    sha256: str
    role: str  # "output" or "unchanged"

    def __post_init__(self) -> None:
        _path(self.path)
        _digest(self.sha256)
        if not isinstance(self.role, str) or self.role not in ("output", "unchanged"):
            raise ValueError("artifact role must be output or unchanged")


@dataclass(frozen=True)
class VerificationPlan:
    contract_sha256: str
    artifacts: tuple[ArtifactExpectation, ...]
    success_checks: tuple[str, ...]

    def __post_init__(self) -> None:
        _digest(self.contract_sha256)
        _validate_artifacts(self.artifacts)
        _validate_checks(self.success_checks)
        payload = {
            "version": 1,
            "contract_sha256": self.contract_sha256,
            "artifacts": [
                {"path": item.path, "sha256": item.sha256, "role": item.role}
                for item in self.artifacts
            ],
            "success_checks": list(self.success_checks),
        }
        if len(json.dumps(payload, ensure_ascii=True)) > _PLAN_LIMIT:
            raise ValueError("verification plan exceeds 32 KiB")

    @classmethod
    def from_dict(cls, raw: object) -> "VerificationPlan":
        data = _fields(raw, {"version", "contract_sha256", "artifacts"},
                       {"success_checks"}, "verification plan")
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError("unsupported verification plan version")
        checks = data.get("success_checks", [])
        if not isinstance(checks, list) or len(checks) > _CHECK_LIMIT:
            raise ValueError("verification plan has too many checks or an invalid check list")
        return cls(data["contract_sha256"], _artifacts(data["artifacts"]), tuple(checks))


def _artifacts(values: object) -> tuple[ArtifactExpectation, ...]:
    if not isinstance(values, list) or not 1 <= len(values) <= _ARTIFACT_LIMIT:
        raise ValueError("verification plan requires 1–64 artifacts")
    artifacts = []
    for value in values:
        item = _fields(value, {"path", "sha256", "role"}, set(), "artifact")
        artifacts.append(ArtifactExpectation(item["path"], item["sha256"], item["role"]))
    return tuple(artifacts)


def _validate_artifacts(values: tuple[ArtifactExpectation, ...]) -> None:
    if not isinstance(values, tuple) or not 1 <= len(values) <= _ARTIFACT_LIMIT:
        raise ValueError("verification plan requires 1–64 artifacts")
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, ArtifactExpectation):
            raise ValueError("verification plan contains an invalid artifact")
        path = item.path.casefold()
        if path in seen:
            raise ValueError("duplicate artifact path")
        seen.add(path)


def _validate_checks(values: tuple[str, ...]) -> None:
    if not isinstance(values, tuple) or len(values) > _CHECK_LIMIT:
        raise ValueError("verification plan has too many checks or an invalid check list")
    for value in values:
        _name(value)
    if len(values) != len(set(values)):
        raise ValueError("duplicate check names")
