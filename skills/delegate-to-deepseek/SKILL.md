---
name: delegate-to-deepseek
description: 把批量 / 重复 / 机械性任务派给 DeepSeek 跑完整 sub-agent loop。当用户提到 i18n 提取、批量改文件、扫长日志、翻译大段文本、一次性 ETL、重复模式的 refactor 时考虑使用。**铁律：派工决策必须在 Claude 读源码之前做** —— 一旦 Read 了源码再派工就是双倍消耗（Claude 烧一遍 + DeepSeek 再烧一遍），失去派工意义。调用 mcp__deepseek__delegate_to_deepseek 工具前只能用 Glob/LS 看范围，不能 Read 文件内容。调用后必须验证结果（不盲信 DeepSeek 自报"完成"），抽样 Read 几个产物确认质量。环境变量 DEEPSEEK_MODE=off 时跳过本 skill。
---

# delegate-to-deepseek — Claude 派工给 DeepSeek 的准则

## ⛔ 铁律：派工决策必须在 Claude 读源码之前做

派工是为了**省 Claude 的 token**。如果 Claude 已经 Read 过源码，源码就进了主对话上下文，token 已经烧了。这时再派给 DeepSeek，DeepSeek 还要**再读一遍**同样的文件（它拿不到 Claude 内存里的内容），变成**双倍消耗**：

```
错误时机（双倍消耗）             正确时机（净省）
─────────────────                ──────────────
用户提出任务                     用户提出任务
    │                                │
    ▼                                ▼
Claude Read 50 个文件 ─ 烧 100k     Claude Glob 看范围 ─ 烧 500
    │                                │
    ▼                                ▼
"嗯，看完了，这事得派 DS"          "50 个文件、模式机械" → 立刻派
    │                                │
    ▼                                ▼
派给 DS（DS 再 Read 100k）         DS 一次性接管所有 Read + 处理
    │                                │
  ❌ 总成本 = Claude 100k +         ✅ 总成本 = Claude 500 +
            DS 100k + verify 20k             DS 100k + verify 20k
                                              （省 100k Claude）
```

### 派工决策前允许的工具

✅ `Glob` —— 看有多少文件、什么扩展名
✅ `LS` —— 看目录结构
✅ `Bash` 只读命令 —— `ls`、`wc -l`、`find . -name`、`du -sh`

### 派工决策前**禁止**的工具

❌ `Read` —— 一旦读就污染上下文，sunk cost 让派工不再合算
❌ `Grep` —— 同上，会把匹配行带进上下文

**判断口诀**：**如果不 Read 你判断不了"该不该派"——那就别派了，自己干完。**

---

## 何时该派 ✅ vs 何时不该派 ❌（决策表）

| 任务特征 | 派 / 不派 | 理由 |
|---|---|---|
| ≥10 文件 + 模式明显 + 总大小 >50KB | ✅ **派** | Glob 一句话定范围，DS 接管，纯省 |
| 数据巨大（>1MB） + 简单处理 | ✅ **派** | 日志、批量数据，模型差异不大 |
| 输出 schema 严格 + 内容机械 | ✅ **派** | i18n、SQL 转 ORM、翻译 |
| 单文件 + 小（<500 行） | ❌ **自己干** | DS 调起来的固定 overhead（reasoning tokens）比省的多 |
| 跨文件判断 / 设计 / 重构 | ❌ **自己干** | DS 不如 Claude 推理强，返工成本高 |
| 需要 CLAUDE.md / dev-cases 上下文 | ❌ **自己干** | DS 不知道项目约定，质量打折 |
| Bug debug / 根因分析 | ❌ **自己干** | 推理任务，v4-pro 不如 Claude |
| 3-10 文件 + 模式半明显 | ⚠️ **灰区** | 看总 token 量：<10k 自己干，>30k 派 |
| 用户没明确指示 | ❌ **默认自己干**（保守） | 派错的成本 > 自己干的成本 |
| 用户明说"派给 DS" / `/ds` | ✅ **强制派** | 明确指令优先 |
| 用户明说"你自己干别派" | ❌ **强制不派** | 同上 |

## 💰 token 经济学（让 Claude 心里有账）

### 派工真省钱的公式

```
派工净省 = (Claude 不派会烧的 tokens)
        - (Claude 准备 task + 验证产物 烧的 tokens)
        - (DeepSeek 烧的 tokens × 价格折算系数)
```

### 简化心法

| 任务规模 | 决策 |
|---|---|
| Glob 看到总文件 < 5 个 / 总大小 < 10KB | ❌ 自己干（DS overhead 太大） |
| Glob 看到总文件 10-50 个 / 总大小 50KB-500KB + 模式明显 | ✅ **派**（甜区） |
| Glob 看到总文件 > 100 个 / 总大小 > 1MB | ✅ 派，但**拆批**（每批 30 文件，避免超 max_turns） |

### 用 DeepSeek thinking token 警觉

DeepSeek v4-pro 是 thinking mode，每次调用自带 reasoning tokens（不可见但计费）。
小任务（<5k 输入）的 reasoning overhead 比任务本身还大 ——
**写个 hello world 烧了 8.8k tokens 就是这个原因**。
所以小任务一定要 Claude 自己干，别派。

---

## 派工前必须做的（避免上下文丢失）

DeepSeek 进入 sub-agent 后**看不到**主对话历史、CLAUDE.md、dev-cases。
所有它需要的上下文必须通过 `task` 和 `context` 参数传过去。

调用前**只用 Glob / LS / 只读 Bash**（不要 Read！）收集：

```
1. 用 Glob 列出涉及的文件路径，传给 DeepSeek
2. 摘要项目约定（从你的记忆里，不要去 Read CLAUDE.md）：
   - 命名规则（i18n key 格式、文件命名）
   - 输出 schema（JSON 结构、字段名）
   - 边界（"只动 src/，不动 vendored/"）
3. 明确成功标准：
   - 应该生成什么文件 / 改什么内容
   - 完成的 verifiable 信号（"写一份 keys.json，至少包含 N 条 key"）
```

## 派工模板

```
mcp__deepseek__delegate_to_deepseek(
  task="把 <project>/Resources/*.lproj/Localizable.strings 里的英文 key
        提取出来，生成 keys.json，schema: { 'file': str, 'keys': [str] }。
        逐文件处理，最终写到 <project>/keys.json。
        (路径用相对 cwd 即可 —— DeepSeek 沙箱根 = Claude 启动目录)",

  context="项目约定：
  - i18n key 命名是 lowerCamelCase
  - Localizable.strings 格式: \"key\" = \"value\";
  - 注释行（// 开头）忽略
  - 涉及文件清单：[Claude 用 Glob 找到的路径列表]
  - 完成后请抽样 3 个文件 verify"
)
```

## 派工后必须做的（避免盲信）

DeepSeek 自报"完成"不等于真的完成。**Claude 必须验证**：

```
1. 用 Read 抽样读 2-3 个产物文件（不必读全部）—— 这次允许 Read，因为是新产物
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
| `ERROR: DeepSeek API error` | 重试 1 次；仍失败 → 自己接管 |
| Agent loop 超 max_turns | 任务可能太大；拆小再派（"先做前 25 个文件"） |
| DeepSeek 干出来质量差 | 验证后修；累计 2 次差 → 后续主动跳过 delegate |

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
| 启动用 `pure` 命令 | DEEPSEEK_MODE=off，本工具会立即返回 disabled |
