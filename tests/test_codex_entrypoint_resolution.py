"""Issue #31: the Windows installer must not guess the entrypoint spelling.

MSYS `test -e` resolves `deepseek-mcp` to `deepseek-mcp.exe`, but Python's
`Path.is_file()` does not, so a shell-side probe accepted a path the smoke test
then reported as missing and the whole install rolled back. Resolution belongs
to the Python runtime that executes the entrypoint.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT / "adapters" / "codex" / "resolve_entrypoint.py"
SMOKE = ROOT / "adapters" / "codex" / "mcp_smoke.py"
CODEX_INSTALL = ROOT / "adapters" / "codex" / "install.sh"

sys.path.insert(0, str(ROOT / "adapters" / "codex"))
import resolve_entrypoint


def _make_executable(path: Path) -> None:
    path.write_bytes(b"console script")
    if os.name == "posix":
        path.chmod(0o755)


class EntrypointResolutionTests(unittest.TestCase):
    def test_resolves_the_only_suffix_pip_actually_created(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw) / "Scripts"
            directory.mkdir()
            created = directory / "deepseek-mcp.exe"
            _make_executable(created)
            self.assertEqual(resolve_entrypoint.resolve(directory), created)

    def test_prefers_the_platform_native_spelling_when_both_exist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw) / "Scripts"
            directory.mkdir()
            plain = directory / "deepseek-mcp"
            suffixed = directory / "deepseek-mcp.exe"
            _make_executable(plain)
            _make_executable(suffixed)
            expected = suffixed if os.name == "nt" else plain
            self.assertEqual(resolve_entrypoint.resolve(directory), expected)

    def test_rejects_a_directory_named_like_the_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw) / "Scripts"
            (directory / "deepseek-mcp").mkdir(parents=True)
            self.assertIsNone(resolve_entrypoint.resolve(directory))

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not a Windows concept")
    def test_rejects_an_entrypoint_writable_by_others(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw) / "Scripts"
            directory.mkdir()
            created = directory / "deepseek-mcp"
            created.write_bytes(b"console script")
            created.chmod(0o777)
            self.assertIsNone(resolve_entrypoint.resolve(directory))

    def test_missing_entrypoint_is_reported_once_without_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            completed = subprocess.run(
                [sys.executable, str(RESOLVER), str(Path(raw) / "Scripts")],
                check=False, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stderr.strip().splitlines(), [
                f"deepseek-mcp entrypoint was not created in {Path(raw) / 'Scripts'}"
            ])
            self.assertNotIn("Traceback", completed.stderr)

    def test_reports_the_resolved_path_for_the_installer_to_use(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw) / "bin"
            directory.mkdir()
            created = directory / "deepseek-mcp"
            _make_executable(created)
            completed = subprocess.run(
                [sys.executable, str(RESOLVER), str(directory)],
                check=False, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), str(created))


class CodexInstallerWiringTests(unittest.TestCase):
    def test_entrypoint_is_resolved_after_install_and_before_use(self) -> None:
        script = CODEX_INSTALL.read_text(encoding="utf-8")
        install = script.index('"$PYBIN" -m pip install')
        resolved = script.index("resolve_entrypoint.py", install)
        smoke = script.index("mcp_smoke.py", resolved)
        registration = script.index('--command "$CLI"', smoke)
        self.assertLess(install, resolved)
        self.assertLess(resolved, smoke)
        self.assertLess(smoke, registration)

    def test_installer_no_longer_probes_the_entrypoint_from_the_shell(self) -> None:
        script = CODEX_INSTALL.read_text(encoding="utf-8")
        self.assertNotIn('[ -x "$CLI.exe" ]', script)
        self.assertNotIn('[ ! -e "$CLI" ]', script)
        self.assertNotIn('CLI="$VENV_BIN/deepseek-mcp"', script)


class SmokeFailureDiagnosticsTests(unittest.TestCase):
    def test_missing_command_error_names_the_real_sibling(self) -> None:
        if importlib.util.find_spec("mcp") is None:
            self.skipTest("the MCP SDK is not installed in this environment")
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _make_executable(directory / "deepseek-mcp.exe")
            completed = subprocess.run(
                [sys.executable, str(SMOKE), str(directory / "deepseek-mcp")],
                cwd=ROOT, check=False, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
            self.assertIn("MCP command does not exist", completed.stderr)
            self.assertIn("deepseek-mcp.exe", completed.stderr)


if __name__ == "__main__":
    unittest.main()
