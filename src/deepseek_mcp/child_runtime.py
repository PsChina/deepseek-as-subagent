"""Isolated launch policy for trusted deepseek-mcp helper processes."""
from __future__ import annotations

import os
import stat
import sys
import sysconfig
from pathlib import Path

_AUDITED_CHILDREN = frozenset(
    {
        "deepseek_mcp.provider_child",
        "deepseek_mcp.tool_child",
    }
)
_BOOTSTRAP = (
    "import runpy,sys;"
    "count=int(sys.argv[1]);"
    "roots=sys.argv[2:2+count];"
    "module=sys.argv[2+count];"
    "args=sys.argv[3+count:];"
    "sys.path[:0]=roots;"
    "sys.argv=[module,*args];"
    "runpy.run_module(module,run_name='__main__',alter_sys=True)"
)


class ChildRuntimeError(RuntimeError):
    """A helper process cannot be launched from a trusted runtime."""


def _resolved_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError) as exc:
        raise ChildRuntimeError("trusted Python runtime path is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ChildRuntimeError("trusted Python runtime path is not a directory")
    return resolved


def child_import_roots() -> tuple[Path, ...]:
    """Return explicit import roots without processing cwd, user site, or .pth files."""
    package_root = Path(__file__).resolve(strict=True).parents[1]
    configured = sysconfig.get_paths()
    candidates = [package_root]
    candidates.extend(Path(configured[name]) for name in ("purelib", "platlib"))
    roots: list[Path] = []
    for candidate in candidates:
        resolved = _resolved_directory(candidate)
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def child_working_directory() -> Path:
    """Use the base interpreter directory rather than an untrusted workspace cwd."""
    return _resolved_directory(Path(sys.base_prefix))


def isolated_child_argv(module: str, *arguments: str) -> list[str]:
    if module not in _AUDITED_CHILDREN:
        raise ChildRuntimeError("helper module is not approved")
    roots = [str(path) for path in child_import_roots()]
    return [
        str(Path(sys.executable).resolve(strict=True)),
        "-I",
        "-S",
        "-c",
        _BOOTSTRAP,
        str(len(roots)),
        *roots,
        module,
        *arguments,
    ]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def trusted_runtime_paths() -> tuple[Path, ...]:
    """Paths that must stay outside a workspace writable by the delegated model."""
    candidates = [
        Path(sys.executable),
        Path(sys.prefix),
        Path(sys.base_prefix),
        child_working_directory(),
        *child_import_roots(),
    ]
    resolved: list[Path] = []
    for candidate in candidates:
        try:
            path = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ChildRuntimeError("trusted Python runtime path is unavailable") from exc
        if path not in resolved:
            resolved.append(path)
    return tuple(resolved)


def runtime_is_within_workspace(workspace: Path) -> bool:
    root = workspace.resolve(strict=True)
    return any(_inside(path, root) for path in trusted_runtime_paths())


def sanitized_python_environment(environment: dict[str, str]) -> dict[str, str]:
    """Drop Python import controls even though isolated mode also ignores them."""
    result = dict(environment)
    for name in tuple(result):
        if name == "PYTHONPATH" or name == "PYTHONHOME" or name.startswith("PYTHONUSERBASE"):
            result.pop(name, None)
    result["PYTHONIOENCODING"] = "utf-8"
    return result
