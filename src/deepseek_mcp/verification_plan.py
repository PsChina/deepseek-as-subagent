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
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
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


@dataclass(frozen=True)
class VerificationPlan:
    contract_sha256: str
    artifacts: tuple[ArtifactExpectation, ...]
    success_checks: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object) -> "VerificationPlan":
        data = _fields(raw, {"version", "contract_sha256", "artifacts"},
                       {"success_checks"}, "verification plan")
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError("unsupported verification plan version")
        try:
            encoded = json.dumps(data, allow_nan=False, ensure_ascii=True)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("verification plan is not bounded JSON") from None
        if len(encoded.encode("utf-8")) > _PLAN_LIMIT:
            raise ValueError("verification plan exceeds 32 KiB")
        return cls(_digest(data["contract_sha256"]),
                   _artifacts(data["artifacts"]),
                   _checks(data.get("success_checks", [])))


def _artifacts(values: object) -> tuple[ArtifactExpectation, ...]:
    if not isinstance(values, list) or not 1 <= len(values) <= _ARTIFACT_LIMIT:
        raise ValueError("verification plan requires 1–64 artifacts")
    artifacts: list[ArtifactExpectation] = []
    seen: set[str] = set()
    for value in values:
        item = _fields(value, {"path", "sha256", "role"}, set(), "artifact")
        path = _path(item["path"])
        if path.casefold() in seen:
            raise ValueError("duplicate artifact path")
        seen.add(path.casefold())
        role = item["role"]
        if role not in ("output", "unchanged") or not isinstance(role, str):
            raise ValueError("artifact role must be output or unchanged")
        artifacts.append(ArtifactExpectation(path, _digest(item["sha256"]), role))
    return tuple(artifacts)


def _checks(values: object) -> tuple[str, ...]:
    if not isinstance(values, list) or len(values) > _CHECK_LIMIT:
        raise ValueError("verification plan has too many checks")
    names = tuple(_name(value) for value in values)
    if len(names) != len(set(names)):
        raise ValueError("duplicate check names")
    return names
