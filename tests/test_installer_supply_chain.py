from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILE_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "NotebookEdit"]


FAKE_PYTHON = r"""#!/usr/bin/env bash
set -eu
if [ "${1:-}" = "--version" ]; then
    echo "Python 3.12.0"
    exit 0
fi
case "${1:-}" in
    *installer_path_guard.py)
        if [ "${2:-}" = "prune-generations" ] \
            && [ -n "${FAKE_PRUNE_GATE:-}" ]; then
            : > "${FAKE_PRUNE_GATE}.entered"
            while [ ! -f "$FAKE_PRUNE_GATE" ]; do sleep 0.02; done
        fi
        exec "$FAKE_REAL_PYTHON" "$@"
        ;;
    *mcp_smoke.py)
        exit 0
        ;;
esac
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
    generation="$3"
    mkdir -p "$generation/bin"
    cp "$0" "$generation/bin/python"
    chmod 700 "$generation/bin/python"
    printf '#!/bin/sh\nexit 0\n' > "$generation/bin/deepseek-mcp"
    chmod 700 "$generation/bin/deepseek-mcp"
    exit 0
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
    exit 0
fi
if [ "${1:-}" = "-c" ]; then
    case "${2:-}" in
        *hashlib*)
            exec "$FAKE_REAL_PYTHON" "$@"
            ;;
        *Config.validate_runtime_settings*)
            if [ "${FAKE_CONFIG_FAILURE:-0}" = "1" ]; then
                echo "invalid runtime configuration"
                exit 1
            fi
            ;;
    esac
    exit 0
fi
exit 1
"""


FAKE_CLAUDE = r"""#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$FAKE_CLAUDE_LOG"
if [ "${1:-}" = "mcp" ] && [ "${2:-}" = "get" ]; then
    get_count=0
    if [ -f "${FAKE_CLAUDE_GET_COUNT}" ]; then
        get_count="$(cat "$FAKE_CLAUDE_GET_COUNT")"
    fi
    get_count=$((get_count + 1))
    printf '%s' "$get_count" > "$FAKE_CLAUDE_GET_COUNT"
    if [ -n "${FAKE_SWAP_ON_GET:-}" ] \
        && [ "$get_count" = "$FAKE_SWAP_ON_GET" ]; then
        printf '%s' "$FAKE_SWAP_COMMAND" > "$FAKE_CLAUDE_STATE"
    fi
    [ -f "$FAKE_CLAUDE_STATE" ] || exit 1
    command_path="$(cat "$FAKE_CLAUDE_STATE")"
    printf 'deepseek:\n  Scope: User config\n  Type: stdio\n  Command: %s\n  Args:\n  Environment:\n' "$command_path"
    exit 0
fi
if [ "${1:-}" = "mcp" ] && [ "${2:-}" = "list" ]; then
    if [ -f "$FAKE_CLAUDE_STATE" ]; then
        printf 'deepseek: configured\n'
    else
        printf 'No MCP servers configured\n'
    fi
    exit 0
fi
if [ "${1:-}" = "mcp" ] && [ "${2:-}" = "remove" ]; then
    if [ "${FAKE_FAIL_REMOVE:-0}" = "1" ]; then
        exit 1
    fi
    rm -f "$FAKE_CLAUDE_STATE"
    if [ -n "${FAKE_SIGNAL_AFTER_REMOVE:-}" ]; then
        kill -s "$FAKE_SIGNAL_AFTER_REMOVE" "$PPID"
    fi
    exit 0
fi
if [ "${1:-}" = "mcp" ] && [ "${2:-}" = "add" ]; then
    while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
    [ "$#" -gt 1 ] || exit 2
    shift
    command_path="$1"
    if [ "${FAKE_FAIL_NEW_ADD:-0}" = "1" ] \
        && [ "$command_path" != "$FAKE_OLD_COMMAND" ]; then
        exit 1
    fi
    printf '%s' "$command_path" > "$FAKE_CLAUDE_STATE"
    exit 0
fi
exit 2
"""


