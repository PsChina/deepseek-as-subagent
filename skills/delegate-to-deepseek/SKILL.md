---
name: delegate-to-deepseek
description: 把所有"中等难度及以下"的任务派给 DeepSeek 跑完整 sub-agent loop（DeepSeek 比 Claude 便宜得多，能派就派）。包括：批量改文件、扫日志、翻译、ETL、写脚本、补测试、写文档、CRUD 增删、单领域 refactor、单组件 / 单 endpoint 实现。**铁律：派工决策必须在 Claude 读源码之前做** —— 一旦 Read 了源码再派工就是双倍消耗。调用 mcp__deepseek__delegate_to_deepseek 工具前只能用 Glob/LS 看范围，不能 Read 文件内容。调用后必须验证结果（不盲信 DeepSeek 自报"完成"），抽样 Read 几个产物确认质量。**只有以下场景才自己干**：(1) 跨领域架构设计 / 选型 ADR；(2) bug 根因分析（推理密集）；(3) 强依赖 CLAUDE.md / dev-cases 上下文；(4) 用户明确说"自己干"；(5) 单文件 < 200 行的微调（DS reasoning overhead 不划算）。环境变量 DEEPSEEK_MODE=off 时跳过本 skill。
---

# delegate-to-deepseek — Claude 派工给 DeepSeek 的准则

## 🚀 核心理念：能派就派

DeepSeek v4-pro 已经很强，**单价比 Claude Opus 便宜 ~10x**。Claude 的稀缺资源是用户的 Max OAuth 配额，DeepSeek 的稀缺资源只是 ¥（用户已付费）。**默认派**，除非以下硬约束之一命中：

- ❌ 任务依赖 CLAUDE.md / dev-cases / 公司 brain 上下文（DS 拿不到）
- ❌ 跨领域架构设计 / 技术选型 / ADR（需要 Claude 的综合推理）
- ❌ bug 根因分析（推理密集，DS 不如 Claude）
- ❌ 单文件 < 200 行的微调（DS 的 reasoning 起步成本 > 省下的 Claude tokens）
- ❌ 用户明确说"你自己干 / 别派"

**其他全部派**。包括但不限于："写个 X"、"补测试"、"修这个 lint"、"重命名 Y 到 Z"、"扫日志"、"翻译这段"、"实现这个 endpoint"。

## ⛔ 铁律：派工决策必须在 Claude 读源码之前做

派工是为了**省 Claude 的 token**。如果 Claude 已经 Read 过源码，源码就进了主对话上下文，token 已经烧了。再派给 DeepSeek，DS 还要**再读一遍**（拿不到 Claude 内存里的内容），变成**双倍消耗**：

```
错误时机（双倍消耗）             正确时机（净省）
─────────────────                ──────────────
用户提出任务                     用户提出任务
    │                                │
    ▼                                ▼
Claude Read 50 个文件 ─ 烧 100k     Claude Glob 看范围 ─ 烧 500
    │                                │
    ▼                                ▼
"嗯，看完了，这事得派 DS"          "范围清楚了" → 立刻派
    │                                │
    ▼                                ▼
派给 DS（DS 再 Read 100k）         DS 一次性接管所有 Read + 处理
    │                                │
  ❌ 总成本 = Claude 100k +         ✅ 总成本 = Claude 500 +
            DS 100k + verify 20k             DS 100k + verify 20k
                                              （省 100k Claude）
```

### 派工决策前**允许**的工具

✅ `Glob` —— 看有多少文件、什么扩展名
✅ `LS` —— 看目录结构
✅ `Bash` 只读命令 —— `ls`、`wc -l`、`find . -name`、`du -sh`、`git status`

### 派工决策前**禁止**的工具

❌ `Read` —— 一旦读就污染上下文，sunk cost 让派工不再合算
❌ `Grep` —— 同上，会把匹配行带进上下文

