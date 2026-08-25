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

# Match the root installer closely so Codex support is not weaker on Windows.
PYTHON_CMD=""
find_python() {
    for candidate in python3 python "py -3"; do
        bin="${candidate%% *}"
        if command -v "$bin" >/dev/null 2>&1; then
            if $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
                PYTHON_CMD="$candidate"
                return 0
            fi
        fi
    done
    return 1
}

if ! find_python; then
    echo "✗ Python 3.10+ was not found in PATH"
    echo "  Install Python 3.10+ and re-run this script."
    exit 1
fi

if [ ! -d "$VENV" ]; then
    echo "[1/5] Creating Python venv..."
    $PYTHON_CMD -m venv "$VENV"
else
    echo "[1/5] Python venv already exists"
fi

if [ -d "$VENV/Scripts" ]; then
    VENV_BIN="$VENV/Scripts"
elif [ -d "$VENV/bin" ]; then
    VENV_BIN="$VENV/bin"
else
    echo "✗ venv exists but neither Scripts/ nor bin/ was found in $VENV"
    exit 1
fi

PYBIN="$VENV_BIN/python"
[ ! -x "$PYBIN" ] && [ -x "$PYBIN.exe" ] && PYBIN="$PYBIN.exe"
CLI="$VENV_BIN/deepseek-mcp"
[ ! -x "$CLI" ] && [ -x "$CLI.exe" ] && CLI="$CLI.exe"

echo "[2/5] Installing deepseek-as-subagent..."
"$PYBIN" -m pip install --quiet --upgrade pip 2>/dev/null || true
"$PYBIN" -m pip install --quiet -e "$PROJECT_ROOT"

[ ! -x "$CLI" ] && [ -x "$CLI.exe" ] && CLI="$CLI.exe"
if [ ! -e "$CLI" ]; then
    echo "✗ deepseek-mcp entrypoint was not created at $CLI"
    exit 1
fi

# Catch dependency/import incompatibilities immediately rather than reporting a
# successful install that will fail only when Codex launches the MCP process.
echo "[3/5] Running MCP import smoke test..."
if ! "$PYBIN" -c 'from deepseek_mcp.server import mcp; print("       MCP import OK")'; then
    echo "✗ deepseek-mcp import smoke test failed"
    echo "  Check the Python dependency installation above before retrying."
    exit 1
fi

mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "[4/5] Creating DeepSeek config template..."
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
    echo "[4/5] DeepSeek config already exists"
fi

# Re-register instead of merely skipping an existing entry. This repairs stale
# paths when the repository was moved/re-cloned and makes reruns self-healing.
echo "[5/5] Registering MCP server with Codex..."
if codex mcp get deepseek >/dev/null 2>&1; then
    echo "       replacing existing deepseek MCP registration"
    if ! codex mcp remove deepseek >/dev/null 2>&1; then
        echo "✗ existing deepseek MCP registration could not be removed"
        echo "  Run 'codex mcp remove deepseek' manually, then retry."
        exit 1
    fi
fi

if ! codex mcp add deepseek -- "$CLI"; then
    echo "✗ failed to register deepseek MCP server with Codex"
    exit 1
fi

if ! codex mcp get deepseek >/dev/null 2>&1; then
    echo "✗ Codex registration verification failed"
    exit 1
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
echo "Codex receives delegation guidance from the MCP server by default."
echo "For stricter or project-specific auto-delegation behavior, also use AGENTS.md."
