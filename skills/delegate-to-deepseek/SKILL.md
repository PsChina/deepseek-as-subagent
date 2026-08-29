---
name: delegate-to-deepseek
description: 默认把中等及以下、批量、重复或机械任务作为完整逻辑单元派给 DeepSeek，并由主 Agent 独立验收。适用于批量改文件、扫日志、翻译、ETL、脚本、测试、文档、CRUD、单领域重构、单组件或单 endpoint。主 Agent 可基于上下文和失败代价调整派工策略；用户显式指令、安全、权限、隐私边界和派工后验证不可突破。DEEPSEEK_MODE=off 时跳过。
---

# delegate-to-deepseek — 主 Agent 派工准则

“主 Agent”指负责决策、整合与最终验收的上层 agent。

## 1. 不可突破的边界

- 用户显式要求派 / 不派、指定执行者或执行方式时，优先服从用户。
- 权限、安全、隐私、敏感信息和非授权写入边界不可绕过。
- `DEEPSEEK_MODE=off` 时不要派工。
- DeepSeek 读取的文件内容会发送到配置的 API endpoint；敏感工作区不得派工。
- coding 能力固定为 Read / Write / Edit / Bash / Glob / Grep / NotebookEdit；readonly 固定为 Read / Glob / Grep。不得通过 task、steering 或其它参数提权。
- coding Bash 是受边界约束的 trusted-host Bash，不是操作系统级沙箱。
- 派工结果必须由主 Agent独立验证，失败由主 Agent 收口。
- 出现文件 mutation、取消、断连或 MCP 重启后，先调用 `get_deepseek_recovery()`，核验实际文件，再用精确 transaction IDs 调用 `acknowledge_deepseek_mutations(...)`；未确认前不要重试 mutation delegation。

## 2. API 选择

| 需求 | API |
|---|---|
| coding，直接等结果 | `delegate_to_deepseek(task, context="", model="flash")` |
| 纯静态文件分析，直接等结果 | `delegate_to_deepseek_readonly(task, context="", model="flash")` |
| coding，需要 steering / status / cancel | `start_deepseek(task, context="", model="flash")` |
| readonly，需要 steering / status / cancel | `start_deepseek_readonly(task, context="", model="flash")` |
| 查询后台任务 | `get_deepseek_status(job_id)` |
| 追加/修正后台指令 | `send_deepseek_message(job_id, message)` |
| 取消后台任务 | `cancel_deepseek(job_id)` |
| 获取最终结果 | `get_deepseek_result(job_id)` |
| 查询 mutation recovery | `get_deepseek_recovery()` |
| 确认已核验 mutation | `acknowledge_deepseek_mutations(transaction_ids)` |

只读、搜索、review 已存在文件且整个任务不执行命令/写文件时用 readonly；其余或不确定时用 coding。readonly job 后续需要 Bash 或写文件时，结束/取消后重新启动 coding job，不能 steering 提权。

## 3. 模型路由

`model` 只允许 `flash` 或 `pro`；不传时使用 `flash`。

- **Flash**：默认通用子代理，约 **Sonnet / Terra 档**。用于正常 coding、review、调查、重构、测试、批处理和常规多文件任务。
- **Pro**：困难任务子代理，约 **Opus / Sol 档**。用于复杂 debugging、架构级推理、困难多文件推理，或 Flash 已明显不足后的升级。
- 没有明确困难信号时保持 Flash；不要因为 Pro 可用就默认 Pro。
- 主 Agent 只选择 `flash/pro`，不要尝试控制 reasoning effort；thinking 档位由用户配置决定。
- background job 启动时冻结模型与能力。需要换模型时结束/取消当前 job，再新建 job。

## 4. 默认派工策略

**默认派给 Flash：**

- 脚本、测试、文档、CRUD、单组件 / 单 endpoint
- 批量修改、重命名、翻译、提取、ETL、日志扫描
- spec 清晰的 feature
- 单领域重构、常规多文件任务
- 静态代码/日志调查（优先 readonly）

**默认由主 Agent 自己处理：**

- 用户明确要求自己处理
- 极小改动：几乎无需读上下文即可完成的 typo / 单变量 rename / 少量注释
- 跨领域架构设计、技术选型、ADR
- 结论高度不明确、需要大量主 Agent 综合上下文的根因分析
- 强依赖主 Agent 私有记忆、CLAUDE.md 或未提供给 DeepSeek 的项目约定

这些是成本优化启发式，不是绝对限制。主 Agent 对任务边界、失败代价和验证手段有高把握时，可以调整；但不可突破第 1 节的边界。

### 快速判断

