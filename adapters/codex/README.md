# Codex Adapter

Use DeepSeek as a **real delegated sub-agent inside Codex CLI**.

Codex remains the main agent: it owns the conversation, planning, judgment, and verification. DeepSeek receives self-contained execution tasks through MCP and runs its own Read / Write / Edit / Bash / Glob / Grep / NotebookEdit loop inside the same workspace.

## Recommended install

From the repository root:

```bash
bash adapters/codex/install.sh
```

The installer:

1. creates/reuses the repo-local Python virtual environment;
2. installs `deepseek-as-subagent` on the compatible MCP Python SDK v1 line;
3. runs an import smoke test;
4. creates `~/.deepseek-mcp/config.json` if needed;
5. replaces any stale `deepseek` MCP registration and registers the current executable with Codex;
6. verifies that Codex can read the resulting MCP registration.

Then edit `~/.deepseek-mcp/config.json` and add your DeepSeek API key if the file still contains the placeholder.

Verify:

```bash
codex mcp list
codex
```

Inside Codex, ask it to call the DeepSeek `ping` tool.

## Delegation guidance

The MCP server publishes host-level delegation instructions during MCP initialization, so current Codex clients can receive a useful default policy without requiring a large copied instruction block.

The most important rules are deliberately front-loaded in the first ~512 characters of the MCP instructions:

- use DeepSeek for self-contained, execution-heavy work;
- decide whether to delegate **before** reading repository source when practical;
- keep architecture, ambiguous root-cause analysis, security-sensitive judgment, and tiny edits in Codex;
- pass all required context explicitly because DeepSeek cannot see Codex chat, `AGENTS.md`, or `CLAUDE.md`;
- verify delegated output and relevant tests before declaring success.

`AGENTS.md` is still useful as an optional stronger/project-specific policy layer. If you want predictable, aggressive automatic delegation in a particular repository, add the relevant policy there. `instructions.md` remains available as a template.

## Two delegation modes

### Simple synchronous delegation

Use `delegate_to_deepseek(task, context)` when the task can run to completion without mid-flight intervention.

```text
Codex
  │ delegate_to_deepseek(...)
  ▼
DeepSeek agent loop
  │ Read / Edit / Bash / ...
  ▼
final result
  │
  ▼
Codex verifies output/tests
```

The MCP call stays open until DeepSeek finishes.

### Steerable background job

For longer work that may need new instructions or cancellation, use the background-job API:

```text
start_deepseek(task, context) -> job_id
send_deepseek_message(job_id, message)
get_deepseek_status(job_id)
cancel_deepseek(job_id)
get_deepseek_result(job_id)
```

`start_deepseek` returns quickly and DeepSeek continues in a background worker. This lets later MCP requests reach the server while the agent is still running.

Steering and cancellation are **cooperative**. They take effect at safe points between model/tool operations; they do not force-interrupt an already in-flight model request or a currently executing tool command.

V1 intentionally permits only **one DeepSeek execution at a time**, shared across synchronous delegation and background jobs, so two agents cannot concurrently modify the same workspace through this server.

## Manual install

### 1. Build the MCP server

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

On Windows, use the equivalent `.venv/Scripts/...` paths.

The project currently pins the MCP Python SDK to the compatible v1 maintenance line (`>=1.28,<2`) because the v2 SDK is a breaking migration and uses a different server API.

### 2. Configure DeepSeek

```bash
mkdir -p ~/.deepseek-mcp
cat > ~/.deepseek-mcp/config.json <<'EOF'
{
  "api_key": "PASTE_YOUR_DEEPSEEK_KEY_HERE",
  "model": "deepseek-v4-pro",
  "max_turns": 50,
  "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "NotebookEdit"]
}
EOF
chmod 600 ~/.deepseek-mcp/config.json
```

Workspace sandboxing auto-follows the directory where Codex launches unless you explicitly set `"workspace"` in the DeepSeek config.

### 3. Register with Codex

```bash
codex mcp add deepseek -- /absolute/path/to/deepseek-as-subagent/.venv/bin/deepseek-mcp
```

Or configure the same command in `~/.codex/config.toml`; see `config.toml.example`.

If you move or re-clone this repository, re-run `bash adapters/codex/install.sh`; it will replace the stale MCP executable path with the current one.

## Network retry behavior

The OpenAI-compatible SDK retry layer is disabled for DeepSeek calls. The project owns one explicit outer retry policy instead, with bounded connect/read/write/pool timeouts. This avoids nested retry amplification in proxy environments where TLS handshakes can time out.

## Disable delegation temporarily

Launch Codex with:

```bash
DEEPSEEK_MODE=off codex
```

The MCP server remains registered, but delegation requests immediately return a disabled response and Codex can continue the task itself.

## Advanced delegation policy

`instructions.md` remains available for users who want an explicit project/global policy in addition to the MCP server's built-in instructions. It is optional rather than required for basic MCP operation, but remains useful when you want stronger and more deterministic project-specific delegation behavior.
