#!/usr/bin/env bash
# Remove only the Codex registration owned by deepseek-as-subagent.

set -euo pipefail
umask 077

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG_DIR="$HOME/.deepseek-mcp"
VENV_ROOT="$CONFIG_DIR/codex-venvs"
CODEX_CONFIG_DIR="${CODEX_HOME:-$HOME/.codex}"
CODEX_CONFIG_FILE="$CODEX_CONFIG_DIR/config.toml"
CONFIG_HELPER="$PROJECT_ROOT/adapters/codex/configure.py"
PATH_GUARD="$PROJECT_ROOT/scripts/installer_path_guard.py"
CODEX_BIN="${DEEPSEEK_CODEX_BIN:-codex}"
ADAPTER_LOCK="$CONFIG_DIR/codex-adapter.lock"
FORCE_REMOVE=0
PYTHON_CMD=()

if [ "${1:-}" = "--force" ]; then
    FORCE_REMOVE=1
    shift
elif [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    echo "Usage: bash adapters/codex/uninstall.sh [--force]"
    exit 0
fi
if [ "$#" -ne 0 ]; then
    echo "Usage: bash adapters/codex/uninstall.sh [--force]" >&2
    exit 2
fi

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

find_config_python() {
    local candidate generation
    for candidate in \
        "$CONFIG_DIR"/codex-venvs/generation.*/bin/python \
        "$CONFIG_DIR"/codex-venvs/generation.*/Scripts/python.exe \
        "$PROJECT_ROOT"/.venv/bin/python \
        "$PROJECT_ROOT"/.venv/Scripts/python.exe; do
        case "$candidate" in
            "$VENV_ROOT"/generation.*/bin/python)
                generation="${candidate%/bin/python}" ;;
            "$VENV_ROOT"/generation.*/Scripts/python.exe)
                generation="${candidate%/Scripts/python.exe}" ;;
            *) generation="$PROJECT_ROOT/.venv" ;;
        esac
        if [ -x "$candidate" ] \
            && "${PYTHON_CMD[@]}" "$PATH_GUARD" validate-venv \
                "$generation" "$candidate" >/dev/null 2>&1 \
            && "$candidate" -c 'import tomlkit' >/dev/null 2>&1; then
            CONFIG_PYTHON="$candidate"
            return 0
        fi
    done
    return 1
}

if ! command -v "$CODEX_BIN" >/dev/null 2>&1; then
    echo "✗ codex CLI was not found in PATH"
    exit 1
fi
if ! find_python; then
    echo "✗ A trusted supported Python (3.10–3.12) was not found in PATH."
    exit 1
fi
case "$(uname -s 2>/dev/null)" in
    MINGW*|CYGWIN*|MSYS*) POSIX_PERMISSIONS=0 ;;
    *) POSIX_PERMISSIONS=1 ;;
esac
"${PYTHON_CMD[@]}" "$PATH_GUARD" prepare-dirs "$CONFIG_DIR"

TRANSACTION_DIR=""
BACKUP_FILE=""
MANIFEST_FILE=""
CONFIG_PYTHON=""
UNINSTALL_SUCCEEDED=0
PRESERVE_RECOVERY=0
LOCK_HELD=0

cleanup_transaction() {
    [ -n "$TRANSACTION_DIR" ] || return 0
    rm -f -- "$BACKUP_FILE" "$MANIFEST_FILE" || true
    rmdir "$TRANSACTION_DIR" 2>/dev/null || true
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
    if [ "$UNINSTALL_SUCCEEDED" -ne 1 ] && [ -f "$MANIFEST_FILE" ]; then
        echo "  Rolling back Codex configuration..." >&2
        if ! "$CONFIG_PYTHON" "$CONFIG_HELPER" rollback --manifest "$MANIFEST_FILE" \
            >/dev/null; then
            PRESERVE_RECOVERY=1
            echo "✗ Automatic rollback failed; backup: $BACKUP_FILE" >&2
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

if ! mkdir "$ADAPTER_LOCK" 2>/dev/null; then
    echo "✗ Another Codex install/uninstall is running, or a prior SIGKILL left:" >&2
    echo "  $ADAPTER_LOCK" >&2
    echo "  Verify no adapter installer is alive before removing that empty directory." >&2
    exit 1
fi
LOCK_HELD=1

"${PYTHON_CMD[@]}" "$PATH_GUARD" prepare-dirs \
    "$VENV_ROOT" "$CODEX_CONFIG_DIR"
"${PYTHON_CMD[@]}" "$PATH_GUARD" validate-files "$CODEX_CONFIG_FILE"
if ! find_config_python; then
    echo "✗ No installed Codex adapter Python environment was found."
    echo "  Re-run adapters/codex/install.sh, then retry uninstall."
    exit 1
fi

TRANSACTION_DIR="$(mktemp -d "$CONFIG_DIR/codex-uninstall.XXXXXX")"
if [ "$POSIX_PERMISSIONS" -eq 1 ]; then
    chmod 700 "$TRANSACTION_DIR"
fi
BACKUP_FILE="$TRANSACTION_DIR/config.toml.backup"
MANIFEST_FILE="$TRANSACTION_DIR/transaction.json"

if [ "$FORCE_REMOVE" -eq 1 ]; then
    "$CONFIG_PYTHON" "$CONFIG_HELPER" uninstall \
        --config "$CODEX_CONFIG_FILE" \
        --backup "$BACKUP_FILE" \
        --manifest "$MANIFEST_FILE" \
        --force
else
    "$CONFIG_PYTHON" "$CONFIG_HELPER" uninstall \
        --config "$CODEX_CONFIG_FILE" \
        --backup "$BACKUP_FILE" \
        --manifest "$MANIFEST_FILE"
fi

verify_registration_absent() {
    "$CODEX_BIN" mcp list --json | \
        "$CONFIG_PYTHON" -I -c '
import json
import sys

sys.path.insert(0, sys.argv[1])
from adapters.codex.configure import (
    ConfigTransactionError,
    validate_registration_absent,
)

try:
    raw = sys.stdin.buffer.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ConfigTransactionError("Codex registration response is too large")
    validate_registration_absent(json.loads(raw))
except (ConfigTransactionError, ValueError) as error:
    print(f"error: {error}", file=sys.stderr)
    raise SystemExit(1)
' "$PROJECT_ROOT"
}

if ! verify_registration_absent; then
    echo "✗ Could not verify that the Codex registration was removed"
    exit 1
fi

UNINSTALL_SUCCEEDED=1
echo "✅ Codex registration removed"
echo "  Preserved DeepSeek config, logs, and adapter Python environments in:"
echo "  $CONFIG_DIR"
