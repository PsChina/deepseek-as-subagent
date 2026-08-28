# deepseek-as-subagent

[English](README.md) · **简体中文**

[![Python](https://img.shields.io/badge/python-3.10--3.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/PsChina/deepseek-as-subagent?style=social)](https://github.com/PsChina/deepseek-as-subagent)
[![Glama MCP server](https://glama.ai/mcp/servers/PsChina/deepseek-as-subagent/badges/score.svg)](https://glama.ai/mcp/servers/PsChina/deepseek-as-subagent)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)](https://modelcontextprotocol.io/)
[![Mentioned in Awesome MCP Servers](https://awesome.re/mentioned-badge.svg)](https://github.com/punkpeye/awesome-mcp-servers)
[![Platforms](https://img.shields.io/badge/platforms-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)](https://github.com/PsChina/deepseek-as-subagent)

> 让 DeepSeek 在 Claude Code / Codex CLI 里作为**真正的 sub-agent**运行，而不只是一个 LLM 接口。
> 主 Agent 保留主对话、规划、判断与验收；DeepSeek 拿到自己的工具循环，负责执行型工作。
> Coding API 使用工作区受限写入和有界 `trusted_host` Bash；独立只读 API 提供不执行命令的纯文件分析。

### 完整 coding 委派

```text
       Claude / Codex（主 Agent）
         ├─ 普通 coding → delegate_to_deepseek
         └─ 方向可能变化的 coding 任务
                           → start_deepseek → job_id
                                              ├─ send_deepseek_message(job_id, ...)
                                              ├─ get_deepseek_status(job_id)
                                              ├─ cancel_deepseek(job_id)
                                              └─ get_deepseek_result(job_id)
         ▼
       DeepSeek coding sub-agent
         │  Read / Write / Edit / Bash / Glob / Grep / NotebookEdit
         │  在工作区内自主读取、修改、运行和测试
         ▼
       结果返回主 Agent
       主 Agent 抽样检查改动 / 测试后再汇报
```

Coding Bash 在可信宿主机上以 `cwd=workspace` 运行；它有时限、输出和凭证隔离，
但不是操作系统级沙箱。

### 只读分析委派

```text
       Claude / Codex（主 Agent）
         ├─ 普通只读分析 → delegate_to_deepseek_readonly
         └─ 方向可能变化的只读分析任务
                           → start_deepseek_readonly → job_id
                                                       ├─ send_deepseek_message(job_id, ...)
                                                       ├─ get_deepseek_status(job_id)
                                                       ├─ cancel_deepseek(job_id)
                                                       └─ get_deepseek_result(job_id)
         ▼
       DeepSeek 只读 sub-agent
         │  Read / Glob / Grep
         │  自主阅读、搜索、review 和静态分析
         ▼
       分析结果返回主 Agent
       主 Agent 核验结论
```

## 快速开始

```bash
git clone https://github.com/PsChina/deepseek-as-subagent.git
cd deepseek-as-subagent
git checkout REVIEWED_TAG_OR_COMMIT
# 先审查 install.sh 与 requirements.lock，再执行：
./install.sh
```

需要先安装 Python 3.10–3.12。安装器不会把远程 bootstrap 脚本直接
传给 shell，依赖统一从 `requirements.lock` 按版本和 hash 校验安装。
之后会注册 MCP server，把 skill + `/ds` 命令复制到受保护的 generation，
不会修改 shell 启动文件。helper 在核心注册提交后尽力部署；遇到外来目标会保留并告警。

安装后，POSIX 编辑 `~/.deepseek-mcp/config.json` 填入 DeepSeek API key；
Windows 仅设置 `DEEPSEEK_API_KEY` 环境变量。然后运行 `claude`，例如：

```text
/ds 检查当前工作区并总结代码结构
```

升级时会先验证现有运行配置。coding 固定使用 `trusted_host`；只读 API
固定只使用 Read/Glob/Grep，不依赖容器运行时。

Codex 和其它 MCP 客户端见下方安装说明。

## 和普通 DeepSeek MCP 有什么不同？

很多 DeepSeek MCP 只暴露一次模型调用。主 Agent 仍然需要自己读文件、整理上下文、再把内容喂给 DeepSeek，因此只省“思考”成本，不省“读写执行”成本。

本项目给 DeepSeek **完整 agent loop**：工具调度、文件 I/O、coding 时的命令执行与多轮推理都由 DeepSeek 自己完成。主 Agent 可以直接把一个完整逻辑单元交出去，再拿结果回来验收。

## 包含什么

- **MCP server**（Python，stdio）
- **coding 与只读委派**：`delegate_to_deepseek` / `delegate_to_deepseek_readonly`
- **后台可控任务**：`start_deepseek` / `start_deepseek_readonly` 与共用控制 API
- **DeepSeek 本地 agent loop**（`agent_loop.py`）
- **固定 capability API**：coding 为 Read / Write / Edit / Bash / Glob / Grep / NotebookEdit；只读为 Read / Glob / Grep
- **Bash 执行**：通过 tool-child 边界运行有界且隔离凭证的 `trusted_host`
- **工作区路径边界**：文件工具拒绝指向工作区外的符号链接
- **跨进程执行租约**：多个 MCP server 也不能同时对同一工作区执行 DeepSeek
- **崩溃安全 mutation journal**：恢复查询、文件核验、精确确认完成前禁止再次委派
- **显式网络重试策略**：关闭 OpenAI SDK 内层重试，避免代理/TLS timeout 环境下出现重试叠加
- Claude Code 的 skill 与 `/ds` 命令；临时禁用请运行 `DEEPSEEK_MODE=off claude`

## 兼容性

既有 MCP 入口 `ping()` 与 `delegate_to_deepseek(task, context="")` 保持
输入 schema；后台任务与恢复工具都是增量新增。这保证输入 schema 兼容，
但会写文件的老宿主必须接入新增的“恢复查询 → 文件核验 → 精确确认”流程，
才能继续下一次委派；只读用法无需调整。健康检查和错误文本包含了更明确的
诊断信息，不承诺逐字节不变。Provider 仍使用 DeepSeek 的
[OpenAI-compatible Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)。
本地 Python 模块签名属于实现细节，不作为稳定公共 API。

## 安装

### Claude Code（默认）

```bash
git clone https://github.com/PsChina/deepseek-as-subagent
cd deepseek-as-subagent
./install.sh
```

然后在 POSIX 编辑 `~/.deepseek-mcp/config.json`；Windows 仅设置
`DEEPSEEK_API_KEY` 环境变量。

### Codex CLI

```bash
git clone https://github.com/PsChina/deepseek-as-subagent
cd deepseek-as-subagent
bash adapters/codex/install.sh
```

详细安装、自动派工策略和后台 job 使用方式见 [adapters/codex/README.md](adapters/codex/README.md)。

Claude 和 Codex 安装器都会新建隔离运行时，验证配置与 MCP 协议后才切换宿主
注册，并保留当前 generation 与一个上一代 generation 供故障恢复。手工安装时，
如果要启用文件写入工具，Python 运行时必须放在委派工作区之外；不安全的目录布局
会在启动时被拒绝。
两个安装器都会串行化 install/uninstall 事务；若进程被强杀，会故意留下
fail-closed 空锁。只有确认没有安装/卸载进程后才应人工删除该锁。

### Cursor / Cline / Claude Desktop / 其它 MCP 客户端

MCP server 本身与客户端无关。先用 `pip --require-hashes` 安装
`requirements.lock`，再禁用依赖解析安装本项目，最后把客户端 MCP
配置指向生成的 `deepseek-mcp` 入口。

## 使用

先按任务的**整个预期生命周期**选择能力。只有任务的每一步都只是用 Read、Glob、
Grep 做静态文件分析、且不执行任何命令时，才选择只读；只要任何阶段可能需要 Bash、
测试、build、lint、Git、运行程序、依赖操作、工作区修改，或无法确定是否只读，就选择
coding。

### 1. 普通同步委派

任务不需要中途干预时，按能力直接使用同步 API：

```text
delegate_to_deepseek(task, context)
delegate_to_deepseek_readonly(task, context)
```

前者用于 coding、Bash、测试或任何可能写工作区的任务；后者只用于静态文件分析。
MCP 请求会一直保持到 DeepSeek 完成。

### 2. 后台可控委派

对于较长、可能需要中途加指令或停止的任务，先选择对应的后台 API，随后两类
job 都使用同一组控制接口：

```text
start_deepseek(task, context) / start_deepseek_readonly(task, context) -> job_id
send_deepseek_message(job_id, message)
get_deepseek_status(job_id)
cancel_deepseek(job_id)
get_deepseek_result(job_id)
```

两个 `start_*` API 都会很快返回，DeepSeek 在后台 worker 中继续执行，因此同一个 MCP session 后续还能继续发送控制请求。steering 只能更新任务指令，不能改变该 job 已冻结的工具或 Bash 可用性。

如果 readonly job 后续需要执行命令或修改工作区，应取消或结束它，再用
`start_deepseek` 新建 coding job；steering 不能升级既有 readonly job。

steering 会在模型 / 工具操作之间的安全点生效。cancel 会立即唤醒 API retry backoff，并及时终止正在进行的 provider 或本地工具子进程。

如果新 steering 在 DeepSeek 已经规划出 tool call、但某个旧 tool call 尚未真正执行时到达，该旧 tool call 会被跳过，DeepSeek 下一轮直接按最新指令重新规划。

每个规范化工作区同一时间只允许一个 DeepSeek execution，包括由不同 MCP server 进程启动的执行。后台 job ID 与结果只在当前 MCP session 内有效，关闭宿主前应先取回结果。

### 3. mutation 恢复

每次文件 mutation 都会在 commit 前写入持久 journal。结果报告 mutation，或
发生取消、断连、MCP 重启后，先执行：

```text
get_deepseek_recovery()
# 逐项核验实际文件
acknowledge_deepseek_mutations(transaction_ids)
```

精确确认这些 transaction ID 前，新委派会 fail closed。恢复操作不依赖有效的
DeepSeek API key，也不会删除或回滚工作区文件。

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
│       DeepSeek agent loop + 工作区受限工具                       │
│    ↓ HTTPS                                                      │
│  api.deepseek.com                                               │
└─────────────────────────────────────────────────────────────────┘
```

本项目不引入第三方代理或云中转。委派任务、模型消息，以及 agent 选择读取的文件/工具输出会发送到配置的 DeepSeek-compatible API；只应委派该端点获准接收的数据。

## 配置

`~/.deepseek-mcp/config.json`：

```json
{
  "api_key": "sk-...",
  "model": "deepseek-v4-pro",
  "max_turns": 50,
  "max_run_seconds": 18000,
  "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "NotebookEdit"]
}
```

`allowed_tools` 为兼容已有配置和校验而保留；它不用于为某次委派选择能力。
MCP API 会在加载配置后应用各自固定的 profile。

`max_run_seconds` 是单次委派的硬墙钟上限。默认 18,000 秒（5 小时），
可以显式向上调整，但配置可接受的绝对上限是 172,800 秒（48 小时）。
单次 provider 请求在总预算内仍单独限制为最多 180 秒。
同步委派时，MCP 客户端自身的 tool timeout 必须至少是运行上限再加清理
余量；Codex 安装后的默认值是 18,060 秒（5 小时加 60 秒）。

**工作区根目录**默认跟随启动宿主客户端时的当前目录。它是文件工具的路径边界，
也是 coding Bash 的工作目录；对 `trusted_host` Bash 而言它不是操作系统级沙箱。
要锁定固定路径，可加：

```json
"workspace": "/abs/path"
```

运行时可用环境变量覆盖：`DEEPSEEK_API_KEY`、`DEEPSEEK_WORKSPACE`、`DEEPSEEK_MODE=off`。

`delegate_to_deepseek` 与 `start_deepseek` 固定使用完整 coding 工具和有界
`trusted_host` Bash。`delegate_to_deepseek_readonly` 与
`start_deepseek_readonly` 固定只给 Read/Glob/Grep，绝不暴露 Bash，也不依赖
Docker/Podman。能力由所选 API 固定，而不是由 task 参数或模型请求决定，整个
job 生命周期都不能改变。具体边界见 [SECURITY.md](SECURITY.md)。

## 卸载

Claude Code：`./uninstall.sh`。Codex：`bash adapters/codex/uninstall.sh`。

两个卸载器都只移除自己管理的宿主注册，不会删除项目、DeepSeek 配置/API key、日志或账户。

## License

MIT
