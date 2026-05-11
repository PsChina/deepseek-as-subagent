#!/usr/bin/env bash
# install.sh — 一键把 deepseek-mcp 装到 Claude Code。
# 跨平台：macOS / Linux (zsh|bash) + Windows Git Bash / MINGW64。
# 无 Python？自动用 uv 装到 ~/.local/（不需要 sudo / 管理员）。
# 幂等：重复跑安全。

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_ROOT/.venv"
CONFIG_DIR="$HOME/.deepseek-mcp"
CONFIG_FILE="$CONFIG_DIR/config.json"
CLAUDE_SKILLS="$HOME/.claude/skills"
CLAUDE_COMMANDS="$HOME/.claude/commands"

echo "▶ deepseek-mcp installer"
echo "  project: $PROJECT_ROOT"
echo ""

# ===== 平台探测 =====
case "$(uname -s 2>/dev/null)" in
    Linux*)               PLATFORM=linux ;;
    Darwin*)              PLATFORM=macos ;;
    MINGW*|CYGWIN*|MSYS*) PLATFORM=windows ;;
    *)                    PLATFORM=unknown ;;
esac
echo "  platform: $PLATFORM"
echo ""

# ===== Step 0: 找 Python；没有就用 uv 装 =====
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

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        return 0
    fi
    echo "  正在装 uv（用户级 Python 管理器，单文件二进制 ~10MB）..."

    if [ "$PLATFORM" = "windows" ]; then
        # MINGW64 / Git Bash on Windows: uv 的 sh installer 不支持，必须走 PowerShell
        if ! command -v powershell.exe >/dev/null 2>&1; then
            echo "✗ powershell.exe 不在 PATH（Windows 环境异常）"
            return 1
        fi
        powershell.exe -ExecutionPolicy ByPass -Command \
            "irm https://astral.sh/uv/install.ps1 | iex" >/dev/null 2>&1 || {
            echo "✗ uv PowerShell installer 失败"
            return 1
        }
    else
        # macOS / Linux
        curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || {
            echo "✗ uv 安装失败，检查网络后重试"
            return 1
        }
    fi

    # uv 装到 ~/.local/bin（0.5+）或 ~/.cargo/bin（旧版）
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1
}

if ! find_python; then
    echo "▶ Python 3.10+ 不在 PATH，开始自动装（用户级，不需要 sudo）..."
    if ! ensure_uv; then
        echo "✗ uv 不可用，请手动从 https://python.org 装 Python 3.10+"
        exit 1
    fi
    echo "  uv 装 Python 3.12..."
    uv python install 3.12 >/dev/null 2>&1 || {
        echo "✗ uv python install 失败"
        exit 1
    }
    # uv 装完后没把 python 加到 PATH —— 我们 Step 1 直接用 `uv venv` 建项目 venv
    USE_UV_VENV=1
    echo "  ✓ Python 3.12 已装到 uv 管理的位置"
else
    echo "  Python: $($PYTHON_CMD --version) (using '$PYTHON_CMD')"
    USE_UV_VENV=0
fi
echo ""

# ===== Step 1: 创建 venv =====
if [ ! -d "$VENV" ]; then
    echo "[1/6] 创建 Python venv..."
    if [ "$USE_UV_VENV" = "1" ]; then
        # 没系统 Python，让 uv 用它自己装的 Python 建 venv
        ensure_uv
        uv venv --python 3.12 "$VENV"
    else
        $PYTHON_CMD -m venv "$VENV"
    fi
else
    echo "[1/6] venv 已存在，跳过"
fi

# venv 的 bin 目录在 Unix 是 bin/，Windows 是 Scripts/
if [ -d "$VENV/Scripts" ]; then
    VENV_BIN="$VENV/Scripts"
elif [ -d "$VENV/bin" ]; then
    VENV_BIN="$VENV/bin"
else
    echo "✗ venv created but neither bin/ nor Scripts/ found inside $VENV"
    exit 1
fi
CLI="$VENV_BIN/deepseek-mcp"
[ ! -x "$CLI" ] && [ -x "$CLI.exe" ] && CLI="$CLI.exe"

# ===== Step 2: 装包 =====
# Windows 上 pip 不能自己升级自己（file lock），必须用 python -m pip 间接调
echo "[2/6] 装 deepseek-mcp..."
PYBIN="$VENV_BIN/python"
[ ! -x "$PYBIN" ] && [ -x "$PYBIN.exe" ] && PYBIN="$PYBIN.exe"

# pip 升级是 best-effort：失败不致命（Windows 旧 pip 也能装项目）
"$PYBIN" -m pip install --quiet --upgrade pip 2>/dev/null || true
"$PYBIN" -m pip install --quiet -e "$PROJECT_ROOT"

[ ! -x "$CLI" ] && [ -x "$CLI.exe" ] && CLI="$CLI.exe"

