#!/usr/bin/env bash
# install.sh — 一键把 deepseek-mcp 装到 Claude Code。
# 跨平台：macOS / Linux (zsh|bash) + Windows Git Bash / MINGW64。
# 幂等：重复跑安全。

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="$PROJECT_ROOT/requirements.lock"
PATH_GUARD="$PROJECT_ROOT/scripts/installer_path_guard.py"
CLAUDE_HELPERS="$PROJECT_ROOT/scripts/claude_helpers.sh"
CONFIG_DIR="$HOME/.deepseek-mcp"
CONFIG_FILE="$CONFIG_DIR/config.json"
VENV_ROOT="$CONFIG_DIR/claude-venvs"
LEGACY_VENV="$PROJECT_ROOT/.venv"
INSTALL_LOCK="$CONFIG_DIR/claude-install.lock"
CLAUDE_SKILLS="$HOME/.claude/skills"
CLAUDE_COMMANDS="$HOME/.claude/commands"
CLAUDE_BIN="${DEEPSEEK_CLAUDE_BIN:-claude}"

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

# ===== Step 0: 找已安装的受支持 Python =====
PYTHON_CMD=""
supported_python() {
    "$@" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] < (3, 13) else 1)' \
        >/dev/null 2>&1
}

find_python() {
    local candidate version
    for candidate in python3.12 python3.11 python3.10 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && supported_python "$candidate"; then
            PYTHON_CMD="$candidate"
            return 0
        fi
    done
    if command -v py >/dev/null 2>&1; then
        for version in -3.12 -3.11 -3.10; do
            if supported_python py "$version"; then
                PYTHON_CMD="py $version"
                return 0
            fi
        done
    fi
    return 1
}

if ! find_python; then
    echo "✗ PATH 中没有受支持的 Python 3.10–3.12。"
    echo "  请先从 https://www.python.org/downloads/ 安装 Python 3.12，再重跑。"
    echo "  安装器不会下载或执行远程 bootstrap 脚本。"
    exit 1
else
    echo "  Python: $($PYTHON_CMD --version) (using '$PYTHON_CMD')"
fi
if [ ! -r "$LOCK_FILE" ]; then
    echo "✗ 缺少依赖锁: $LOCK_FILE"
    exit 1
fi
if [ ! -r "$CLAUDE_HELPERS" ]; then
    echo "✗ 缺少 Claude helper 部署脚本: $CLAUDE_HELPERS" >&2
    exit 1
fi
. "$CLAUDE_HELPERS"
echo ""

normalize_command() {
    local converted=""
    if [ "$PLATFORM" = "windows" ] && command -v cygpath >/dev/null 2>&1; then
        converted="$(cygpath -u "$1" 2>/dev/null || true)"
    fi
    if [ -n "$converted" ]; then
        printf '%s' "$converted"
    else
        printf '%s' "$1"
    fi
}

same_command_path() {
    [ "$(normalize_command "$1")" = "$(normalize_command "$2")" ]
}

is_managed_registration() {
    local candidate relative generation suffix token first
    candidate="$(normalize_command "$1")"
    case "$candidate" in
        "$LEGACY_VENV/bin/deepseek-mcp"|"$LEGACY_VENV/bin/deepseek-mcp.exe"|\
        "$LEGACY_VENV/Scripts/deepseek-mcp"|"$LEGACY_VENV/Scripts/deepseek-mcp.exe")
            return 0
            ;;
        "$VENV_ROOT"/*) relative="${candidate#"$VENV_ROOT"/}" ;;
        *) return 1 ;;
    esac
    generation="${relative%%/*}"
    suffix="${relative#*/}"
    [ "$generation/$suffix" = "$relative" ] || return 1
    case "$suffix" in
        bin/deepseek-mcp|bin/deepseek-mcp.exe|\
        Scripts/deepseek-mcp|Scripts/deepseek-mcp.exe) ;;
        *) return 1 ;;
    esac
    case "$generation" in generation.*) token="${generation#generation.}" ;; *) return 1 ;; esac
    [ -n "$token" ] && [ "${#token}" -le 128 ] || return 1
    case "$token" in *[!A-Za-z0-9_-]*) return 1 ;; esac
    first="${token%"${token#?}"}"
    case "$first" in [A-Za-z0-9]) return 0 ;; *) return 1 ;; esac
}

