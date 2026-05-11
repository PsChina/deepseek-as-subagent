---
name: delegate-to-deepseek
description: 把所有"中等难度及以下"的任务派给 DeepSeek 跑完整 sub-agent loop（DeepSeek 比 Claude 便宜 ~50x，能派就派）。包括：批量改文件、扫日志、翻译、ETL、写脚本、补测试、写文档、CRUD 增删、单领域 refactor、单组件 / 单 endpoint 实现。**派工粒度铁律：每次派"完整逻辑单元"（DS 内部跑 10-30 turns 一次到位），不要拆"步骤"分多次派 —— 拆太细会被 5 个反派工税（拆任务/上下文重读/验证/起步费/返工）吃掉省钱效果，甚至比不派还贵。** **决策铁律：派工决策必须在 Claude 读源码之前做** —— 一旦 Read 了源码再派工就是双倍消耗。调用 mcp__deepseek__delegate_to_deepseek 工具前只能用 Glob/LS 看范围，不能 Read 文件内容（WebSearch/WebFetch 允许，因为是外部资料）。**DeepSeek sub-agent 不能联网** —— 任务需要外部文档 / 新 API / 错误码 / spec 时，Claude 必须用自己的 WebSearch / WebFetch 先查好，把摘要塞进 context 传给 DS（Anthropic 包了费用，免费）。调用后必须验证结果（不盲信 DeepSeek 自报"完成"），抽样 Read 几个产物确认质量。**只有以下场景才自己干**：(1) 跨领域架构设计 / 选型 ADR；(2) bug 根因分析（推理密集）；(3) 强依赖 CLAUDE.md / dev-cases 上下文；(4) 用户明确说"自己干"；(5) 单文件 < 200 行的微调（DS reasoning overhead 不划算）。环境变量 DEEPSEEK_MODE=off 时跳过本 skill。
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
✅ `WebSearch` / `WebFetch` —— 查外部文档 / 新 API / 错误码（用来给 DS 补 context，Anthropic 包了费用）

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

## 🧩 派工粒度：完整逻辑单元 > 细颗粒步骤

**核心反直觉**：拆得越细 ≠ 越省钱。拆过头反而比不派还贵。

### 5 个"反派工税"（拆越细，越亏）

| 税种 | 机制 |
|---|---|
| **拆任务税** | Claude 想"怎么拆 / 给什么 context / 怎么 task" 本身烧 Claude tokens |
| **上下文重读税** | Claude 一个对话里读过的文件下次还能引用；DS 每次 delegate 是独立进程，**同样的文件要重新读 N 遍** |
| **验证税** | DS 每完成一次 Claude 要 Read 抽样验证；拆越多次 → 验证越多 |
| **DS 起步费** | v4-pro thinking mode 每次启动 ~5-10k reasoning tokens 起步；拆 10 次 = 50-100k 起步费 |
| **碎片返工税** | 子任务之间缺全局视野，产物不一致；返工时拆+重做+重验全来一遍 |

### 数学直觉（按 Claude Opus 等价成本）

| 策略 | 总成本 |
|---|---|
| Claude 自己干完 | 1.0X |
| 派 1 个完整逻辑单元给 DS | ~0.13X ✅ **省 87%** |
| 派 5 个子任务（拆步骤） | ~0.50X 省 50% |
| 派 10 个微任务 | ~0.95X ❌ 几乎不省 |
| 派 20 个细任务 | ~1.88X ❌ **比不派还贵** |

### 真省钱的形态

✅ **派"完整逻辑单元"**，DS 内部 loop 自己跑 10-30 turns 一次到位：
- "实现这个 feature 端到端" → 1 次 delegate，DS 自己 Read/Write/Test 循环
- "把这 50 个文件批量改一遍" → 1 次 delegate，DS 内部跑文件循环
- "扫整个 logs/ 目录提取错误栈" → 1 次 delegate，DS 跑遍所有日志文件

❌ **不要**拆"做 feature 的第 1 步、第 2 步、第 3 步"分别派：
- 每步都要 Claude 拆 + 验证 + DS 重读上下文，5 个税全中
- 不如让 DS 一次性接管整个 feature

### 拆分原则（Claude 干这部分）

