---
description: 显式派工给 DeepSeek sub-agent（绕过 Claude 自动决策）。用法：/ds <task description>
---

# /ds — Delegate to DeepSeek

把后面跟的任务**强制**派给 DeepSeek 处理，绕过 Claude 的自动决策（"该派 / 不该派"）。

## 你要做的

1. 按 `delegate-to-deepseek` skill 的准则准备 `task` 和 `context`：
   - 用 Glob / LS 收集涉及的文件路径
   - 摘要项目约定（命名规则、输出 schema、边界）
   - 写明成功标准

2. 调用 `mcp__deepseek__delegate_to_deepseek` 工具，把用户的请求当作 task 传入：

```
用户输入: $ARGUMENTS
```

3. 工具返回后**必须验证**：
   - Read 抽样产物文件
   - 检查数量 / schema sanity
   - 失败按 skill 的 fallback 策略处理

## 不要做的

- ❌ 不要在调用前问用户"你确定要派吗" —— 用户敲 `/ds` 已经是明确指令
- ❌ 不要在工具返回 ERROR 时直接放弃 —— 按 skill 的 fallback 策略重试或接管
- ❌ 不要把 API key / 凭证塞进 task / context