registration_snapshot() {
    local details="" command="" custom="" listing=""
    if details="$("$CLAUDE_BIN" mcp get deepseek 2>/dev/null)"; then
        command="$(printf '%s\n' "$details" | sed -n 's/^  Command: //p' | tr -d '\r')"
        custom="$(printf '%s\n' "$details" | sed -n -e 's/^  Args: //p' \
            -e '/^  Environment:$/,/^$/ { /^    /p; }')"
        [ -n "$command" ] && [ -z "$custom" ] \
            && printf '%s\n' "$details" | grep -q '^  Scope: User' \
            && printf '%s\n' "$details" | grep -q '^  Type: stdio$' \
            && is_managed_registration "$command" || return 1
        printf 'present:%s' "$command"
        return 0
    fi
    listing="$("$CLAUDE_BIN" mcp list 2>/dev/null)" || return 1
    printf '%s\n' "$listing" | grep -q '^deepseek:' && return 1
    printf 'absent'
}

registration_matches_expected() {
    local expected="$1" snapshot="" current=""
    snapshot="$(registration_snapshot)" || return 1
    if [ -z "$expected" ]; then
        [ "$snapshot" = "absent" ] || return 1
        return 0
    fi
    case "$snapshot" in present:*) current="${snapshot#present:}" ;; *) return 1 ;; esac
    same_command_path "$current" "$expected"
}

$PYTHON_CMD "$PATH_GUARD" prepare-dirs "$CONFIG_DIR" "$VENV_ROOT"
$PYTHON_CMD "$PATH_GUARD" secure-files "$CONFIG_FILE"

GENERATION_DIR="" INSTALL_SUCCEEDED=0 REGISTRATION_TRANSACTION=0 PRESERVE_GENERATION=0 LOCK_HELD=0 CLAUDE_AVAILABLE=0 REGISTERED_COMMAND=""

cleanup_generation() {
    [ -n "$GENERATION_DIR" ] || return 0
    if ! $PYTHON_CMD "$PATH_GUARD" delete-generation \
        "$VENV_ROOT" "$GENERATION_DIR"; then
        echo "warning: 无法安全清理未完成的运行时: $GENERATION_DIR" >&2
    fi
}

release_install_lock() {
    if [ "$LOCK_HELD" -eq 1 ]; then
        if ! rmdir "$INSTALL_LOCK"; then
            echo "warning: 无法释放安装锁: $INSTALL_LOCK" >&2
        fi
        LOCK_HELD=0
    fi
}

on_exit() {
    local exit_code=$?
    if [ "$HELPER_TRANSACTION" -eq 1 ]; then
        trap '' INT TERM HUP
        rollback_helper_transaction \
            || echo "warning: 安装中断后 helper 恢复不完整；请查看上方路径。" >&2
    fi
    if [ "$REGISTRATION_TRANSACTION" -eq 1 ]; then
        REGISTRATION_TRANSACTION=0
        trap '' INT TERM HUP
        echo "  正在恢复 Claude MCP 注册..." >&2
        if ! restore_registration "$REGISTERED_COMMAND"; then
            PRESERVE_GENERATION=1
            echo "✗ 注册恢复失败；保留候选运行时: $GENERATION_DIR" >&2
        fi
    fi
    if [ "$INSTALL_SUCCEEDED" -ne 1 ] && [ "$PRESERVE_GENERATION" -ne 1 ]; then
        cleanup_generation
    fi
    release_install_lock
    return "$exit_code"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

# mkdir is an atomic, cross-process mutex on every supported platform.  A
# process killed with SIGKILL intentionally leaves the directory behind so the
# next installer fails closed instead of guessing whether pruning is safe.
if ! mkdir "$INSTALL_LOCK" 2>/dev/null; then
    echo "✗ 另一个安装/卸载事务正在运行，或上次异常退出留下了锁:" >&2
    echo "  $INSTALL_LOCK" >&2
    echo "  确认没有 install.sh/uninstall.sh 运行后再手动删除该空目录。" >&2
    exit 1
fi
LOCK_HELD=1

# Snapshot the existing registration only after taking the lease.  An
# unparseable or foreign registration is never overwritten.
if command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
    CLAUDE_AVAILABLE=1
    REGISTERED_SNAPSHOT="$(registration_snapshot)" || {
        echo "✗ deepseek MCP 注册存在但不属于此安装器，或无法安全解析。" >&2
        echo "  为避免覆盖用户配置，本次安装已中止。" >&2
        exit 1
    }
    case "$REGISTERED_SNAPSHOT" in
        present:*) REGISTERED_COMMAND="${REGISTERED_SNAPSHOT#present:}" ;;
        absent) REGISTERED_COMMAND="" ;;
        *) echo "✗ 无法读取 deepseek MCP 注册。" >&2; exit 1 ;;
    esac
