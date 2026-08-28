# Codex instructions for DeepSeek delegation

Use this file as an optional stronger project/global policy in `AGENTS.md` or Codex instructions. The MCP server already publishes a compact default policy during initialization.

---

## Using DeepSeek as a delegated sub-agent

You have access to DeepSeek through the `deepseek` MCP server. DeepSeek runs its
own agent loop inside the configured workspace.

### API reference and selection

- `ping()` — check server availability.
- `delegate_to_deepseek(task, context="")` — wait for full coding to finish;
  it cannot be steered, queried, or cancelled while running.
- `delegate_to_deepseek_readonly(task, context="")` — wait for Read/Glob/Grep
  analysis only; no Bash or workspace mutation, and no background controls.
- `start_deepseek(task, context="")` / `start_deepseek_readonly(task, context="")`
  — start coding/read-only background work and return `job_id`.
- `get_deepseek_status(job_id)` / `get_deepseek_result(job_id)` — read job state
  or final result.
- `send_deepseek_message(job_id, message)` / `cancel_deepseek(job_id)` — steer
  or cancel a background job.
- `get_deepseek_recovery()` / `acknowledge_deepseek_mutations(transaction_ids)`
  — inspect and acknowledge exact reviewed coding-mutation records.

`task` gives the goal and acceptance criteria; optional `context` gives paths,
constraints, and conventions. `job_id` comes from `start_*`; `message` is an
additional instruction; `transaction_ids` is the exact recovery ID list.

Use `delegate_*` when the host can wait for completion. Use `start_*` when it
needs steering, status, or cancellation. Use readonly only for pure
reading/search/review of existing files or text with no command at any step;
use coding for everything else or uncertainty. Steering cannot change frozen
capabilities: a readonly job that later needs a command or mutation must be
cancelled or finished, then replaced with a new coding job.

One DeepSeek execution may run per canonical workspace across both API types and
across MCP server processes. Background jobs and their IDs are scoped to the
current MCP session.

Every file mutation is durably journaled before commit. After any result that
contains mutations, call `get_deepseek_recovery`, verify each reported file,
then pass the exact transaction IDs to
`acknowledge_deepseek_mutations(transaction_ids)`. Do the same after
cancellation, disconnection, or MCP restart before retrying. A new delegation
fails closed while unacknowledged records remain; recovery query/ack does not
require a working provider API key.

Steering is applied at safe points between model/tool operations. Cancellation wakes retry backoff and promptly terminates an in-flight provider or local-tool subprocess. If a newer steering instruction arrives before a planned tool call executes, stale remaining tool calls may be skipped and DeepSeek will re-plan from the latest instruction.

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

Allowed lightweight discovery by the **main agent** before deciding (this is
not a capability of the readonly DeepSeek API):
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
- busy response: another DeepSeek execution owns this workspace lease; inspect/finish/cancel it before starting another against the same workspace.
- recovery-required response: do not retry; query recovery, verify the actual files, and acknowledge only the exact reviewed IDs.

### Granularity rule

Delegate complete logical units rather than a chain of tiny steps. Every extra delegation repeats context loading, startup, and verification costs. A useful test is: could a competent new engineer finish this unit independently if given all context up front? If yes, it is a good delegation unit.
