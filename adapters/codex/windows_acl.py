"""Compatibility import for the package-owned Windows ACL implementation."""
from pathlib import Path
import sys

try:
    from deepseek_mcp.windows_acl import WindowsAclError, validate_private_handle
except ModuleNotFoundError as exc:  # Direct source-tree script execution.
    if exc.name != "deepseek_mcp":
        raise
    source = Path(__file__).resolve().parents[2] / "src"
    sys.path.insert(0, str(source))
    from deepseek_mcp.windows_acl import WindowsAclError, validate_private_handle

__all__ = ["WindowsAclError", "validate_private_handle"]
