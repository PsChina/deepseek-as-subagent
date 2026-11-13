# Task Contracts

Task Contracts provide a versioned, per-delegation capability boundary. They are
optional: callers that omit \`contract\` retain legacy execution behavior. A
contract can only narrow the administrator's global tool allowlist and the API
execution profile; it cannot grant a capability removed by either layer.

## Example

\`\`\`json
{
  "version": 1,
  "intent": "implement",
  "permissions": {
    "tools": ["Read", "Glob", "Grep", "Write", "Edit"],
    "read_paths": ["src/**", "tests/**"],
    "write_paths": ["src/**", "tests/**"],
    "deny_paths": ["**/generated/**"]
  },
  "constraints": {
    "must_not_change": ["Public API behavior"],
    "success_checks": ["python -m unittest discover -s tests -v"],
    "expected_outputs": ["changed_files", "test_results", "risks"]
  },
  "limits": {
    "max_tool_calls": 40,
    "max_mutation_bytes": 10485760
  }
}
\`\`\`

Pass the object as the optional \`contract\` argument to
\`delegate_to_deepseek\`, \`delegate_to_deepseek_readonly\`,
\`start_deepseek\`, or \`start_deepseek_readonly\`.

## Enforcement model

The effective tool set is the intersection of the administrator's
\`config.json\` \`allowed_tools\`, the immutable API execution profile, and the
contract's optional \`permissions.tools\` list. The host compiles this into an
immutable \`EffectivePolicy\`, passes the same snapshot across the isolated
tool-process boundary, and checks it again at dispatch time.

- \`read_paths\` limits Read, Glob, and Grep results.
- \`write_paths\` limits Write, Edit, and NotebookEdit. Mutation tools are
  removed when no write paths are declared.
- \`deny_paths\` always includes built-in credential and secret patterns.
  User-provided patterns can add restrictions but cannot remove built-ins.
- Resource limits can lower, never raise, process-wide hard limits.
- Patterns are workspace-relative, slash-separated globs. \`**\` matches zero
  or more path segments. Absolute paths, parent traversal, and backslashes
  are rejected.

## Current limitations

Bash is intentionally unavailable whenever a Task Contract is supplied. A
shell command can bypass file-tool path scopes; exposing Bash before a real
command allowlist or OS-level sandbox exists would make the contract misleading.
Calls without a contract retain existing Bash behavior.

\`must_not_change\` and \`success_checks\` are supplied to the model as task
instructions. They are not proof of compliance. Independent verification and
structured handoff are follow-on work; a model's final text is not test evidence.
