# Codex instructions for DeepSeek delegation

Use this file as an optional stronger project/global policy in `AGENTS.md` or Codex instructions. The MCP server already publishes a compact default policy during initialization.

---

## Using DeepSeek as a delegated sub-agent

You have access to DeepSeek through the `deepseek` MCP server. DeepSeek runs its own agent loop inside the workspace with Read / Write / Edit / Bash / Glob / Grep / NotebookEdit tools.

### Choose the delegation mode

Use the simple synchronous tool when the task can safely run to completion without mid-flight intervention:

```text
delegate_to_deepseek(task, context)
```

Use the steerable background-job API when the work is longer, exploratory, or may need new instructions/cancellation while running:

```text
start_deepseek(task, context) -> job_id
send_deepseek_message(job_id, message)
get_deepseek_status(job_id)
cancel_deepseek(job_id)
get_deepseek_result(job_id)
```

V1 permits one DeepSeek execution at a time across both modes.

Steering/cancellation is cooperative. It takes effect at safe points between model/tool operations rather than force-killing an in-flight model request or tool command. If a newer steering instruction arrives before a planned tool call executes, stale remaining tool calls may be skipped and DeepSeek will re-plan from the latest instruction.

### Core principle: delegate execution-heavy complete units

DeepSeek is best used for self-contained implementation and mechanical execution. Keep architecture, ambiguous root-cause analysis, security-sensitive judgment, and tiny edits in the main agent unless the user explicitly asks otherwise.

Typical fits:
- multi-file implementation with clear acceptance criteria
- batch refactors / renames / migrations
- test generation and test-gap filling
- i18n extraction / translation / ETL / log processing
- boilerplate / CRUD / protocol conversion
- repetitive repository maintenance

### Decide before reading large amounts of source

The delegation decision should happen before the main agent reads large amounts of repository content. Otherwise the main agent and DeepSeek both pay the same reading cost.

Allowed lightweight discovery before deciding:
- directory/file listing
- file counts / sizes
- path discovery
- read-only shell commands such as `ls`, `find`, `wc`, `du`, `git status`
- web search for external documentation

If the task requires deep project reading just to decide whether to delegate, keep it in the main agent.

### Pass complete context

DeepSeek cannot see the parent conversation, `AGENTS.md`, `CLAUDE.md`, or other host-only context unless it is explicitly included in `task` or `context`.

Include:
- relevant paths
- desired outcome
- constraints / boundaries
- project conventions that matter
- success criteria
- external API/spec facts already gathered by the host

### Verify every delegation

DeepSeek's completion message is not proof of correctness. The main agent owns verification.

After completion:
1. inspect representative changed files;
2. run relevant tests/checks;
3. verify counts/schema when the task is batch-oriented;
4. fix small issues locally;
5. re-delegate only when the remaining work is still a coherent independent unit.

### Steering guidance

Use `send_deepseek_message` for genuine changes in direction, newly discovered constraints, or corrections while a background job is still running. Do not spam the job with micro-instructions; each steering message should be meaningful enough to change subsequent work.

Examples:
- "Stop adding new files; modify only the existing adapter files."
- "The API is version 3, not version 2. Use the following signature..."
- "Keep the implementation but replace the polling loop with event-driven logic."

Use `cancel_deepseek` when continuing the current job would be wasteful or unsafe.

### Failure handling

- configuration/API failure: retry once when clearly transient, otherwise take over in the main agent;
- max-turns: split into larger independent logical units, not micro-steps;
- poor output twice: stop delegating that task and take over;
- busy response: another DeepSeek execution owns the V1 execution slot; inspect/finish/cancel it before starting another.

### Granularity rule

Delegate complete logical units rather than a chain of tiny steps. Every extra delegation repeats context loading, startup, and verification costs. A useful test is: could a competent new engineer finish this unit independently if given all context up front? If yes, it is a good delegation unit.