**判断口诀**：**判断不了"该不该派"？默认派 —— DS 多烧几千 token 是小事，Claude 多烧 100k 才是大事。**

---

## 难度分级 + 派工决策

| 难度 | 例子 | 默认 |
|---|---|---|
| 🟢 **简单** | 写 hello world / 单脚本、写测试用例、补文档、单 endpoint CRUD、单组件实现 | ✅ **派** |
| 🟡 **中等** | 3-10 文件 batch 改、一个 feature 的实现（spec 清晰）、补全测试、生成 boilerplate、简单 refactor、扫日志 / ETL | ✅ **派** |
| 🟠 **中等偏上** | 10+ 文件批量、一个领域内的 refactor、性能优化（数据已给）、i18n 提取、协议转换 | ✅ **派**（必要时拆批） |
| 🔴 **困难** | 跨领域架构设计、技术选型、ADR、bug 根因分析、需要项目深度约定 | ❌ **自己干** |
| 🌶️ **极小** | 单文件 < 200 行的 typo / rename / 加注释 | ❌ **自己干**（DS overhead 不划算） |

**简单 / 中等 / 中等偏上都派**。不要因为"听起来简单我顺手就做了"而省略派工 —— 那省的是 5 分钟，烧的是几万 Claude tokens。

### 决策快速通道

| 用户说 / 看到 | Claude 行为 |
|---|---|
| "写一个 X" / "实现 Y" / "做一个 Z" | 派（除非命中🔴/🌶️） |
| "重命名 / 批量改 / 翻译 / 提取" | 派 |
| "测试 / 文档 / boilerplate / lint 修" | 派 |
| "为啥这个 bug" / "为啥这里挂了" | 自己干（推理任务） |
| "我应该用 A 还是 B" | 自己干（选型） |
| "改个 typo / rename 一个变量" | 自己干（极小，DS overhead 不值） |
| "派给 DS" / `/ds <任务>` | 强制派 |
| "你自己干" / "别派" | 强制不派 |

---

## 💰 token 经济学（让 Claude 心里有账）

### 派工真省钱的公式

```
派工净省 = (Claude 不派会烧的 tokens)
        - (Claude 准备 task + 验证产物 烧的 tokens)
        - (DeepSeek 烧的 tokens × 价格折算系数 ≈ 0.1x)
```

折算系数 0.1x 意味着 **DS 烧 10k tokens 才相当于 Claude 1k tokens 的钱**。所以即使 task 不大，派工也常常划算。

### 反直觉但常见的"该派"信号

- "这事我 5 分钟自己写完" → **如果要 Read 文件 / 写 50+ 行**，那 5 分钟也烧 10-20k Claude tokens，派给 DS 更便宜
- "DeepSeek 估计要折腾几轮" → 让它折腾，反正它便宜
- "代码量小不至于派吧" → 看是不是 < 200 行**且无依赖读取**。要 Read 几个文件才能开始写？派

### 唯一应该警惕的"不该派"信号

- DS 的 reasoning tokens 起步开销大（v4-pro thinking mode）：**单纯写一个 hello world 也烧 ~8k tokens**
- 所以"几乎无 Read、改动 < 200 行" → Claude 自己 5 行就搞定，比 DS 8k tokens 划算

---

## 派工前必须做的（避免上下文丢失）

DeepSeek 进入 sub-agent 后**看不到**主对话历史、CLAUDE.md、dev-cases、Claude 内存。所有它需要的上下文**必须**通过 `task` 和 `context` 参数传过去。

调用前**只用 Glob / LS / 只读 Bash**（不要 Read！）收集：

```
1. 用 Glob 列出涉及的文件路径（如果有），传给 DeepSeek
2. 摘要项目约定（从 Claude 自己已有的记忆，不要去 Read CLAUDE.md）：
   - 命名规则、输出 schema、边界
   - 技术栈（语言版本、框架、关键依赖）
3. 明确成功标准：
   - 应该生成 / 修改什么
   - 完成的 verifiable 信号（"写一个 fastapi endpoint，curl localhost/x 返回 200"）
```

