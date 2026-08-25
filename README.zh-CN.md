# deepseek-as-subagent

[English](README.md) · **简体中文**

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/PsChina/deepseek-as-subagent?style=social)](https://github.com/PsChina/deepseek-as-subagent)
[![Glama MCP server](https://glama.ai/mcp/servers/PsChina/deepseek-as-subagent/badges/score.svg)](https://glama.ai/mcp/servers/PsChina/deepseek-as-subagent)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)](https://modelcontextprotocol.io/)
[![Mentioned in Awesome MCP Servers](https://awesome.re/mentioned-badge.svg)](https://github.com/punkpeye/awesome-mcp-servers)
[![Platforms](https://img.shields.io/badge/platforms-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)](https://github.com/PsChina/deepseek-as-subagent)

> 让 DeepSeek 在 Claude Code / Codex CLI 里作为**真正的 sub-agent**运行，而不只是一个 LLM 接口。
> 主 Agent 保留主对话、规划、判断与验收；DeepSeek 拿到自己的 Read / Write / Edit / Bash / Glob / Grep / NotebookEdit 工具循环，负责执行型工作。

```text
       Claude / Codex（主 Agent）
         │
         ├─ 普通任务 → delegate_to_deepseek(task, context)
         │
         └─ 可中途控制任务 → start_deepseek(task, context) → job_id
                                  │
                                  ├─ send_deepseek_message(job_id, ...)
                                  ├─ get_deepseek_status(job_id)
                                  ├─ cancel_deepseek(job_id)
                                  └─ get_deepseek_result(job_id)
         ▼
       DeepSeek sub-agent
         │  Read / Write / Edit / Bash / Glob / Grep / NotebookEdit —— 全部本地
         │  在工作区里自主循环
         ▼
       结果返回主 Agent
       主 Agent 抽样检查 / 跑测试后再汇报
```

## 快速开始

```bash
curl -sSL https://raw.githubusercontent.com/PsChina/deepseek-as-subagent/main/curl-install.sh | bash
```

一行命令。把仓库 clone 到 `~/.local/share/deepseek-as-subagent`，在隔离 venv 中安装 Python 包，把 MCP server 注册到 Claude Code，部署 skill + `/ds` 命令，并添加 `pure` shell 别名。

安装后编辑 `~/.deepseek-mcp/config.json` 填入 DeepSeek API key，然后运行 `claude`，例如：

```text
/ds write a python hello world to /tmp/hi.py
```

Codex 和其它 MCP 客户端见下方安装说明。

## 和普通 DeepSeek MCP 有什么不同？

很多 DeepSeek MCP 只暴露一次模型调用。主 Agent 仍然需要自己读文件、整理上下文、再把内容喂给 DeepSeek，因此只省“思考”成本，不省“读写执行”成本。

本项目给 DeepSeek **完整 agent loop**：工具调度、文件 I/O、命令执行、多轮推理都由 DeepSeek 自己完成。主 Agent 可以直接把一个完整逻辑单元交出去，再拿结果回来验收。

## 包含什么

- **MCP server**（Python，stdio）
- **简单同步委派**：`delegate_to_deepseek(task, context)`
- **后台可控任务**：`start_deepseek`、`send_deepseek_message`、`get_deepseek_status`、`cancel_deepseek`、`get_deepseek_result`
- **DeepSeek 本地 agent loop**（`agent_loop.py`）
- **7 个沙箱工具**：Read / Write / Edit / Bash / Glob / Grep / NotebookEdit
- **路径沙箱 + 命令黑名单**（`safety.py`）
- **单执行槽 V1**：同步委派和后台 job 不能同时执行，避免两个 DeepSeek 同时改同一个工作区
- **显式网络重试策略**：关闭 OpenAI SDK 内层重试，避免代理/TLS timeout 环境下出现重试叠加
- Claude Code 的 skill、`/ds` 命令和 `pure` 别名

## 安装

### Claude Code（默认）

```bash
git clone https://github.com/PsChina/deepseek-as-subagent
cd deepseek-as-subagent
./install.sh
```

然后编辑 `~/.deepseek-mcp/config.json` 填入 DeepSeek API key。

### Codex CLI

```bash
git clone https://github.com/PsChina/deepseek-as-subagent
cd deepseek-as-subagent
bash adapters/codex/install.sh
```

详细安装、自动派工策略和后台 job 使用方式见 [adapters/codex/README.md](adapters/codex/README.md)。

### Cursor / Cline / Claude Desktop / 其它 MCP 客户端

MCP server 本身与客户端无关。`pip install -e .` 后，把客户端 MCP 配置指向 `<repo>/.venv/bin/deepseek-mcp`。

## 使用

### 1. 普通同步委派

任务不需要中途干预时直接使用：

```text
delegate_to_deepseek(task, context)
```

MCP 请求会一直保持到 DeepSeek 完成。

### 2. 后台可控委派

对于较长、可能需要中途加指令或停止的任务：

```text
start_deepseek(task, context) -> job_id
send_deepseek_message(job_id, message)
get_deepseek_status(job_id)
cancel_deepseek(job_id)
get_deepseek_result(job_id)
```

`start_deepseek` 会很快返回，DeepSeek 在后台 worker 中继续执行，因此同一个 MCP session 后续还能继续发送控制请求。

steering 和 cancel 是**协作式控制**：只会在模型调用 / 工具调用之间的安全点生效，不会硬切断一个正在进行中的模型请求或 Bash 命令。

如果新 steering 在 DeepSeek 已经规划出 tool call、但某个旧 tool call 尚未真正执行时到达，该旧 tool call 会被跳过，DeepSeek 下一轮直接按最新指令重新规划。

V1 故意限制为**同一时间只允许一个 DeepSeek execution**，无论它来自同步 API 还是后台 job。

### Claude Code 辅助入口

- `delegate_to_deepseek` —— 合适时自动调用
- `/ds <task>` —— 强制同步委派
- `pure` —— 本次 Claude 会话禁用 DeepSeek

## 网络重试

项目显式设置 connect/read/write/pool timeout，并在创建 OpenAI-compatible client 时设置：

```text
max_retries=0
```

重试只由项目自己的 `_call_with_retry()` 负责，只对网络错误、429 和 5xx 做有限外层重试。这样可以避免“SDK 内层重试 × agent loop 外层重试”导致一次 TLS handshake timeout 被放大成多轮长等待。

## 委派什么时候最划算

委派决策最好发生在主 Agent 大量读源码之前，否则主 Agent 和 DeepSeek 会重复读取同一批文件。

适合：
- ✅ 多文件实现、机械重构、补测试
- ✅ 日志扫描、ETL、批量转换
- ✅ 明确、可独立验收的完整逻辑单元

不适合：
- ❌ 极小修改，派工开销反而更大
- ❌ 跨领域架构、模糊根因分析、安全敏感判断

## 架构

```text
┌─────────────────────────────────────────────────────────────────┐
│  Claude Code / Codex CLI（主 Agent）                            │
│    ↓ stdio（本地 MCP）                                         │
│  deepseek-as-subagent（Python MCP 进程）                        │
│    ├─ 同步 delegate                                             │
│    └─ 可 steering 的后台 job manager                            │
│         ↓                                                       │
│       DeepSeek agent loop + 本地沙箱工具                         │
│    ↓ HTTPS                                                      │
│  api.deepseek.com                                               │
└─────────────────────────────────────────────────────────────────┘
```

除了真正调用 DeepSeek API 的 HTTPS 请求，其余部分都在本机运行。本项目不引入第三方代理或云中转。

## 配置

`~/.deepseek-mcp/config.json`：

```json
{
  "api_key": "sk-...",
  "model": "deepseek-v4-pro",
  "max_turns": 50,
  "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "NotebookEdit"]
}
```

**工作区（沙箱根）**默认跟随启动宿主客户端时的当前目录。要锁定固定路径，可加：

```json
"workspace": "/abs/path"
```

运行时可用环境变量覆盖：`DEEPSEEK_API_KEY`、`DEEPSEEK_WORKSPACE`、`DEEPSEEK_MODE=off`。

## 卸载

```bash
./uninstall.sh
```

移除 Claude Code 的 MCP 注册、skill 和斜杠命令，不动你的项目和 DeepSeek API 账户。

## License

MIT
