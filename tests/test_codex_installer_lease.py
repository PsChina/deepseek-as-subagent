from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import tomlkit

from adapters.codex.configure import FORWARDED_ENV_VARS


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "POSIX process-group and SIGKILL semantics")
class CodexInstallerLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.home = self.root / "home"
        self.fake_bin = self.root / "bin"
        self.home.mkdir()
        self.fake_bin.mkdir()
        self.marker = self.root / "venv-started"
        self.release_marker = self.root / "release-venv"
        self.rollback_marker = self.root / "rollback-started"
        self.rollback_release = self.root / "release-rollback"
        self.gate_after_venv = False
        self.fail_prune = False
        self.wrong_registration = False
        self.double_signal_action = ""
        self.codex = self.fake_bin / "codex"
        self.python = self.fake_bin / "python3.12"
        self._write_executable(self.codex, "#!/usr/bin/env bash\nexit 0\n")
        self._write_executable(
            self.python,
            '''#!/usr/bin/env bash
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
    : > "$BLOCK_MARKER"
    while :; do sleep 1; done
fi
exec "$REAL_PYTHON" "$@"
''',
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def _write_executable(path: Path, source: str) -> None:
        path.write_text(source, encoding="utf-8")
        path.chmod(0o700)

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update(
            {
                "HOME": str(self.home),
                "CODEX_HOME": str(self.home / ".codex"),
                "DEEPSEEK_CODEX_BIN": str(self.codex),
                "REAL_PYTHON": sys.executable,
                "BLOCK_MARKER": str(self.marker),
                "RELEASE_MARKER": str(self.release_marker),
                "GATE_AFTER_VENV": "1" if self.gate_after_venv else "0",
                "FAIL_PRUNE": "1" if self.fail_prune else "0",
                "FAKE_WRONG_COMMAND": "1" if self.wrong_registration else "0",
                "DOUBLE_SIGNAL_ACTION": self.double_signal_action,
                "ROLLBACK_MARKER": str(self.rollback_marker),
                "ROLLBACK_RELEASE": str(self.rollback_release),
                "PATH": os.pathsep.join(
                    (str(self.fake_bin), str(Path(sys.executable).parent), "/usr/bin", "/bin")
                ),
            }
        )
        return environment

    def _run(self, script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(ROOT / "adapters" / "codex" / script)],
            cwd=ROOT,
            env=self._environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )

    def _enable_fast_successful_install(self) -> None:
        self._write_executable(self.python, self._fast_python_source())
        self._write_executable(self.codex, self._fake_codex_source())

    @staticmethod
    def _fast_python_source() -> str:
        return '''#!/usr/bin/env bash
if [[ "${1:-}" == *configure.py ]] && [ "${2:-}" = "rollback" ] \
    && [ -n "${DOUBLE_SIGNAL_ACTION:-}" ]; then
    : > "$ROLLBACK_MARKER"
    while [ ! -e "$ROLLBACK_RELEASE" ]; do sleep 0.05; done
    exec "$REAL_PYTHON" "$@"
fi
if [[ "${1:-}" == *configure.py ]] \
    && [ "${2:-}" = "${DOUBLE_SIGNAL_ACTION:-none}" ]; then
    "$REAL_PYTHON" "$@"
    status=$?
    [ "$status" -ne 0 ] || kill -TERM "$PPID"
    exit "$status"
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
    generation="$3"
    mkdir -p "$generation/bin"
    ln -s "$0" "$generation/bin/python"
    printf '#!/usr/bin/env bash\nexit 0\n' > "$generation/bin/deepseek-mcp"
    chmod 700 "$generation/bin/deepseek-mcp"
    if [ "${GATE_AFTER_VENV:-0}" = "1" ]; then
        : > "$BLOCK_MARKER"
        while [ ! -e "$RELEASE_MARKER" ]; do sleep 0.05; done
    fi
    exit 0
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
    exit 0
fi
case "${1:-}" in
    *mcp_smoke.py) exit 0 ;;
    *installer_path_guard.py)
        if [ "${2:-}" = "prune-generations" ] && [ "${FAIL_PRUNE:-0}" = "1" ]; then
            exit 9
        fi
        ;;
esac
if [ "${1:-}" = "-c" ] && [[ "${2:-}" == *Config.validate_runtime_settings* ]]; then
    exit 0
fi
exec "$REAL_PYTHON" "$@"
'''

    @staticmethod
    def _fake_codex_source() -> str:
        return f'''#!{sys.executable}
import json
import os
from pathlib import Path

command = next((Path(os.environ["HOME"]) / ".deepseek-mcp" / "codex-venvs").glob(
    "generation.*/bin/deepseek-mcp"
))
if os.environ.get("FAKE_WRONG_COMMAND") == "1":
    command = Path(str(command) + "-wrong")
print(json.dumps({{
    "name": "deepseek",
    "enabled": True,
    "transport": {{
        "type": "stdio", "command": str(command), "args": [],
        "env": None, "env_vars": {FORWARDED_ENV_VARS!r}, "cwd": None,
    }},
}}))
'''

    def test_concurrent_uninstall_and_post_sigkill_install_fail_closed(self) -> None:
        process = subprocess.Popen(
            ["bash", str(ROOT / "adapters" / "codex" / "install.sh")],
            cwd=ROOT,
            env=self._environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not self.marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.marker.exists(), "installer never reached the leased venv step")
            lease = self.home / ".deepseek-mcp" / "codex-adapter.lock"
            self.assertTrue(lease.is_dir())

            concurrent = self._run("uninstall.sh")
            self.assertNotEqual(concurrent.returncode, 0, concurrent.stdout)
            self.assertIn("SIGKILL", concurrent.stderr)
            self.assertTrue(lease.is_dir())

            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
            self.assertTrue(lease.is_dir())

            retry = self._run("install.sh")
            self.assertNotEqual(retry.returncode, 0, retry.stdout)
            self.assertIn("SIGKILL", retry.stderr)
            self.assertTrue(lease.is_dir())
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)

    def test_prune_failure_is_a_warning_after_verified_install(self) -> None:
        self._enable_fast_successful_install()
        self.fail_prune = True

        result = self._run("install.sh")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("current install is usable", result.stderr)
        self.assertIn("Codex support installed", result.stdout)
        lease = self.home / ".deepseek-mcp" / "codex-adapter.lock"
        self.assertFalse(lease.exists())
        generations = self.home / ".deepseek-mcp" / "codex-venvs"
        self.assertEqual(len(list(generations.glob("generation.*"))), 1)

    def test_concurrent_installs_leave_one_existing_registered_generation(self) -> None:
        self._enable_fast_successful_install()
        self.gate_after_venv = True
        first = subprocess.Popen(
            ["bash", str(ROOT / "adapters" / "codex" / "install.sh")],
            cwd=ROOT,
            env=self._environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            self._wait_for_marker()
            second = self._run("install.sh")
            self.assertNotEqual(second.returncode, 0, second.stdout)
            self.assertIn("SIGKILL", second.stderr)
            self.release_marker.touch()
            stdout, stderr = first.communicate(timeout=10)
            self.assertEqual(first.returncode, 0, stdout + stderr)

            config = tomlkit.parse((self.home / ".codex" / "config.toml").read_text())
            command = Path(config["mcp_servers"]["deepseek"]["command"])
            self.assertTrue(command.is_file())
            generations = self.home / ".deepseek-mcp" / "codex-venvs"
            self.assertEqual(len(list(generations.glob("generation.*"))), 1)
        finally:
            self.release_marker.touch()
            if first.poll() is None:
                os.killpg(first.pid, signal.SIGKILL)
                first.communicate(timeout=5)

    def test_wrong_registered_command_rolls_back_config_and_generation(self) -> None:
        self._enable_fast_successful_install()
        self.wrong_registration = True

        result = self._run("install.sh")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("unexpected MCP command", result.stderr)
        self.assertIn("Rolling back Codex configuration", result.stderr)
        self.assertFalse((self.home / ".codex" / "config.toml").exists())
        generations = self.home / ".deepseek-mcp" / "codex-venvs"
        self.assertEqual(list(generations.glob("generation.*")), [])
        self.assertFalse((self.home / ".deepseek-mcp" / "codex-adapter.lock").exists())

    def test_install_rollback_ignores_second_term_and_releases_lease(self) -> None:
        self._enable_fast_successful_install()
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        original = b'[user]\nvalue = "preserve-install"\n'
        config.write_bytes(original)
        self.double_signal_action = "install"

        result = self._run_double_signal("install.sh", signal.SIGTERM)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(config.read_bytes(), original)
        self.assertFalse((self.home / ".deepseek-mcp" / "codex-adapter.lock").exists())

    def test_uninstall_rollback_ignores_second_int_and_releases_lease(self) -> None:
        self._enable_fast_successful_install()
        installed = self._run("install.sh")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        config = self.home / ".codex" / "config.toml"
        original = config.read_bytes()
        self.double_signal_action = "uninstall"

        result = self._run_double_signal("uninstall.sh", signal.SIGINT)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(config.read_bytes(), original)
        self.assertFalse((self.home / ".deepseek-mcp" / "codex-adapter.lock").exists())

    def _run_double_signal(
        self, script: str, second_signal: signal.Signals
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            ["bash", str(ROOT / "adapters" / "codex" / script)],
            cwd=ROOT,
            env=self._environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            self._wait_for_path(self.rollback_marker, "rollback helper never started")
            os.kill(process.pid, second_signal)
            time.sleep(0.1)
            self.assertIsNone(process.poll(), "second signal interrupted rollback")
            self.rollback_release.touch()
            stdout, stderr = process.communicate(timeout=10)
            return subprocess.CompletedProcess(
                process.args, process.returncode, stdout, stderr
            )
        finally:
            self.rollback_release.touch()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)

    def _wait_for_marker(self) -> None:
        self._wait_for_path(self.marker, "installer never reached the leased venv step")

    @staticmethod
    def _wait_for_path(path: Path, failure: str) -> None:
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not path.exists():
            raise AssertionError(failure)


if __name__ == "__main__":
    unittest.main()
