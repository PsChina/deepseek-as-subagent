"""Stable MCP host instructions, separated to keep the server entrypoint small."""

HOST_INSTRUCTIONS = """
After a result containing mutations, verify delegated output and each changed
file, call `get_deepseek_recovery`, then call `acknowledge_deepseek_mutations` with its exact IDs. Use `delegate_to_deepseek` or
`start_deepseek` for code changes, builds, tests, lint, git, dependency work, or
anything that may write the workspace. They have full coding tools and
trusted-host Bash. For reading code, search, code review, log analysis,
root-cause investigation, or other non-mutating work, use
`delegate_to_deepseek_readonly` or `start_deepseek_readonly`. They have only
Read/Glob/Grep and need neither Bash nor a container runtime. Use coding APIs
for any Bash command, test, build, lint, Git command, program run, or possible
workspace write. Do not pass a mode or backend; the API fixes capabilities
before each task starts.

Every mutation is journaled before commit.
After cancellation, disconnection, or restart, query recovery before retrying.
Keep architecture, ambiguous root-cause analysis, security judgment, and tiny
edits in the host. DeepSeek cannot see host chat or project instructions; pass
context explicitly. Do not retry a denied capability.

For steering or cancellation, use `send_deepseek_message`, `get_deepseek_status`,
`cancel_deepseek`, and `get_deepseek_result`. Steering is consumed between
operations; cancellation terminates provider/tool subprocesses. One OS lease
permits one execution per canonical workspace. Background jobs/results are
process-local. New delegation fails closed while recovery records remain.
""".strip()