fi

GENERATION_DIR="$(mktemp -d "$VENV_ROOT/generation.XXXXXX")"
case "$PLATFORM" in
    windows) ;;
    *) chmod 700 "$GENERATION_DIR" ;;
esac

# ===== Step 1: 创建隔离 generation =====
echo "[1/7] 创建隔离 Python 运行时..."
$PYTHON_CMD -m venv "$GENERATION_DIR"

# venv 的 bin 目录在 Unix 是 bin/，Windows 是 Scripts/
if [ -d "$GENERATION_DIR/Scripts" ]; then
    VENV_BIN="$GENERATION_DIR/Scripts"
elif [ -d "$GENERATION_DIR/bin" ]; then
    VENV_BIN="$GENERATION_DIR/bin"
else
    echo "✗ venv created but neither bin/ nor Scripts/ found inside $GENERATION_DIR"
    exit 1
fi
CLI="$VENV_BIN/deepseek-mcp"
[ ! -x "$CLI" ] && [ -x "$CLI.exe" ] && CLI="$CLI.exe"

# ===== Step 2: 装包 =====
echo "[2/7] 装 deepseek-mcp..."
PYBIN="$VENV_BIN/python"
[ ! -x "$PYBIN" ] && [ -x "$PYBIN.exe" ] && PYBIN="$PYBIN.exe"
$PYTHON_CMD "$PATH_GUARD" validate-venv "$GENERATION_DIR" "$PYBIN"

if ! supported_python "$PYBIN"; then
    echo "✗ 新运行时使用了不受支持的 Python: $($PYBIN --version 2>&1 || true)"
    exit 1
fi

"$PYBIN" -m pip install --quiet --only-binary=:all: --require-hashes -r "$LOCK_FILE"
"$PYBIN" -m pip install --quiet --no-deps --no-build-isolation "$PROJECT_ROOT"
"$PYBIN" -m pip check

[ ! -x "$CLI" ] && [ -x "$CLI.exe" ] && CLI="$CLI.exe"
if [ ! -e "$CLI" ]; then
    echo "✗ deepseek-mcp entrypoint was not created at $CLI"
    exit 1
fi

