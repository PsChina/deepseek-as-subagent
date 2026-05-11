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

### Core principle: delegate by default

DeepSeek v4-pro is ~10x cheaper per token than top-tier models. Your scarce
resource is the user's session token budget; DeepSeek's scarce resource is just
¥ (already paid). **Default: delegate.** Only keep tasks yourself when one of
these hard constraints applies:

- ❌ Task depends on context from `AGENTS.md` or other repo files DeepSeek
  can't see
- ❌ Cross-domain architectural design / tech selection / ADR
- ❌ Bug root-cause analysis (reasoning-heavy)
- ❌ Single file < 200 lines, simple edit (DS reasoning overhead > savings)
- ❌ User explicitly said "do it yourself" / "don't delegate"

### Difficulty tiers (delegate green/yellow/orange, keep red/tiny)

| Tier | Examples | Default |
|---|---|---|
| 🟢 Easy | Write hello world / single script, test scaffolding, single CRUD endpoint, single component | ✅ delegate |
| 🟡 Medium | 3-10 file batch, feature impl with clear spec, fill test gaps, generate boilerplate, simple refactor | ✅ delegate |
| 🟠 Medium-hard | 10+ file batch, single-domain refactor, perf opt (data given), i18n extraction, protocol conversion | ✅ delegate (split if needed) |
| 🔴 Hard | Cross-domain design, tech selection, ADR, root-cause analysis, deep project context | ❌ keep yourself |
| 🌶️ Tiny | Single file < 200 lines, typo / rename / add comment | ❌ keep yourself (overhead) |

**Easy / Medium / Medium-hard all go to DeepSeek.** Don't skip delegation just
because "it's quick to do myself" — that "quick" still burns 10-20k of your
own context tokens.

Typical fits:
- "Extract i18n keys from 50 .strings files into a JSON"
- "Scan 200 MB of logs for EXC_BAD_ACCESS stacks"
- "Translate all README.md files to English"
- "Add docstrings to these 30 legacy Python files"
- "Replace every call to old_api() with new_api() across the codebase"
- "Write a fastapi endpoint that returns user JSON"
- "Add unit tests for the parser module"
- "Convert these argparse calls to click"

### Critical rule: don't read before deciding

**The delegation decision must happen before you read source files.** If you
Read files first and then delegate, both you and DeepSeek pay to read the
same files — net token cost goes up, not down.

Allowed before deciding to delegate:
- `Glob` patterns (count and list matching files)
- `LS` (directory structure)
- Read-only `Bash`: `ls`, `wc -l`, `find -name`, `du -sh`
- Web search / web fetch tools (your built-in ones) — see "Pre-flight search" below

**Not** allowed before deciding:
- `Read` (file contents)
- `Grep` (file contents)

If you can't decide without reading file contents — you shouldn't delegate.
Just do the task yourself.

### Pre-flight web search (key insight)

**DeepSeek sub-agent cannot reach the network** — the MCP server's Bash
sandbox blocks curl/wget, and no web tools are exposed to DeepSeek. But
your own web search / fetch tools cost nothing extra (covered by your
subscription / API plan).

**Rule**: if the task needs external knowledge (new framework APIs,
spec excerpts, error code meanings, niche library docs), **you should
search before delegating** and paste the summary into `context`. This
doesn't violate the no-Read rule because web content is external — there
is no sunk-cost double-burn.

When to pre-flight search:
| Signal in task | Search for |
|---|---|
| New version of a framework ("FastAPI 0.115") | Latest changelog / breaking changes |
| Niche library you're unsure about | README + main API examples |
| Implementing a protocol/spec | Key spec section summaries |
| Fixing a bug with a known error code | Official error description + known issues |
| Calling a SaaS API | Official endpoints + param schema |

Template:
```
1. Web-search 1-3 queries (don't over-search)
2. Summarize key info (signatures, imports, gotchas)
3. Paste summary at the start of delegate_to_deepseek(context=...)
4. Delegate
```

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

### Granularity rule: complete logical units, not micro-steps

**Counter-intuitive**: splitting tasks finer ≠ saving more money. Past a
point, finer splits cost *more* than not delegating at all.

5 "anti-delegation taxes" that scale with split count:
1. **Split tax** — your tokens spent deciding how to split + what context to send
2. **Context re-read tax** — DeepSeek can't reuse context across delegations; same files re-read N times
3. **Verification tax** — every DS completion → you sample-read to verify
4. **Startup fee** — v4-pro thinking mode burns ~5-10k reasoning tokens per call just to "warm up"
5. **Fragment rework tax** — micro-tasks lack global view; outputs inconsistent

Rough math (in your-model-equivalent cost, X = doing it yourself):
- Delegate 1 complete unit → ~0.13X (87% savings) ✅
- Split into 5 sub-tasks → ~0.50X (50% savings)
- Split into 10 micro-tasks → ~0.95X (barely any savings)
- Split into 20 fine steps → ~1.88X (**worse than not delegating**)

**Right shape**: delegate "implement this entire feature" or "process all
these files" as one call — let DS run its own 10-30 turn loop internally.

**Wrong shape**: delegate "step 1", verify, delegate "step 2", verify,
delegate "step 3"... — you pay all 5 taxes 3 times.

**Test before splitting further**: "Could I hand this sub-task to a 1-week
new hire with all context up front and have them finish independently?"
- Yes → delegate
- No (they'd need to come back asking questions or check earlier outputs)
  → don't split it out, merge it with the parent unit

### Cost intuition

DeepSeek v4-pro runs thinking mode → every call carries reasoning token
overhead. For small tasks (<5k tokens of work), that overhead can exceed
the work itself. Don't delegate tiny tasks just because you *can*.

Sweet spot: 10–50 files, 50KB–500KB total, mechanical pattern — one delegate
call, not five.
