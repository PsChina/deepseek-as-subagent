"""Stable MCP host instructions, separated to keep the server entrypoint small."""

HOST_INSTRUCTIONS = """
After any result with mutations—or cancellation, disconnection, or restart—call
`get_deepseek_recovery`, verify delegated output and each changed file, then
call `acknowledge_deepseek_mutations(transaction_ids)` with the exact reviewed
IDs.

API reference:
- `ping()`: check that the MCP server is available.
- `delegate_to_deepseek(task, context="")`: wait for full coding to finish; it
  cannot be steered, queried, or cancelled while running.
- `delegate_to_deepseek_readonly(task, context="")`: wait for file-only analysis
  with Read/Glob/Grep; it has no Bash or mutation tools and cannot be controlled
  while running.
- `start_deepseek(task, context="")` / `start_deepseek_readonly(task, context="")`:
  start a coding/read-only background job and return `job_id`.
- `get_deepseek_status(job_id)`: read a background job's state.
- `send_deepseek_message(job_id, message)`: add or correct its task instruction.
- `cancel_deepseek(job_id)`: cancel a background job.
- `get_deepseek_result(job_id)`: return its final result, or not-ready state.
- `get_deepseek_recovery()`: list unacknowledged mutations from coding work.
- `acknowledge_deepseek_mutations(transaction_ids)`: acknowledge exact reviewed IDs.
`task` states the goal and acceptance criteria; optional `context` supplies paths,
constraints, and project conventions. `job_id` comes from `start_*`.

Selection: use `delegate_*` when the host can wait for completion. Use `start_*`
when it needs steering, status, or cancellation. Use readonly only for pure
reading/search/review of existing files or text when no task step needs a command;
use coding for everything else or uncertainty. The API freezes the capability for
the job. Steering cannot enable Bash or mutation tools: if a readonly job later
needs either, cancel or finish it and create a new coding job.

DeepSeek cannot see host chat or project instructions; pass needed context
explicitly. One OS lease permits one execution per canonical workspace.
Background jobs/results are process-local. New delegation fails closed while
recovery records remain.
""".strip()
