#!/usr/bin/env bash
# Transactional Codex CLI installer for deepseek-as-subagent.

set -euo pipefail
umask 077

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG_DIR="$HOME/.deepseek-mcp"
CONFIG_FILE="$CONFIG_DIR/config.json"
CODEX_CONFIG_DIR="${CODEX_HOME:-$HOME/.codex}"
CODEX_CONFIG_FILE="$CODEX_CONFIG_DIR/config.toml"
VENV_ROOT="$CONFIG_DIR/codex-venvs"
CONFIG_HELPER="$PROJECT_ROOT/adapters/codex/configure.py"
PATH_GUARD="$PROJECT_ROOT/scripts/installer_path_guard.py"
LOCK_FILE="$PROJECT_ROOT/requirements.lock"
CODEX_BIN="${DEEPSEEK_CODEX_BIN:-codex}"
ADAPTER_LOCK="$CONFIG_DIR/codex-adapter.lock"
FORCE_REPLACE=0

usage() {
    echo "Usage: bash adapters/codex/install.sh [--force-replace]"
}

if [ "${1:-}" = "--force-replace" ]; then
    FORCE_REPLACE=1
    shift
elif [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    usage
    exit 0
fi
if [ "$#" -ne 0 ]; then
    usage >&2
    exit 2
fi

echo "▶ deepseek-as-subagent — Codex installer"
echo "  project: $PROJECT_ROOT"
echo ""

if ! command -v "$CODEX_BIN" >/dev/null 2>&1; then
    echo "✗ codex CLI was not found in PATH"
    echo "  Install/update Codex first, then re-run this script."
    exit 1
fi

PYTHON_CMD=()

supported_python() {
    "$@" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] < (3, 13) else 1)' \
        >/dev/null 2>&1
}

find_python() {
    local candidate version
    for candidate in python3.12 python3.11 python3.10 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && supported_python "$candidate"; then
            PYTHON_CMD=("$candidate")
            return 0
        fi
    done
    if command -v py >/dev/null 2>&1; then
        for version in -3.12 -3.11 -3.10; do
            if supported_python py "$version"; then
                PYTHON_CMD=(py "$version")
                return 0
            fi
        done
    fi
    return 1
}

if ! find_python; then
    echo "✗ A supported Python (3.10–3.12) was not found."
    echo "  Install Python 3.12 from https://www.python.org/downloads/, then retry."
    echo "  This installer does not download or execute remote bootstrap scripts."
    exit 1
fi
if [ ! -r "$LOCK_FILE" ]; then
    echo "✗ Dependency lock is missing: $LOCK_FILE"
    exit 1
fi

case "$(uname -s 2>/dev/null)" in
    MINGW*|CYGWIN*|MSYS*) POSIX_PERMISSIONS=0 ;;
    *) POSIX_PERMISSIONS=1 ;;
esac
"${PYTHON_CMD[@]}" "$PATH_GUARD" prepare-dirs "$CONFIG_DIR"

GENERATION_DIR=""
TRANSACTION_DIR=""
BACKUP_FILE=""
MANIFEST_FILE=""
CONFIG_MUTATED=0
INSTALL_SUCCEEDED=0
PRESERVE_RECOVERY=0
LOCK_HELD=0
PYBIN=""

cleanup_transaction() {
    [ -n "$TRANSACTION_DIR" ] || return 0
    rm -f -- "$BACKUP_FILE" "$MANIFEST_FILE" || true
    rmdir "$TRANSACTION_DIR" 2>/dev/null || true
}

cleanup_generation() {
    [ -n "$GENERATION_DIR" ] || return 0
    if ! "${PYTHON_CMD[@]}" "$PATH_GUARD" delete-generation \
        "$VENV_ROOT" "$GENERATION_DIR"; then
        echo "warning: could not safely clean generation: $GENERATION_DIR" >&2
    fi
}

release_adapter_lock() {
    if [ "$LOCK_HELD" -eq 1 ]; then
        rmdir "$ADAPTER_LOCK" \
            || echo "warning: could not release installer lease: $ADAPTER_LOCK" >&2
        LOCK_HELD=0
    fi
}