# ===== Step 3: 配置文件 + 交互式问 API key =====
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "[3/6] 配置 DeepSeek..."
    echo ""

    # 默认值
    API_KEY=""
    DEFAULT_KEY_HINT="(回车跳过，之后用编辑器填 $CONFIG_FILE)"

    # 能从终端读才交互（curl | bash 也支持，因为我们 read < /dev/tty）
    INTERACTIVE=0
    if [ -e /dev/tty ] && [ -r /dev/tty ]; then
        INTERACTIVE=1
    elif [ -t 0 ]; then
        INTERACTIVE=1
    fi

    if [ "$INTERACTIVE" = "1" ]; then
        echo "  需要 DeepSeek API key 才能 work。"
        echo "  没有？去 https://platform.deepseek.com 注册 + 充值（¥20 起够用很久）"
        echo "  (沙箱自动跟随 Claude 启动目录，无需配置)"
        echo ""
        # `|| true` 防止 set -e 在用户 Ctrl+C 时整个脚本退出
        if [ -e /dev/tty ] && [ -r /dev/tty ]; then
            read -r -p "  粘贴 DeepSeek API key $DEFAULT_KEY_HINT: " API_KEY < /dev/tty || true
        else
            read -r -p "  粘贴 DeepSeek API key $DEFAULT_KEY_HINT: " API_KEY || true
        fi
        echo ""
    fi

    if [ -z "$API_KEY" ]; then
        API_KEY="PASTE_YOUR_DEEPSEEK_KEY_HERE"
        NEED_KEY=1
    else
        NEED_KEY=0
    fi

    # workspace 不写入：让 MCP server 用 os.getcwd() 跟随 Claude Code 启动目录
    # 高级用户想锁定沙箱：手动加 "workspace": "/abs/path" 字段
    cat > "$CONFIG_FILE" <<EOF
{
  "api_key": "$API_KEY",
  "model": "deepseek-v4-pro",
  "max_turns": 50,
  "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
}
EOF
    chmod 600 "$CONFIG_FILE" 2>/dev/null || true
    if [ "$NEED_KEY" = "0" ]; then
        echo "  ✓ config 已写入（含你刚才输入的 key）"
    else
        echo "  ✓ config 模板已写入（key 占位，之后手动填）"
    fi
else
    echo "[3/6] config.json 已存在，跳过"
    if grep -q "PASTE_YOUR_DEEPSEEK_KEY_HERE" "$CONFIG_FILE"; then
        NEED_KEY=1
    else
        NEED_KEY=0
    fi
fi

# ===== Step 4: 注册到 Claude Code =====
echo "[4/6] 注册 MCP server 到 Claude Code (user scope)..."
if ! command -v claude >/dev/null 2>&1; then
    echo "       ⚠ claude CLI 不在 PATH，跳过注册"
    echo "       (装完 Claude Code 后重跑 install.sh)"
elif claude mcp list 2>/dev/null | grep -q "^deepseek:"; then
    echo "       已注册，跳过"
else
    claude mcp add deepseek -s user -- "$CLI"
fi

# ===== Step 5: 部署 skill + command =====
echo "[5/6] 部署 skill 和 slash command..."
mkdir -p "$CLAUDE_SKILLS" "$CLAUDE_COMMANDS"

deploy_link() {
    local src="$1"; local dst="$2"; local label="$3"
    [ -e "$dst" ] && return 0
    if ln -s "$src" "$dst" 2>/dev/null; then
        echo "       $label (symlink): $dst"
    elif cp -r "$src" "$dst"; then
        echo "       $label (copy): $dst"
        echo "       ⚠ 用了 copy 不是 symlink — 改源码后需重跑 install.sh"
    else
        echo "       ✗ 部署 $label 失败"
        return 1
    fi
}

deploy_link "$PROJECT_ROOT/skills/delegate-to-deepseek" \
            "$CLAUDE_SKILLS/delegate-to-deepseek" "skill"
deploy_link "$PROJECT_ROOT/commands/ds.md" \
            "$CLAUDE_COMMANDS/ds.md" "command"

# ===== Step 6: 添加 pure alias 到 shell rc =====
SHELL_RC=""
case "${SHELL:-}" in
    */zsh) SHELL_RC="$HOME/.zshrc" ;;
    */bash)
        if [ "$(uname -s 2>/dev/null)" = "Darwin" ] && [ -f "$HOME/.bash_profile" ]; then
            SHELL_RC="$HOME/.bash_profile"
        else
            SHELL_RC="$HOME/.bashrc"
        fi
        ;;
    *)
        for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
            [ -f "$rc" ] && SHELL_RC="$rc" && break
        done
        ;;
esac

if [ -n "$SHELL_RC" ] && ! grep -q "===== deepseek-orchestrator:" "$SHELL_RC" 2>/dev/null; then
    echo "[6/6] 加 'pure' alias 到 $SHELL_RC..."
    cat >> "$SHELL_RC" <<'EOF'

# ===== deepseek-orchestrator: 切换 alias =====
# `pure` 启动 Claude 时关闭 DeepSeek 派工（全程纯 Claude）
alias pure='DEEPSEEK_MODE=off claude'
# ===== end deepseek-orchestrator =====
EOF
else
    echo "[6/6] pure alias 已存在或未找到 shell rc，跳过"
fi

echo ""
echo "✅ 安装完成"
echo ""

if [ "${NEED_KEY:-0}" = "1" ]; then
    echo "下一步:"
    echo "  1. 编辑 $CONFIG_FILE 把 api_key 改成你的 DeepSeek key"
    echo "     (没有的话去 https://platform.deepseek.com 拿)"
    echo "  2. 跑 claude，在对话里说: '请调用 ping 工具' 验证"
    echo ""
    # 自动打开配置文件
    if command -v code >/dev/null 2>&1; then
        code "$CONFIG_FILE"
    elif command -v notepad >/dev/null 2>&1; then
        notepad "$CONFIG_FILE" 2>/dev/null || true
    elif command -v open >/dev/null 2>&1; then
        open -t "$CONFIG_FILE" 2>/dev/null || true
    fi
else
    echo "下一步: 跑 claude，主对话提到批量任务时会自动派给 DeepSeek"
    echo "       或显式用 /ds <task> 强制派工"
fi
echo ""
echo "(可选 'pure' alias 下次开终端自动生效)"
echo "卸载: ./uninstall.sh"
