#!/usr/bin/env bash
# uninstall.sh — 拆掉 deepseek-mcp。

set -euo pipefail

echo "▶ deepseek-mcp uninstaller"

# 1. 从 Claude Code 移除注册
echo "[1/4] 从 Claude Code 移除 mcp..."
claude mcp remove deepseek -s user 2>/dev/null || echo "       未注册或已移除"

# 2. 删 skill / command 链接
echo "[2/4] 删 skill / command 链接..."
rm -f "$HOME/.claude/skills/delegate-to-deepseek"
rm -f "$HOME/.claude/commands/ds.md"

# 3. 提示用户决定是否删配置
echo "[3/4] 配置目录:"
if [ -d "$HOME/.deepseek-mcp" ]; then
    echo "       $HOME/.deepseek-mcp 仍存在（含 API key 和日志）"
    echo "       要删请手动: rm -rf $HOME/.deepseek-mcp"
fi

# 4. 提示用户清 zshrc
echo "[4/4] ~/.zshrc 里的 pure alias:"
if grep -q "===== deepseek-orchestrator:" "$HOME/.zshrc" 2>/dev/null; then
    echo "       仍存在，请手动删除以下段落:"
    echo "       ===== deepseek-orchestrator: 切换 alias ====="
    echo "       ... pure alias ..."
    echo "       ===== end deepseek-orchestrator ====="
fi

echo ""
echo "✅ 主战场 claude 完全不受影响"
echo "   项目目录 $(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) 本身没删，你可以保留代码"
