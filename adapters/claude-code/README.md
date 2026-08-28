# Claude Code Adapter

This is the **default** adapter — the root `install.sh` installs it
automatically. This README documents what gets installed.

## What it adds to Claude Code

| File | Purpose |
|---|---|
| `~/.claude.json` entry `mcpServers.deepseek` | MCP server registration |
| `~/.claude/skills/delegate-to-deepseek/` (symlink) | Teaches Claude when to delegate |
| `~/.claude/commands/ds.md` (symlink) | `/ds <task>` slash command for explicit delegation |
| `~/.deepseek-mcp/config.json` | DeepSeek API key + model settings (workspace auto-follows `claude` cwd) |

The skill and command symlinks target installer-owned copies under
`~/.deepseek-mcp/claude-helpers/`, so moving or deleting the checkout does not
break an installed Claude setup. Re-run the installer to publish repo updates.
The installer never edits shell startup files.

## Delegation capabilities

Use `delegate_to_deepseek` or `start_deepseek` for coding: they always expose
Read, Write, Edit, Bash, Glob, Grep, and NotebookEdit. Bash runs on the trusted
host with `cwd=workspace`; it is bounded and credential-isolated, but not an OS
sandbox.

Use `delegate_to_deepseek_readonly` or `start_deepseek_readonly` for static
file analysis. They always expose only Read, Glob, and Grep—no Bash and no
workspace mutation. Both `start_*` APIs return a `job_id` controlled by the
same `send_deepseek_message`, status, cancel, and result APIs. The selected API
fixes its capability for the whole job, including after steering.

## Install

From repo root:
```bash
./install.sh
```

Idempotent — safe to re-run after pulling repo updates.

## Uninstall

```bash
./uninstall.sh
```

Removes the MCP registration, skill, and slash command. Does **not** touch
your `~/.deepseek-mcp/config.json` (preserves your API key). An alias left by an
older release must be removed manually from your shell startup file.

## Per-feature control

| Feature | How to disable |
|---|---|
| Auto-delegation in main conversation | Tell Claude "do it yourself, don't delegate" |
| Whole session, no DeepSeek | Run `DEEPSEEK_MODE=off claude` |
| Permanently | `./uninstall.sh` |

## Files in this adapter (relative to repo root)

- `../../skills/delegate-to-deepseek/SKILL.md` — delegation decision rules
- `../../commands/ds.md` — `/ds` slash command
- `../../install.sh` — installer that wires this adapter into `~/.claude/`
- `../../uninstall.sh` — removes everything this adapter added