on_exit() {
    local exit_code=$?
    trap '' INT TERM HUP
    if [ "$INSTALL_SUCCEEDED" -ne 1 ]; then
        if [ "$CONFIG_MUTATED" -eq 1 ] && [ -f "$MANIFEST_FILE" ]; then
            echo "  Rolling back Codex configuration..." >&2
            if ! "$PYBIN" "$CONFIG_HELPER" rollback --manifest "$MANIFEST_FILE" \
                >/dev/null; then
                PRESERVE_RECOVERY=1
                echo "✗ Automatic rollback failed; backup: $BACKUP_FILE" >&2
                echo "  Preserved MCP environment: $GENERATION_DIR" >&2
            fi
        fi
        if [ "$PRESERVE_RECOVERY" -ne 1 ]; then
            cleanup_generation
        fi
    fi
    if [ "$PRESERVE_RECOVERY" -ne 1 ]; then
        cleanup_transaction
    fi
    release_adapter_lock
    return "$exit_code"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

# A surviving directory means a previous process may have died mid-transaction.
# Never guess that it is stale: the operator must verify no installer is alive.
if ! mkdir "$ADAPTER_LOCK" 2>/dev/null; then
    echo "✗ Another Codex install/uninstall is running, or a prior SIGKILL left:" >&2
    echo "  $ADAPTER_LOCK" >&2
    echo "  Verify no adapter installer is alive before removing that empty directory." >&2
    exit 1
fi
LOCK_HELD=1

"${PYTHON_CMD[@]}" "$PATH_GUARD" prepare-dirs \
    "$VENV_ROOT" "$CODEX_CONFIG_DIR"
"${PYTHON_CMD[@]}" "$PATH_GUARD" secure-files "$CONFIG_FILE"
"${PYTHON_CMD[@]}" "$PATH_GUARD" validate-files "$CODEX_CONFIG_FILE"

GENERATION_DIR="$(mktemp -d "$VENV_ROOT/generation.XXXXXX")"
TRANSACTION_DIR="$(mktemp -d "$CONFIG_DIR/codex-config.XXXXXX")"
if [ "$POSIX_PERMISSIONS" -eq 1 ]; then
    chmod 700 "$GENERATION_DIR" "$TRANSACTION_DIR"
fi
BACKUP_FILE="$TRANSACTION_DIR/config.toml.backup"
MANIFEST_FILE="$TRANSACTION_DIR/transaction.json"

echo "[1/5] Creating an isolated Python generation..."
"${PYTHON_CMD[@]}" -m venv "$GENERATION_DIR"

if [ -d "$GENERATION_DIR/Scripts" ]; then
    VENV_BIN="$GENERATION_DIR/Scripts"
else
    VENV_BIN="$GENERATION_DIR/bin"
fi
PYBIN="$VENV_BIN/python"
[ ! -x "$PYBIN" ] && [ -x "$PYBIN.exe" ] && PYBIN="$PYBIN.exe"
CLI="$VENV_BIN/deepseek-mcp"
[ ! -x "$CLI" ] && [ -x "$CLI.exe" ] && CLI="$CLI.exe"
"${PYTHON_CMD[@]}" "$PATH_GUARD" validate-venv "$GENERATION_DIR" "$PYBIN"

echo "[2/5] Installing deepseek-as-subagent..."
"$PYBIN" -m pip install --quiet --only-binary=:all: --require-hashes -r "$LOCK_FILE"
"$PYBIN" -m pip install --quiet --no-deps --no-build-isolation "$PROJECT_ROOT"
"$PYBIN" -m pip check

if [ ! -e "$CLI" ]; then
    echo "✗ deepseek-mcp entrypoint was not created at $CLI"
    exit 1
fi

echo "[3/5] Running MCP protocol smoke tests..."
"$PYBIN" "$PROJECT_ROOT/adapters/codex/mcp_smoke.py" "$CLI"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "[4/5] Creating DeepSeek config template..."
    "${PYTHON_CMD[@]}" "$PATH_GUARD" write-exclusive "$CONFIG_FILE" <<'EOF'
{
  "api_key": "PASTE_YOUR_DEEPSEEK_KEY_HERE",
  "flash": "deepseek-v4-flash",
  "pro": "deepseek-v4-pro",
  "max_turns": 50,
  "max_run_seconds": 18000,
  "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "NotebookEdit"]
}
EOF
else
    echo "[4/5] DeepSeek config already exists"
fi
"${PYTHON_CMD[@]}" "$PATH_GUARD" secure-files "$CONFIG_FILE"

CONFIG_ERROR=""
if ! CONFIG_ERROR="$("$PYBIN" -c '
import sys
from deepseek_mcp.config import Config
try:
    Config.validate_runtime_settings()
except RuntimeError as error:
    print(error)
    sys.exit(1)
' 2>&1)"; then
    echo "✗ DeepSeek config is incompatible with this release: $CONFIG_FILE" >&2
    echo "  $CONFIG_ERROR" >&2
    echo "  Remove obsolete bash_backend/bash_runtime/bash_image settings before retrying." >&2
    exit 1
fi

echo "[5/5] Updating Codex MCP configuration atomically..."
CONFIG_MUTATED=1
if [ "$FORCE_REPLACE" -eq 1 ]; then
    "$PYBIN" "$CONFIG_HELPER" install \
        --config "$CODEX_CONFIG_FILE" \
        --backup "$BACKUP_FILE" \
        --manifest "$MANIFEST_FILE" \
        --command "$CLI" \
        --force-replace
else
    "$PYBIN" "$CONFIG_HELPER" install \
        --config "$CODEX_CONFIG_FILE" \
        --backup "$BACKUP_FILE" \
        --manifest "$MANIFEST_FILE" \
        --command "$CLI"
fi

verify_registration() {
    "$CODEX_BIN" mcp get deepseek --json | \
        "$PYBIN" -I -c '
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from adapters.codex.configure import (
    ConfigTransactionError,
    validate_registration_payload,
)

try:
    raw = sys.stdin.buffer.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ConfigTransactionError("Codex registration response is too large")
    validate_registration_payload(json.loads(raw), Path(sys.argv[2]), Path(sys.argv[3]))
except (ConfigTransactionError, ValueError) as error:
    print(f"error: {error}", file=sys.stderr)
    raise SystemExit(1)
' "$PROJECT_ROOT" "$CLI" "$VENV_ROOT"
}

if ! verify_registration; then
    echo "✗ Codex registration verification failed"
    exit 1
fi

INSTALL_SUCCEEDED=1
if ! "${PYTHON_CMD[@]}" "$PATH_GUARD" prune-generations \
    "$VENV_ROOT" "$GENERATION_DIR"; then
    echo "warning: old Codex generations were not pruned; current install is usable" >&2
fi
echo ""
echo "✅ Codex support installed"
echo "  MCP environment: $GENERATION_DIR"
echo "  Approval policy: writes (unless an existing custom policy was preserved)"
echo "  Tool timeout: 18060s (5h run + 60s cleanup grace; custom values are preserved)"
echo ""
echo "Verify:"
echo "  codex mcp list"
echo "  codex"
echo "  > call the deepseek ping tool"
echo ""
if grep -q "PASTE_YOUR_DEEPSEEK_KEY_HERE" "$CONFIG_FILE" 2>/dev/null; then
    if [ "$POSIX_PERMISSIONS" -eq 1 ]; then
        echo "Before delegating work, edit $CONFIG_FILE and replace the placeholder."
    else
        echo "Before delegating work, set DEEPSEEK_API_KEY in the environment."
        echo "Do not persist a real API key in config.json on Windows."
    fi
else
    echo "DeepSeek config already contains a key. You can start Codex now."
fi

echo ""
echo "Codex receives delegation guidance from the MCP server by default."
echo "For stricter or project-specific auto-delegation behavior, also use AGENTS.md."