## 派工模板

```
mcp__deepseek__delegate_to_deepseek(
  task="<清晰描述要做什么 + 成功标准 + 涉及路径>
        (路径用相对 cwd 即可 —— DeepSeek 沙箱根 = Claude 启动目录)",

  context="<项目约定 / 框架版本 / schema / 边界 / 已知坑>
  - 完成后请抽样 verify N 个产物"
)
```

### 实例

**🟢 简单（写脚本）**：
```
task="在 scripts/ 下写一个 batch_rename.py，把当前目录所有 *.JPG 改成 *.jpg。
      用 pathlib，不要 os.system。运行成功后打印改名数量。"
context="Python 3.10+，没有第三方依赖。"
```

**🟡 中等（实现 endpoint）**：
```
task="在 api/users.py 里加一个 GET /users/:id endpoint，返回 user 详情 JSON。
      表已经在 db/schema.sql 里（users 表）。用 FastAPI + SQLAlchemy async。
      成功标准：curl localhost:8000/users/1 返回 {id, name, email}。"
context="项目用 FastAPI 0.115，DB session 注入用 Depends(get_session)。
        路由模块约定：每个文件一个 router 实例，名字叫 router。
        完成后请用 Bash 起服务 + curl 自验。"
```

**🟠 中等偏上（批量提取）**：
```
task="把 Resources/*.lproj/Localizable.strings 里的所有 key 提取到
      keys.json，schema: { 'file': str, 'keys': [str] }。
      逐文件处理，写到 ./keys.json。"
context="key 命名是 lowerCamelCase；.strings 格式: \"key\" = \"value\";
        注释行（// 开头）忽略。完成后抽样 verify 3 个文件。"
```

## 派工后必须做的（避免盲信）

DeepSeek 自报"完成"不等于真的完成。**Claude 必须验证**：

```
1. 用 Read 抽样读 1-2 个产物文件（不必读全部）—— 这次允许 Read，因为是新产物
2. 检查 schema 是否符合要求
3. 数量 sanity check（"50 个文件应该生成 ≥50 条 key"）
4. 如果发现质量问题：
   a. 轻微（几条漏了）→ Claude 自己补
   b. 严重（schema 错 / 大面积缺失）→ Edit 修后再 delegate 一次
   c. 灾难（DeepSeek 完全没干完）→ 自己接管 + 告知用户外包失败
```

## Fallback 策略

| 症状 | 处理 |
|---|---|
| `ERROR: deepseek-mcp not configured` | 告诉用户："DeepSeek 没配 key，我自己干" + Claude 接管 |
| `ERROR: DeepSeek API error` | MCP 已自动重试 2 次；仍失败 → 自己接管 |
| Agent loop 超 max_turns | 任务太大；拆小再派（"先做前 25 个文件"） |
| 产物质量差 | 验证后修；累计 2 次差 → 后续主动跳过 delegate（本会话） |
| 用户连续 2 次 `pure` 启动 | 默认不派工，等用户显式 `/ds` 才派 |

## 与公司 brain rules 的关系

- `proactive-thinking` — 派工前充分收集 context，不留半成品
- `secrets-policy` — 不要把 API key / 敏感数据塞进 task / context 参数
- `event-driven` — 不要 sleep 等 DeepSeek 完成，工具调用同步返回
- `code-quality` — 派工不豁免代码质量责任，验证产物时按 SOLID / LoD 标准抽查

## 用户显式控制

| 用户说 | Claude 行为 |
|---|---|
| "派给 DS" / "外包给 deepseek" | 强制调用本工具，不再自行判断 |
| "你自己干" / "别派" | 禁止调本工具，本对话主动 fallback |
| `/ds <task>` (slash command) | 等同"派给 DS" |
| 启动用 `pure` 命令 | DEEPSEEK_MODE=off，本工具立即返回 disabled |