Claude 的活是**"识别逻辑单元 + 设计接口 + 整合"**，DS 的活是**"单元的完整实现"**：

1. **识别**：什么是"完整逻辑单元"？接口清晰、可独立验证、自包含（不依赖另一个 DS 任务的产物）
2. **设计**：单元之间的输入输出格式（schema / 文件路径），Claude 定，DS 实现
3. **整合**：DS 干完后 Claude 串起来，必要时做最后的胶水代码 / 验证

### 一个测试：要不要再拆？

派工前问自己：**"这个子任务能给一个 1 周新人，一次性给完所有 context，让他独立完成吗？"**
- 能 → 派给 DS 没问题
- 不能（需要中途回来问问题 / 看前序结果）→ **不要拆出来**，让它和前序合并成一个更大的单元

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

DeepSeek 进入 sub-agent 后**看不到**主对话历史、CLAUDE.md、dev-cases、Claude 内存、**也不能联网**。所有它需要的上下文（包括外部资料）**必须**通过 `task` 和 `context` 参数传过去。

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

## 🌐 用 Claude 自己的 WebSearch / WebFetch 给 DS 补外部知识

**关键认识**：DeepSeek sub-agent **不能联网**（沙箱阻 curl/wget，也没暴露 web 工具）。Claude 的 `WebSearch` / `WebFetch` 是 Anthropic 后端实现的，包含在 Max OAuth 订阅里 —— **不额外花钱**。

**派工前规则**：如果任务需要 Claude 自己不熟的外部知识，**Claude 应该用 WebSearch / WebFetch 查好，把结果摘要塞进 `context`**。这条规则不破坏铁律（不 Read 项目源码），因为 Web 工具拿到的是外部资料，不是项目代码的 sunk cost。

### 何时该 pre-flight 搜索

| 任务里出现的信号 | Claude 该搜什么 |
|---|---|
| 用新版本 / 新框架 API（"FastAPI 0.115"、"Tailwind v4"） | 最新文档 / changelog / breaking changes |
| 用 Claude 不确定的库（小众 / niche） | 库的 README + 主要 API 示例 |
| 实现某协议 / spec（"OIDC"、"WebRTC SDP"） | spec 关键章节摘要 |
| 修一个有错误码的 bug | 错误码对应的官方说明 / 已知 issue |
| 用某 SaaS API（DeepSeek API、Stripe API） | 官方 endpoint + 参数 schema 摘要 |
| 性能优化某算法 | 已知最佳实现 / benchmark 数据 |

### Pre-flight 搜索模板

```
1. 用 WebSearch 查 1-3 个 query（不要狂搜，省 Anthropic 配额）
2. 摘要关键信息：
   - API 签名 / 参数表
   - 必要的 import / setup
   - 常见坑 / breaking change
3. 把摘要塞进 delegate_to_deepseek(context=...) 的开头
4. 派工
```

### 实例：DS 实现一个 fastapi SSE endpoint

**❌ 不 pre-flight 的派工（DS 拿不到最新文档，写出来可能用 0.95 时代的旧 API）**：
```
task="实现一个 fastapi SSE endpoint /events 推流。"
context="项目用 fastapi 0.115。"
```

**✅ pre-flight 后的派工**：
```
（先 Claude 端调用 WebSearch："fastapi SSE EventSourceResponse 0.115 example"）
（拿到关键代码片段，摘要进 context）

task="实现 fastapi SSE endpoint /events 推流。"
context="项目 fastapi 0.115，参考 API 用法：
- from sse_starlette.sse import EventSourceResponse
- 返回 EventSourceResponse(generator())
- generator 是 async def，yield dict {'event': 'msg', 'data': '...'}
- 客户端用 EventSource API 接收

边界：放在 api/events.py，复用 db session = Depends(get_session)
成功标准：curl -N localhost:8000/events 拿到 SSE 流。"
```

第二种 DS 一次就写对的概率显著提高。

### 何时不需要 pre-flight

- DS 应该会的常识（Python stdlib、shell 命令、SQL 基础）
- 项目内部 idiom（用 Glob/LS 收集而非 web 搜索）
- 任务本身就是搜索（"扫这些日志找 X"）—— 没什么需要外部资料的

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