FAKE_MV = r"""#!/usr/bin/env bash
set -eu
"$FAKE_REAL_MV" "$@"
if [ "${FAKE_SIGNAL_AFTER_MV:-0}" = "1" ] \
    && [ ! -f "$FAKE_MV_GATE" ]; then
    : > "$FAKE_MV_GATE"
    kill -TERM "$PPID"
fi
"""


FAKE_CP = r"""#!/usr/bin/env bash
set -eu
if [ "${FAKE_FAIL_HELPER_CP:-0}" = "1" ]; then
    case "$*" in *commands/ds.md*) exit 1 ;; esac
fi
exec "$FAKE_REAL_CP" "$@"
"""


def _requirement_blocks(path: Path) -> list[str]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)
    return ["\n".join(block) for block in blocks]


def _fresh_allowed_tools(path: Path) -> list[str]:
    script = path.read_text(encoding="utf-8")
    match = re.search(r'(?m)^  "allowed_tools": (\[[^\n]+\])$', script)
    if match is None:
        raise AssertionError(f"fresh config template missing from {path}")
    return json.loads(match.group(1))


def _write_executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    path.chmod(0o700)


def _installer_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    project = root / "project"
    home = root / "home"
    fake_bin = root / "bin"
    (project / "scripts").mkdir(parents=True)
    (project / "src" / "deepseek_mcp").mkdir(parents=True)
    (project / "adapters" / "codex").mkdir(parents=True)
    (project / "skills" / "delegate-to-deepseek").mkdir(parents=True)
    (project / "commands").mkdir(parents=True)
    home.mkdir(mode=0o700)
    shutil.copy2(ROOT / "install.sh", project / "install.sh")
    shutil.copy2(ROOT / "uninstall.sh", project / "uninstall.sh")
    shutil.copy2(
        ROOT / "scripts" / "installer_path_guard.py",
        project / "scripts" / "installer_path_guard.py",
    )
    shutil.copy2(
        ROOT / "scripts" / "installer_asset_guard.py",
        project / "scripts" / "installer_asset_guard.py",
    )
    shutil.copy2(
        ROOT / "scripts" / "claude_helpers.sh",
        project / "scripts" / "claude_helpers.sh",
    )
    for name in ("__init__.py", "windows_acl.py", "windows_file_io.py"):
        shutil.copy2(
            ROOT / "src" / "deepseek_mcp" / name,
            project / "src" / "deepseek_mcp" / name,
        )
    (project / "requirements.lock").write_text("locked\n", encoding="utf-8")
    (project / "adapters" / "codex" / "mcp_smoke.py").write_text("", encoding="utf-8")
    shutil.copy2(
        ROOT / "skills" / "delegate-to-deepseek" / "SKILL.md",
        project / "skills" / "delegate-to-deepseek" / "SKILL.md",
    )
    shutil.copy2(ROOT / "commands" / "ds.md", project / "commands" / "ds.md")
    _write_executable(fake_bin / "python3.12", FAKE_PYTHON)
    _write_executable(fake_bin / "claude", FAKE_CLAUDE)
    _write_executable(fake_bin / "mv", FAKE_MV)
    _write_executable(fake_bin / "cp", FAKE_CP)
    return project, home, fake_bin, project / ".venv" / "bin" / "deepseek-mcp"


def _installer_environment(
    project: Path,
    home: Path,
    fake_bin: Path,
    old_command: Path,
    **overrides: str,
) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(home),
            "PATH": os.pathsep.join((str(fake_bin), environment.get("PATH", ""))),
            "SHELL": "/bin/false",
            "DEEPSEEK_CLAUDE_BIN": str(fake_bin / "claude"),
            "FAKE_REAL_PYTHON": sys.executable,
            "FAKE_CLAUDE_LOG": str(home / "claude.log"),
            "FAKE_CLAUDE_STATE": str(home / "claude.state"),
            "FAKE_CLAUDE_GET_COUNT": str(home / "claude.get-count"),
            "FAKE_OLD_COMMAND": str(old_command),
            "FAKE_REAL_MV": shutil.which("mv") or "/bin/mv",
            "FAKE_REAL_CP": shutil.which("cp") or "/bin/cp",
            "FAKE_MV_GATE": str(home / "mv.signal-sent"),
            **overrides,
        }
    )
    environment.pop("DEEPSEEK_API_KEY", None)
    return environment


