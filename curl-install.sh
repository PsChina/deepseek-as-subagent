#!/usr/bin/env bash
# curl-install.sh — one-line remote installer for deepseek-as-subagent.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/PsChina/deepseek-as-subagent/main/curl-install.sh | bash
#
# What it does:
#   1. Clones the repo to ~/.local/share/deepseek-as-subagent (override with DEEPSEEK_MCP_DIR)
#   2. Hands off to the repo's install.sh
#   3. Re-running upgrades to latest main (git pull --ff-only)
#
# install.sh handles everything else:
#   - Auto-installs Python via uv if missing (no sudo / no admin)
#   - Interactively prompts for DeepSeek API key (skip = manual fill later)
#   - Registers MCP with Claude Code, deploys skill + slash command
#   - Cross-platform: macOS / Linux / Windows MINGW64
#
# To uninstall: cd to the install dir and run ./uninstall.sh

set -euo pipefail

REPO_URL="https://github.com/PsChina/deepseek-as-subagent.git"
INSTALL_DIR="${DEEPSEEK_MCP_DIR:-$HOME/.local/share/deepseek-as-subagent}"

echo "▶ deepseek-as-subagent remote installer"
echo "  target: $INSTALL_DIR"
echo

# ===== Only hard prereq: git (install.sh handles the rest) =====
if ! command -v git >/dev/null; then
    echo "✗ git not installed."
    case "$(uname -s 2>/dev/null)" in
        Linux*)               echo "  Install: apt install git / dnf install git / pacman -S git" ;;
        Darwin*)              echo "  Install: run 'xcode-select --install' or 'brew install git'" ;;
        MINGW*|CYGWIN*|MSYS*) echo "  You already have Git Bash, so git should be available — please reinstall Git for Windows" ;;
    esac
    exit 1
fi

# ===== Clone or update =====
mkdir -p "$(dirname "$INSTALL_DIR")"
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "▶ already cloned, pulling latest…"
    cd "$INSTALL_DIR"
    git pull --ff-only
else
    echo "▶ cloning to $INSTALL_DIR…"
    git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

echo

# ===== Hand off to install.sh (handles Python install, key prompt, etc) =====
exec ./install.sh