| 信号 | 默认行为 |
|---|---|
| “写 / 实现 / 补测试 / 批量改 / 翻译 / 提取” | Flash |
| 普通 review / 静态调查 | readonly Flash |
| 明显复杂 debug / 架构级推理 | Pro（若决定委派） |
| Flash 已验证不足 | 新建 Pro 任务 |
| 极小改动 | 主 Agent 自己做 |
| “派给 DS” / `/ds` | 强制派，默认 Flash；明显困难可 Pro |
| “你自己干 / 别派” | 不派 |

## 5. 派工时机

尽量在主 Agent 大量读取项目源码之前决定是否派工，避免主 Agent 和 DeepSeek 重复加载同一批上下文。

派工决策前优先使用：

- Glob / LS / 目录树
- 主 Agent 的只读 Bash：`ls`、`find`、`wc -l`、`git status` 等
- 必要的外部 WebSearch / WebFetch，用于补最新 API / spec / 错误码

避免仅为了决定“要不要派”而先 Read/Grep 大量源码。如果主 Agent 已经拥有相关上下文，则直接利用已有上下文，不必机械遵守该启发式。

## 6. 派工粒度

优先派**完整逻辑单元**，不要把一个 feature 拆成大量微任务。

合适的单元应尽量满足：

- 目标清晰
- 输入/输出边界明确
- 可独立验证
- 所需 context 能一次性给齐

主 Agent负责识别单元、定义接口和最终整合；DeepSeek 负责单元内部的 Read / Implement / Test 循环。

如果一个子任务必须频繁回来询问主 Agent 或依赖前一个子任务的临时结果，通常应合并，而不是继续拆细。

## 7. task / context 怎么写

DeepSeek 看不到主对话历史、主 Agent 私有记忆或未显式提供的项目约定。调用时给足完成任务所需的信息，但不要放 API key、凭证或不应发送到外部 API 的敏感数据。

`task` 至少包含：

- 要做什么
- 涉及路径/范围（已知时）
- 明确边界
- 可验证的成功标准

`context` 只补充真正必要的信息，例如：

- 项目技术栈 / 版本
- 命名、schema、接口约定
- 主 Agent 已知的项目规则摘要
- 外部文档/API 的关键结论
- 已知坑或失败现象

普通任务省略 `model`：

```text
mcp__deepseek__delegate_to_deepseek(
  task="<目标 + 范围 + 成功标准>",
  context="<必要约定 / 外部资料摘要>"
)
```

困难任务显式升级：

```text
mcp__deepseek__delegate_to_deepseek(
  task="<困难任务 + 成功标准>",
  context="<必要上下文>",
  model="pro"
)
```

## 8. 外部知识 pre-flight

DeepSeek 没有 web 工具。任务依赖最新或不熟悉的外部知识时，主 Agent 先查官方/可靠资料，把**摘要**放进 `context`，再派工。

常见触发：

- 新版本框架 / API
- 小众依赖
- 协议 / spec
- SaaS API
- 明确错误码 / breaking change

不需要为 Python stdlib、基础 SQL、常见 shell 等常识做额外 pre-flight。

## 9. 派工后验收

DeepSeek 自报完成不等于完成。主 Agent 至少做与风险匹配的独立验证：

1. 查看关键 diff / 产物文件。
2. 检查 schema、接口、边界和数量级是否符合要求。
3. 能运行测试/静态检查时运行。
4. mutation 任务按 recovery 协议核验和 acknowledge。

问题处理：

- 小问题：主 Agent 直接修。
- 明显遗漏但任务仍适合委派：补充明确反馈后重试；Flash 能力不足时可升级 Pro。
- 大范围错误、权限问题或连续失败：停止派工，由主 Agent 接管。

## 10. Fallback

| 情况 | 处理 |
|---|---|
| MCP / API 未配置或不可用 | 主 Agent 接管 |
| capability/tool not allowed | 不绕过权限；主 Agent 接管或让 operator 调整配置 |
| busy / workspace already owned | 处理现有 job 后再派，不并发写同一 workspace |
| 超 max_turns / 任务过大 | 按独立逻辑单元拆分 |
| Flash 质量不足 | 验证后新建 Pro 任务 |
| 连续两次质量差 | 本会话停止主动派工 |

## 11. 用户显式控制

| 用户说 | 行为 |
|---|---|
| “派给 DS / DeepSeek” | 强制派，默认 Flash；明显困难可 Pro |
| `/ds <task>` | 同上 |
| “你自己干 / 别派” | 不派 |
| `DEEPSEEK_MODE=off` / pure 模式 | 不派 |
