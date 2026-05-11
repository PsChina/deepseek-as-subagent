# Codex instructions for `delegate_to_deepseek`

Copy the section below into your `AGENTS.md` (project root) or
`~/.codex/instructions.md` (global). It teaches the Codex model when and how
to use the `delegate_to_deepseek` MCP tool.

---

## Using the `delegate_to_deepseek` tool

You have access to an MCP tool `delegate_to_deepseek` (provided by the
deepseek MCP server). It runs a full sub-agent loop inside a sandboxed
workspace — DeepSeek does its own Read / Write / Edit / Bash / Glob / Grep
and returns a final summary.

### When to delegate ✅

Hand off to DeepSeek when at least two of these apply:
- **Batch**: ≥10 files involved, or ≥1 MB of data
- **Mechanical pattern**: each file/item handled the same way (no per-item
  judgment needed)
- **High tolerance for retry**: if it fails partially, rerunning is cheap
- **Heavy on token-budget**: reading all the files yourself would burn
  significant context

Typical fits:
- "Extract i18n keys from 50 .strings files into a JSON"
- "Scan 200 MB of logs for EXC_BAD_ACCESS stacks"
- "Translate all README.md files in this folder to English"
- "Add docstrings to these 30 legacy Python files"
- "Replace every call to old_api() with new_api() across the codebase"

### When NOT to delegate ❌

Do it yourself when:
- Single file under ~500 lines
- Cross-file design / architectural judgment / refactor decisions
- Bug root-cause analysis (reasoning task)
- Tasks needing project-specific idioms from `AGENTS.md` or other repo context
- User explicitly says "do it yourself" / "don't delegate"

### Critical rule: don't read before deciding

**The delegation decision must happen before you read source files.** If you
Read files first and then delegate, both you and DeepSeek pay to read the
same files — net token cost goes up, not down.

Allowed before deciding to delegate:
- `Glob` patterns (count and list matching files)
- `LS` (directory structure)
- Read-only `Bash`: `ls`, `wc -l`, `find -name`, `du -sh`

**Not** allowed before deciding:
- `Read` (file contents)
- `Grep` (file contents)

If you can't decide without reading file contents — you shouldn't delegate.
Just do the task yourself.

### How to call it

```
delegate_to_deepseek(
  task = "<clear task description with file paths and success criteria>",
  context = "<optional project conventions, output schemas, boundaries>"
)
```

DeepSeek can't see your conversation history or your `AGENTS.md`. Anything
it needs (file paths, naming conventions, output format, etc.) must be in
`task` or `context`.

### After delegation: verify

DeepSeek's self-report "done" is not proof of correctness. Always:

1. **Sample-read** 2–3 output files (now allowed — they're new artifacts)
2. **Check schema** matches what you asked for
3. **Sanity-check counts** ("50 input files → ≥50 output entries")
4. **On quality issues**:
   - Minor (a few missing) → fix yourself
   - Major (schema wrong / large gaps) → fix locally then delegate again with
     stricter prompt
   - Catastrophic → take over yourself and report the failed delegation to
     the user

### Fallback when delegation fails

| Symptom | Action |
|---|---|
| `ERROR: deepseek-mcp not configured` | Tell user "DeepSeek not configured, I'll do it myself" and take over |
| `ERROR: DeepSeek API error: ...` | Retry once; if still failing, take over yourself |
| Agent loop hit max_turns | Task too big; split it and delegate in smaller batches |
| Output quality poor twice in a row | Stop delegating in this session; do it yourself |

### Cost intuition

DeepSeek v4-pro runs thinking mode → every call carries reasoning token
overhead. For small tasks (<5k tokens of work), that overhead can exceed
the work itself. Don't delegate tiny tasks just because you *can*.

Sweet spot: 10–50 files, 50KB–500KB total, mechanical pattern.