def _run_installer(
    project: Path,
    home: Path,
    fake_bin: Path,
    old_command: Path,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(project / "install.sh")],
        cwd=project,
        env=_installer_environment(project, home, fake_bin, old_command, **overrides),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def _run_uninstaller(
    project: Path,
    home: Path,
    fake_bin: Path,
    old_command: Path,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(project / "uninstall.sh")],
        cwd=project,
        env=_installer_environment(project, home, fake_bin, old_command, **overrides),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def _seed_legacy_install(home: Path, old_command: Path, tools: list[str]) -> None:
    _write_executable(old_command, "#!/bin/sh\nprintf 'legacy-runtime'\n")
    (home / "claude.state").write_text(str(old_command), encoding="utf-8")
    config_dir = home / ".deepseek-mcp"
    config_dir.mkdir(mode=0o700)
    config = config_dir / "config.json"
    config.write_text(
        json.dumps({"api_key": "sk-old", "allowed_tools": tools}),
        encoding="utf-8",
    )
    config.chmod(0o600)


class InstallerSupplyChainTests(unittest.TestCase):
    def test_fresh_installers_enable_file_writes_without_bash(self) -> None:
        for name in ("install.sh", "adapters/codex/install.sh"):
            tools = _fresh_allowed_tools(ROOT / name)
            self.assertEqual(tools, DEFAULT_FILE_TOOLS, name)
            self.assertNotIn("Bash", tools, name)

    def test_every_locked_requirement_is_exact_and_hashed(self) -> None:
        for name in ("requirements.lock", "requirements-audit.lock"):
            blocks = _requirement_blocks(ROOT / name)
            self.assertTrue(blocks, name)
            for block in blocks:
                first = block.splitlines()[0]
                self.assertIn("==", first, first)
                self.assertIn("--hash=sha256:", block, first)

    def test_runtime_lock_contains_portable_google_re2_artifacts(self) -> None:
        lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        match = re.search(r"(?ms)^google-re2==1\.1\.20251105.*?(?=^[a-z0-9])", lock)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(match.group(0).count("--hash=sha256:"), 12)

    def test_installers_use_only_the_runtime_hash_lock(self) -> None:
        forbidden = ("astral.sh", "upgrade pip", "irm ", "| iex")
        for name in ("install.sh", "adapters/codex/install.sh"):
            script = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("requirements.lock", script)
            self.assertIn("--require-hashes", script)
            self.assertIn("--only-binary=:all:", script)
            self.assertNotIn("requirements-audit.lock", script)
            for fragment in forbidden:
                self.assertNotIn(fragment, script)

    def test_claude_installer_switches_only_after_generation_validation(self) -> None:
        script = (ROOT / "install.sh").read_text(encoding="utf-8")
        install = script.index('"$PYBIN" -m pip install')
        config_validation = script.index("Config.validate_runtime_settings", install)
        smoke = script.index("mcp_smoke.py", config_validation)
        registration = script.index('switch_registration "$REGISTERED_COMMAND"', smoke)
        success = script.index("INSTALL_SUCCEEDED=1", registration)
        helpers = script.index("deploy_claude_helpers", registration)
        pruning = script.index("prune-generations", success)

        self.assertIn('VENV_ROOT="$CONFIG_DIR/claude-venvs"', script)
        self.assertLess(install, config_validation)
        self.assertLess(config_validation, smoke)
        self.assertLess(smoke, registration)
        self.assertLess(registration, success)
        self.assertLess(success, helpers)
        self.assertLess(helpers, pruning)
        self.assertLess(success, pruning)
        self.assertIn("delete-generation", script)

    @unittest.skipIf(os.name == "nt", "Git Bash paths differ from native Python paths")
    def test_legacy_bash_config_upgrades_with_bash_effectively_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            old_tools = [
                "Read", "Write", "Edit", "Bash", "Glob", "Grep", "NotebookEdit"
            ]
            _seed_legacy_install(home, old_command, old_tools)

            result = _run_installer(project, home, fake_bin, old_command)

            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads(
                (home / ".deepseek-mcp" / "config.json").read_text(encoding="utf-8")
            )
            self.assertIn("Bash", config["allowed_tools"])
            check = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import json,sys; from deepseek_mcp.config import "
                    "_load_allowed_tools; print(json.dumps(_load_allowed_tools("
                    "json.load(open(sys.argv[1])))))",
                    str(home / ".deepseek-mcp" / "config.json"),
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual(json.loads(check.stdout), DEFAULT_FILE_TOOLS)

    @unittest.skipIf(os.name == "nt", "Git Bash paths differ from native Python paths")
    def test_config_failure_preserves_runtime_and_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)

            result = _run_installer(
                project, home, fake_bin, old_command, FAKE_CONFIG_FAILURE="1"
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual((home / "claude.state").read_text(), str(old_command))
            self.assertTrue(old_command.is_file())
            calls = (home / "claude.log").read_text().splitlines()
            self.assertEqual(calls, ["mcp get deepseek"])
            generations = home / ".deepseek-mcp" / "claude-venvs"
            self.assertEqual(list(generations.glob("generation.*")), [])
            self.assertFalse(
                (home / ".claude" / "skills" / "delegate-to-deepseek").exists()
            )
            self.assertFalse((home / ".claude" / "commands" / "ds.md").exists())
            self.assertFalse((home / ".zshrc").exists())
            self.assertIn("invalid runtime configuration", result.stderr)

    @unittest.skipIf(os.name == "nt", "Git Bash paths differ from native Python paths")
    def test_registration_add_failure_restores_legacy_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)

            result = _run_installer(
                project, home, fake_bin, old_command, FAKE_FAIL_NEW_ADD="1"
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual((home / "claude.state").read_text(), str(old_command))
            self.assertTrue(old_command.is_file())
            calls = (home / "claude.log").read_text().splitlines()
            self.assertEqual(calls[0], "mcp get deepseek")
            self.assertEqual(calls.count("mcp remove deepseek -s user"), 1)
            self.assertTrue(any(str(old_command) in call for call in calls[1:]))
            generations = home / ".deepseek-mcp" / "claude-venvs"
            self.assertEqual(list(generations.glob("generation.*")), [])

    @unittest.skipIf(os.name == "nt", "Git Bash paths differ from native Python paths")
    def test_successful_upgrade_switches_to_verified_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)

            result = _run_installer(project, home, fake_bin, old_command)

            self.assertEqual(result.returncode, 0, result.stderr)
            registered = Path((home / "claude.state").read_text())
            self.assertNotEqual(registered, old_command)
            self.assertTrue(registered.is_file())
            generations = home / ".deepseek-mcp" / "claude-venvs"
            self.assertEqual(len(list(generations.glob("generation.*"))), 1)
            calls = (home / "claude.log").read_text().splitlines()
            self.assertGreaterEqual(calls.count("mcp get deepseek"), 4)

    @unittest.skipIf(os.name == "nt", "Git Bash paths differ from native Python paths")
    def test_registration_change_before_switch_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)
            foreign = home / "foreign" / "deepseek-mcp"

            result = _run_installer(
                project,
                home,
                fake_bin,
                old_command,
                FAKE_SWAP_ON_GET="2",
                FAKE_SWAP_COMMAND=str(foreign),
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual((home / "claude.state").read_text(), str(foreign))
            calls = (home / "claude.log").read_text().splitlines()
            self.assertNotIn("mcp remove deepseek -s user", calls)
            self.assertFalse(any(call.startswith("mcp add ") for call in calls))
            generations = home / ".deepseek-mcp" / "claude-venvs"
            self.assertEqual(len(list(generations.glob("generation.*"))), 1)
            self.assertIn("保留候选运行时", result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX signal semantics")
    def test_term_after_remove_restores_registration_and_cleans_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)

            result = _run_installer(
                project,
                home,
                fake_bin,
                old_command,
                FAKE_SIGNAL_AFTER_REMOVE="TERM",
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual((home / "claude.state").read_text(), str(old_command))
            self.assertFalse((home / ".deepseek-mcp" / "claude-install.lock").exists())
            generations = home / ".deepseek-mcp" / "claude-venvs"
            self.assertEqual(list(generations.glob("generation.*")), [])
            calls = (home / "claude.log").read_text().splitlines()
            self.assertEqual(calls.count("mcp remove deepseek -s user"), 1)
            self.assertTrue(any(call.endswith(f"-- {old_command}") for call in calls))

    @unittest.skipIf(os.name == "nt", "POSIX signal semantics")
    def test_term_after_helper_move_restores_the_previous_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)
            skill = home / ".claude" / "skills" / "delegate-to-deepseek"
            command = home / ".claude" / "commands" / "ds.md"
            skill.parent.mkdir(parents=True)
            command.parent.mkdir(parents=True)
            skill.symlink_to(
                project / "skills" / "delegate-to-deepseek",
                target_is_directory=True,
            )
            command.symlink_to(project / "commands" / "ds.md")

            result = _run_installer(
                project, home, fake_bin, old_command, FAKE_SIGNAL_AFTER_MV="1"
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertTrue(skill.is_symlink())
            self.assertEqual(skill.resolve(), (project / "skills" / "delegate-to-deepseek").resolve())
            self.assertTrue(command.is_symlink())
            self.assertEqual(
                list((home / ".claude" / "skills").glob("*.deepseek-mcp.*")), []
            )

    @unittest.skipIf(os.name == "nt", "Git Bash paths differ from native Python paths")
    def test_partial_helper_generation_is_cleaned_on_copy_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)

            result = _run_installer(
                project, home, fake_bin, old_command, FAKE_FAIL_HELPER_CP="1"
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            helper_root = home / ".deepseek-mcp" / "claude-helpers"
            self.assertEqual(list(helper_root.glob("generation.*")), [])
            self.assertIn("无法安全创建 Claude helper generation", result.stderr)

    @unittest.skipIf(os.name == "nt", "Git Bash paths differ from native Python paths")
    def test_installer_does_not_modify_shell_startup_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)
            shell_rc = home / ".zshrc"
            shell_rc.write_text("# user settings\n", encoding="utf-8")

            result = _run_installer(project, home, fake_bin, old_command)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(shell_rc.read_text(encoding="utf-8"), "# user settings\n")
            self.assertIn("DEEPSEEK_MODE=off claude", result.stdout)

    @unittest.skipIf(os.name == "nt", "Git Bash paths differ from native Python paths")
    def test_install_lease_remains_held_through_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)
            gate = Path(tmpdir) / "release-prune"
            environment = _installer_environment(
                project,
                home,
                fake_bin,
                old_command,
                FAKE_PRUNE_GATE=str(gate),
            )
            first = subprocess.Popen(
                ["bash", str(project / "install.sh")],
                cwd=project,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 10
                while not Path(f"{gate}.entered").exists():
                    if first.poll() is not None:
                        stdout, stderr = first.communicate()
                        self.fail(f"first installer exited early:\n{stdout}\n{stderr}")
                    if time.monotonic() >= deadline:
                        self.fail("first installer did not reach prune gate")
                    time.sleep(0.02)

                active = Path((home / "claude.state").read_text())
                self.assertTrue(active.is_file())
                calls_before = (home / "claude.log").read_text().splitlines()
                second = _run_installer(project, home, fake_bin, old_command)
                self.assertNotEqual(second.returncode, 0, second.stdout)
                self.assertIn("安装/卸载事务", second.stderr)
                self.assertEqual(Path((home / "claude.state").read_text()), active)
                self.assertTrue(active.is_file())
                self.assertEqual(
                    (home / "claude.log").read_text().splitlines(), calls_before
                )
            finally:
                gate.write_text("release\n", encoding="utf-8")
                stdout, stderr = first.communicate(timeout=20)
            self.assertEqual(first.returncode, 0, f"{stdout}\n{stderr}")
            self.assertFalse((home / ".deepseek-mcp" / "claude-install.lock").exists())

    @unittest.skipIf(os.name == "nt", "Git Bash paths differ from native Python paths")
    def test_malformed_generation_registration_is_foreign(self) -> None:
        bad_suffixes = (
            "generation.good/../generation.evil/bin/deepseek-mcp",
            "generation.good/extra/bin/deepseek-mcp",
            "generation.-bad/bin/deepseek-mcp",
            "generation.good/bin/../bin/deepseek-mcp",
        )
        for suffix in bad_suffixes:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmpdir:
                project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
                _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)
                malicious = home / ".deepseek-mcp" / "claude-venvs" / suffix
                (home / "claude.state").write_text(str(malicious), encoding="utf-8")

                result = _run_installer(project, home, fake_bin, old_command)

                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual((home / "claude.state").read_text(), str(malicious))
                self.assertEqual(
                    (home / "claude.log").read_text().splitlines(),
                    ["mcp get deepseek"],
                )

    @unittest.skipIf(os.name == "nt", "Git Bash paths differ from native Python paths")
    def test_published_legacy_copy_is_upgraded_but_modified_copy_is_rejected(self) -> None:
        old_skill = subprocess.check_output(
            ["git", "show", "HEAD:skills/delegate-to-deepseek/SKILL.md"],
            cwd=ROOT,
            text=True,
        )
        for modified in (False, True):
            with self.subTest(modified=modified), tempfile.TemporaryDirectory() as tmpdir:
                project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
                _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)
                skill = home / ".claude" / "skills" / "delegate-to-deepseek"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(
                    old_skill + ("\n# user change\n" if modified else ""),
                    encoding="utf-8",
                )

                result = _run_installer(project, home, fake_bin, old_command)

                if modified:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(skill.is_symlink())
                    self.assertIn("# user change", (skill / "SKILL.md").read_text())
                    self.assertNotEqual(
                        (home / "claude.state").read_text(), str(old_command)
                    )
                    self.assertIn("核心 MCP 已安装", result.stdout)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(skill.is_symlink())
                    helper_root = home / ".deepseek-mcp" / "claude-helpers"
                    self.assertEqual(
                        skill.resolve().parent.parent, helper_root.resolve()
                    )
                    self.assertRegex(skill.resolve().parent.name, r"^generation\.")
                    self.assertEqual(
                        (skill / "SKILL.md").read_text(encoding="utf-8"),
                        (project / "skills" / "delegate-to-deepseek" / "SKILL.md")
                        .read_text(encoding="utf-8"),
                    )

    @unittest.skipIf(os.name == "nt", "Git Bash paths differ from native Python paths")
    def test_uninstall_without_claude_cli_preserves_registration_and_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)
            skill = home / ".claude" / "skills" / "delegate-to-deepseek"
            command = home / ".claude" / "commands" / "ds.md"
            skill.parent.mkdir(parents=True)
            command.parent.mkdir(parents=True)
            skill.symlink_to(
                project / "skills" / "delegate-to-deepseek",
                target_is_directory=True,
            )
            command.symlink_to(project / "commands" / "ds.md")

            result = _run_uninstaller(
                project,
                home,
                fake_bin,
                old_command,
                DEEPSEEK_CLAUDE_BIN=str(fake_bin / "missing-claude"),
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual((home / "claude.state").read_text(), str(old_command))
            self.assertTrue(skill.is_symlink())
            self.assertTrue(command.is_symlink())
            self.assertIn("未做任何删除", result.stderr)

    def test_key_material_is_outside_xtrace_window(self) -> None:
        script = (ROOT / "install.sh").read_text(encoding="utf-8")
        disable = script.index("set +x")
        read_key = script.index("read -rs", disable)
        clear_raw = script.index('API_KEY=""', read_key)
        clear_escaped = script.index('escaped_value=""', clear_raw)
        restore = script.index("set -x", clear_escaped)
        self.assertLess(disable, read_key)
        self.assertLess(read_key, clear_raw)
        self.assertLess(clear_raw, clear_escaped)
        self.assertLess(clear_escaped, restore)

    @unittest.skipIf(os.name == "nt", "Git Bash paths differ from native Python paths")
    def test_foreign_uninstall_registration_and_assets_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)
            skill = home / ".claude" / "skills" / "delegate-to-deepseek"
            command = home / ".claude" / "commands" / "ds.md"
            skill.parent.mkdir(parents=True)
            command.parent.mkdir(parents=True)
            skill.symlink_to(project / "skills" / "delegate-to-deepseek", target_is_directory=True)
            command.symlink_to(project / "commands" / "ds.md")
            foreign = home / "foreign" / "deepseek-mcp"
            (home / "claude.state").write_text(str(foreign), encoding="utf-8")

            result = _run_uninstaller(project, home, fake_bin, old_command)

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual((home / "claude.state").read_text(), str(foreign))
            self.assertTrue(skill.is_symlink())
            self.assertTrue(command.is_symlink())
            self.assertEqual(
                (home / "claude.log").read_text().splitlines(), ["mcp get deepseek"]
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)
            skill = home / ".claude" / "skills" / "delegate-to-deepseek"
            command = home / ".claude" / "commands" / "ds.md"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("user-owned\n", encoding="utf-8")
            command.parent.mkdir(parents=True)
            command.symlink_to(project / "commands" / "ds.md")

            result = _run_uninstaller(project, home, fake_bin, old_command)

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual((home / "claude.state").read_text(), str(old_command))
            self.assertTrue((skill / "SKILL.md").is_file())
            self.assertTrue(command.is_symlink())
            calls = (home / "claude.log").read_text().splitlines()
            self.assertNotIn("mcp remove deepseek -s user", calls)

    @unittest.skipIf(os.name == "nt", "POSIX FIFO semantics")
    def test_foreign_fifo_helper_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)
            skill = home / ".claude" / "skills" / "delegate-to-deepseek"
            skill.mkdir(parents=True)
            os.mkfifo(skill / "SKILL.md", 0o600)

            started = time.monotonic()
            result = _run_uninstaller(project, home, fake_bin, old_command)

            self.assertLess(time.monotonic() - started, 5)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertTrue((skill / "SKILL.md").exists())
            self.assertEqual((home / "claude.state").read_text(), str(old_command))

    @unittest.skipIf(os.name == "nt", "Git Bash paths differ from native Python paths")
    def test_owned_uninstall_removes_registration_and_deployments_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)
            skill = home / ".claude" / "skills" / "delegate-to-deepseek"
            command = home / ".claude" / "commands" / "ds.md"
            skill.parent.mkdir(parents=True)
            command.parent.mkdir(parents=True)
            skill.symlink_to(project / "skills" / "delegate-to-deepseek", target_is_directory=True)
            command.symlink_to(project / "commands" / "ds.md")

            result = _run_uninstaller(project, home, fake_bin, old_command)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((home / "claude.state").exists())
            self.assertFalse(skill.exists())
            self.assertFalse(command.exists())
            self.assertTrue(old_command.is_file())
            self.assertTrue((home / ".deepseek-mcp" / "config.json").is_file())

    @unittest.skipIf(os.name == "nt", "Git Bash paths differ from native Python paths")
    def test_uninstall_rechecks_registration_before_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)
            foreign = home / "foreign" / "deepseek-mcp"
            skill = home / ".claude" / "skills" / "delegate-to-deepseek"
            command = home / ".claude" / "commands" / "ds.md"
            skill.parent.mkdir(parents=True)
            command.parent.mkdir(parents=True)
            skill.symlink_to(
                project / "skills" / "delegate-to-deepseek",
                target_is_directory=True,
            )
            command.symlink_to(project / "commands" / "ds.md")

            result = _run_uninstaller(
                project,
                home,
                fake_bin,
                old_command,
                FAKE_SWAP_ON_GET="2",
                FAKE_SWAP_COMMAND=str(foreign),
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual((home / "claude.state").read_text(), str(foreign))
            self.assertTrue(skill.is_symlink())
            self.assertTrue(command.is_symlink())
            calls = (home / "claude.log").read_text().splitlines()
            self.assertNotIn("mcp remove deepseek -s user", calls)

    @unittest.skipIf(os.name == "nt", "Git Bash paths differ from native Python paths")
    def test_uninstall_remove_failure_restores_staged_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)
            skill = home / ".claude" / "skills" / "delegate-to-deepseek"
            command = home / ".claude" / "commands" / "ds.md"
            skill.parent.mkdir(parents=True)
            command.parent.mkdir(parents=True)
            skill.symlink_to(
                project / "skills" / "delegate-to-deepseek",
                target_is_directory=True,
            )
            command.symlink_to(project / "commands" / "ds.md")

            result = _run_uninstaller(
                project, home, fake_bin, old_command, FAKE_FAIL_REMOVE="1"
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual((home / "claude.state").read_text(), str(old_command))
            self.assertTrue(skill.is_symlink())
            self.assertTrue(command.is_symlink())

    @unittest.skipIf(os.name == "nt", "POSIX signal semantics")
    def test_uninstall_term_after_helper_move_restores_all_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project, home, fake_bin, old_command = _installer_fixture(Path(tmpdir))
            _seed_legacy_install(home, old_command, DEFAULT_FILE_TOOLS)
            skill = home / ".claude" / "skills" / "delegate-to-deepseek"
            command = home / ".claude" / "commands" / "ds.md"
            skill.parent.mkdir(parents=True)
            command.parent.mkdir(parents=True)
            skill.symlink_to(
                project / "skills" / "delegate-to-deepseek",
                target_is_directory=True,
            )
            command.symlink_to(project / "commands" / "ds.md")

            result = _run_uninstaller(
                project, home, fake_bin, old_command, FAKE_SIGNAL_AFTER_MV="1"
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual((home / "claude.state").read_text(), str(old_command))
            self.assertTrue(skill.is_symlink())
            self.assertTrue(command.is_symlink())
            self.assertEqual(
                list((home / ".claude" / "skills").glob("*.deepseek-mcp.*")), []
            )

    def test_remote_pipe_compatibility_script_is_inert(self) -> None:
        script = (ROOT / "curl-install.sh").read_text(encoding="utf-8")
        executable_tail = script.split("\nEOF\n", 1)[1]
        self.assertNotIn("git clone", executable_tail)
        self.assertNotIn("git pull", executable_tail)
        self.assertNotIn("exec ./install.sh", executable_tail)
        self.assertRegex(script, r"(?m)^exit 1$")

    def test_ci_uses_locked_runtime_and_auditor(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn(
            "--only-binary=:all: --require-hashes -r requirements.lock", workflow
        )
        self.assertIn(
            "--only-binary=:all: --require-hashes -r requirements-audit.lock",
            workflow,
        )
        self.assertIn("python -m pip_audit --local", workflow)

    def test_container_build_pins_base_and_runtime_dependencies(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(
            dockerfile,
            r"(?m)^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$",
        )
        self.assertIn(
            "--only-binary=:all: --require-hashes -r requirements.lock", dockerfile
        )
        self.assertIn("--no-deps --no-build-isolation .", dockerfile)
        self.assertNotIn("pip install --no-cache-dir -e .", dockerfile)
        self.assertIn("USER 65532:65532", dockerfile)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", dockerfile)


if __name__ == "__main__":
    unittest.main()
