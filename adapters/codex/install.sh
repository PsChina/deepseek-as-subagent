#!/usr/bin/env bash
# First-class Codex CLI installer for deepseek-as-subagent.
# Installs the Python MCP server into the repo-local venv and registers it with Codex.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="$PROJECT_ROOT/.venv"
CONFIG_DIR="$HOME/.deepseek-mcp"
CONFIG_FILE="$CONFIG_DIR/config.json"

echo "▶ deepseek-as-subagent — Codex installer"
echo "  project: $PROJECT_ROOT"
echo ""

if ! command -v codex >/dev/null 2>&1; then
    echo "✗ codex CLI was not found in PATH"
    echo "  Install/update Codex first, then re-run this script."
    exit 1
fi

PYTHON_CMD=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        PYTHON_CMD="$candidate"
        break
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo "✗ Python 3.10+ was not found in PATH"
    echo "  Install Python 3.10+ and re-run this script."
    exit 1
fi

if [ ! -d "$VENV" ]; then
    echo "[1/4] Creating Python venv..."
    "$PYTHON_CMD" -m venv "$VENV"
else
    echo "[1/4] Python venv already exists"
fi

if [ -d "$VENV/Scripts" ]; then
    VENV_BIN="$VENV/Scripts"
else
    VENV_BIN="$VENV/bin"
fi

PYBIN="$VENV_BIN/python"
[ ! -x "$PYBIN" ] && [ -x "$PYBIN.exe" ] && PYBIN="$PYBIN.exe"
CLI="$VENV_BIN/deepseek-mcp"
[ ! -x "$CLI" ] && [ -x "$CLI.exe" ] && CLI="$CLI.exe"

echo "[2/4] Installing deepseek-as-subagent..."
"$PYBIN" -m pip install --quiet --upgrade pip 2>/dev/null || true
"$PYBIN" -m pip install --quiet -e "$PROJECT_ROOT"

[ ! -x "$CLI" ] && [ -x "$CLI.exe" ] && CLI="$CLI.exe"
if [ ! -e "$CLI" ]; then
    echo "✗ deepseek-mcp entrypoint was not created at $CLI"
    exit 1
fi

mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "[3/4] Creating DeepSeek config template..."
    (
        umask 077
        cat > "$CONFIG_FILE" <<'EOF'
{
  "api_key": "PASTE_YOUR_DEEPSEEK_KEY_HERE",
  "model": "deepseek-v4-pro",
  "max_turns": 50,
  "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "NotebookEdit"]
}
EOF
    )
    chmod 600 "$CONFIG_FILE" 2>/dev/null || true
else
    echo "[3/4] DeepSeek config already exists"
fi

echo "[4/4] Registering MCP server with Codex..."
if codex mcp get deepseek >/dev/null 2>&1; then
    echo "       deepseek MCP server is already registered; keeping existing entry"
else
    codex mcp add deepseek -- "$CLI"
fi

echo ""
echo "✅ Codex support installed"
echo ""
echo "Verify:"
echo "  codex mcp list"
echo "  codex"
echo "  > call the deepseek ping tool"
echo ""
if grep -q "PASTE_YOUR_DEEPSEEK_KEY_HERE" "$CONFIG_FILE" 2>/dev/null; then
    echo "Before delegating work, edit:"
    echo "  $CONFIG_FILE"
    echo "and replace PASTE_YOUR_DEEPSEEK_KEY_HERE with your DeepSeek API key."
else
    echo "DeepSeek config already contains a key. You can start Codex now."
fi

echo ""
echo "Codex will receive delegation guidance directly from the MCP server."
echo "No AGENTS.md copy/paste is required for the default behavior."