# ===== Step 3: 配置文件 + 交互式问 API key =====
$PYTHON_CMD "$PATH_GUARD" prepare-dirs "$CONFIG_DIR"
$PYTHON_CMD "$PATH_GUARD" secure-files "$CONFIG_FILE"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "[3/7] 配置 DeepSeek..."
    echo ""

    # Disable xtrace before any secret-bearing assignment or expansion.  Restore
    # it only after both the raw and escaped values have been cleared.
    XTRACE_WAS_ON=0
    case "$-" in
        *x*) XTRACE_WAS_ON=1; set +x ;;
    esac

    # 默认值
    API_KEY=""
    DEFAULT_KEY_HINT="(回车跳过，之后用编辑器填 $CONFIG_FILE)"

    # POSIX 仅在本地终端可读时交互；Windows 只允许环境变量 key。
    INTERACTIVE=0
    if [ "$PLATFORM" = "windows" ]; then
        INTERACTIVE=0
    elif [ -e /dev/tty ] && [ -r /dev/tty ]; then
        INTERACTIVE=1
    elif [ -t 0 ]; then
        INTERACTIVE=1
    fi

    if [ "$INTERACTIVE" = "1" ]; then
        echo "  需要 DeepSeek API key 才能 work。"
        echo "  没有？去 https://platform.deepseek.com 注册 + 充值（¥20 起够用很久）"
        echo "  (沙箱自动跟随 Claude 启动目录，无需配置)"
        echo ""
        # -s 静默：API key 不回显到屏幕 / scrollback
        # || true 防止 set -e 在用户 Ctrl+C 时整个脚本退出
        if [ -e /dev/tty ] && [ -r /dev/tty ]; then
            read -rs -p "  粘贴 DeepSeek API key $DEFAULT_KEY_HINT: " API_KEY < /dev/tty || true
        else
            read -rs -p "  粘贴 DeepSeek API key $DEFAULT_KEY_HINT: " API_KEY || true
        fi
        echo ""
        echo ""
        # strip 前后空白（粘贴常带尾空格 / 换行）
        API_KEY="$(printf '%s' "$API_KEY" | tr -d '[:space:]')"
    fi

    if [ -z "$API_KEY" ]; then
        API_KEY="PASTE_YOUR_DEEPSEEK_KEY_HERE"
        NEED_KEY=1
    else
        NEED_KEY=0
    fi

    # Escape the sole interpolated JSON string. Expansion output is not
    # evaluated again by the shell, so command syntax remains ordinary data.
    escaped_value="${API_KEY//\\/\\\\}"
    escaped_value="${escaped_value//\"/\\\"}"

    # workspace 不写入：让 MCP server 用 os.getcwd() 跟随 Claude Code 启动目录
    # 高级用户想锁定沙箱：手动加 "workspace": "/abs/path" 字段
    #
    # umask 077 在子 shell 内生效，确保 config 文件创建时就是 600（避免
    # "先 644 后 chmod" 的 race window，本地多用户机器上有意义）
    $PYTHON_CMD "$PATH_GUARD" write-exclusive "$CONFIG_FILE" <<EOF
{
  "api_key": "$escaped_value",
  "flash": "deepseek-v4-flash",
  "flash_reasoning_effort": "high",
  "pro": "deepseek-v4-pro",
  "pro_reasoning_effort": "high",
  "_reasoning_effort_options": ["none", "low", "high", "max"],
  "max_turns": 50,
  "max_run_seconds": 18000,
  "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "NotebookEdit"]
}
EOF
    API_KEY=""
    escaped_value=""
    if [ "$XTRACE_WAS_ON" -eq 1 ]; then
        set -x
    fi
    if [ "$NEED_KEY" = "0" ]; then
        echo "  ✓ config 已写入（含你刚才输入的 key）"
    elif [ "$PLATFORM" = "windows" ]; then
        echo "  ✓ config 模板已写入（保留占位符；真实 key 仅从环境变量读取）"
    else
        echo "  ✓ config 模板已写入（key 占位，之后手动填）"
    fi
else
    echo "[3/7] config.json 已存在，跳过"
    $PYTHON_CMD "$PATH_GUARD" secure-files "$CONFIG_FILE"
    if grep -q "PASTE_YOUR_DEEPSEEK_KEY_HERE" "$CONFIG_FILE"; then
        NEED_KEY=1
    else
        NEED_KEY=0
    fi
fi

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
    echo "✗ 现有 DeepSeek 配置与当前版本不兼容: $CONFIG_FILE" >&2
    echo "  $CONFIG_ERROR" >&2
    echo "  请检查配置字段格式；删除旧的 bash_backend/bash_runtime/bash_image 配置后重试。" >&2
    exit 1
fi

# ===== Step 4: 在切换注册前验证新 generation =====
echo "[4/7] 验证 MCP initialize/list_tools/ping..."
"$PYBIN" "$PROJECT_ROOT/adapters/codex/mcp_smoke.py" "$CLI"

# ===== Steps 5-6: optional helpers deploy only after core registration =====

restore_registration() {
    local previous="$1" snapshot="" current=""
    snapshot="$(registration_snapshot)" || return 1
    case "$snapshot" in present:*) current="${snapshot#present:}" ;; absent) ;; *) return 1 ;; esac
    if [ -n "$previous" ] && [ -n "$current" ] \
        && same_command_path "$current" "$previous"; then
        return 0
    fi
    if [ -z "$previous" ] && [ -z "$current" ]; then
        return 0
    fi
    if [ -n "$current" ]; then
        same_command_path "$current" "$CLI" || return 1
        registration_matches_expected "$current" || return 1
        "$CLAUDE_BIN" mcp remove deepseek -s user >/dev/null 2>&1 || return 1
        registration_matches_expected "" || return 1
    else
        registration_matches_expected "" || return 1
    fi
    if [ -n "$previous" ]; then
        "$CLAUDE_BIN" mcp add deepseek -s user -- "$previous" >/dev/null 2>&1 \
            || return 1
    fi
    registration_matches_expected "$previous"
}

