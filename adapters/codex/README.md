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
2. installs `deepseek-as-subagent`;
3. creates `~/.deepseek-mcp/config.json` if needed;
4. registers the `deepseek` MCP server with Codex.

Then edit `~/.deepseek-mcp/config.json` and add your DeepSeek API key if the file still contains the placeholder.

Verify:

```bash
codex mcp list
codex
```

Inside Codex, ask it to call the DeepSeek `ping` tool.

## No AGENTS.md copy/paste required

The MCP server now publishes host-level delegation instructions during MCP initialization. Modern Codex clients receive the policy automatically when they connect to the server.

That policy tells Codex to:

- delegate self-contained implementation, batch editing, mechanical refactors, test generation, and other execution-heavy work;
- keep architecture, ambiguous root-cause analysis, security-sensitive judgment, and tiny edits in Codex;
- decide whether to delegate **before** reading large amounts of repository source;
- send DeepSeek a complete task + context because DeepSeek cannot see Codex conversation history;
- verify representative output and relevant tests after delegation.

You can still add project-specific rules to `AGENTS.md`. Use `instructions.md` as an optional, more aggressive policy template if you want Codex to delegate more often than the built-in default.

## Manual install

### 1. Build the MCP server

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

On Windows, use the equivalent `.venv/Scripts/...` paths.

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

## How the flow works

```text
Codex (main agent)
  │
  │ decides a task is a good delegation unit
  ▼
delegate_to_deepseek(task, context)
  │
  ▼
DeepSeek sub-agent
  │ Read / Write / Edit / Bash / Glob / Grep / NotebookEdit
  │ owns its own multi-turn loop
  ▼
summary + usage metadata
  │
  ▼
Codex verifies output/tests and continues the main task
```

## Disable delegation temporarily

Launch Codex with:

```bash
DEEPSEEK_MODE=off codex
```

The MCP server remains registered, but delegation requests immediately return a disabled response and Codex can continue the task itself.

## Advanced delegation policy

`instructions.md` remains available for users who want an explicit project/global policy in addition to the MCP server's built-in instructions. It is optional rather than required.