switch_registration() {
    local previous="$1"
    REGISTRATION_TRANSACTION=1
    registration_matches_expected "$previous" || return 1
    if [ -n "$previous" ]; then
        "$CLAUDE_BIN" mcp remove deepseek -s user >/dev/null 2>&1 || return 1
        registration_matches_expected "" || return 1
    fi
    "$CLAUDE_BIN" mcp add deepseek -s user -- "$CLI" >/dev/null 2>&1 || return 1
    registration_matches_expected "$CLI" || return 1
    INSTALL_SUCCEEDED=1 REGISTRATION_TRANSACTION=0
    return 0
}

# Registration is the last state switch. Until here, the old runtime and user
# registration remain untouched, so config/package/smoke failures are harmless.
echo "[7/7] 注册 MCP server 到 Claude Code (user scope)..."
if [ "$CLAUDE_AVAILABLE" -eq 0 ]; then
    echo "       ⚠ claude CLI 不在 PATH，跳过注册"
    echo "       (装完 Claude Code 后重跑 install.sh)"
elif ! switch_registration "$REGISTERED_COMMAND"; then
    echo "✗ Claude MCP 注册切换失败；退出事务时将恢复旧注册。" >&2
    if [ -n "$REGISTERED_COMMAND" ]; then
        echo "  旧运行时仍保留在: $REGISTERED_COMMAND" >&2
    fi
    exit 1
else
    echo "       ✓ 注册已切换到 $CLI"
    INSTALL_SUCCEEDED=1
fi

# Helper assets are optional and are deployed only after the core registration
# has committed.  A foreign/raced path is preserved and reported without
# turning a usable MCP registration into a failed installation.
deploy_claude_helpers

# Success was marked atomically with transaction completion so EXIT cleanup can
# never remove a generation that Claude may actively reference.
[ "$CLAUDE_AVAILABLE" -eq 1 ] || INSTALL_SUCCEEDED=1
if [ "$CLAUDE_AVAILABLE" -eq 1 ]; then
    if ! $PYTHON_CMD "$PATH_GUARD" prune-generations \
        "$VENV_ROOT" "$GENERATION_DIR"; then
        echo "warning: 旧运行时 generation 清理失败；当前安装仍可用。" >&2
    fi
else
    echo "       ⚠ 无法确认活动注册，保留所有旧 generation"
fi

echo ""
echo "✅ 安装完成"
echo "  MCP environment: $GENERATION_DIR"
[ "$HELPER_WARNINGS" -eq 0 ] \
    || echo "  ⚠ 核心 MCP 已安装，但部分可选 helper 未部署；请查看上方 warning。"
echo ""

if [ "${NEED_KEY:-0}" = "1" ]; then
    echo "下一步:"
    if [ "$PLATFORM" = "windows" ]; then
        echo "  1. 设置 DEEPSEEK_API_KEY 环境变量；不要把真实 key 写入 config.json。"
    else
        echo "  1. 编辑 $CONFIG_FILE 把 api_key 改成你的 DeepSeek key"
    fi
    echo "     (没有的话去 https://platform.deepseek.com 拿)"
    echo "  2. 跑 claude，输入: 请调用 ping 工具"
    echo ""
    # Windows 不打开配置文件，避免误把真实 key 持久化。
    if [ "$PLATFORM" != "windows" ]; then
        if command -v code >/dev/null 2>&1; then
            code "$CONFIG_FILE"
        elif command -v open >/dev/null 2>&1; then
            open -t "$CONFIG_FILE" 2>/dev/null || true
        fi
    fi
else
    echo "立即试用:"
    echo "  cd <你的项目目录> && claude     # 已在运行的 claude 需要重启才能加载新 MCP"
    echo "  > /ds 检查当前项目并总结代码结构            # 强制派 DeepSeek 干活"
    echo "  > 请调用 ping 工具                       # 验证 MCP 连接 + 看沙箱根"
    echo ""
    echo "自动派工: 主对话里说\"批量提取 i18n 到 JSON\"之类的任务，Claude 会自己派给 DeepSeek"
    echo "关闭派工: 运行 DEEPSEEK_MODE=off claude（仅当前 Claude 会话）"
fi
echo ""
echo "卸载: ./uninstall.sh"