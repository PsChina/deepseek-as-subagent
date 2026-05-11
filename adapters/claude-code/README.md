# Claude Code Adapter

This is the **default** adapter — the root `install.sh` installs it
automatically. This README documents what gets installed.

## What it adds to Claude Code

| File | Purpose |
|---|---|
| `~/.claude.json` entry `mcpServers.deepseek` | MCP server registration |
| `~/.claude/skills/delegate-to-deepseek/` (symlink) | Teaches Claude when to delegate |
| `~/.claude/commands/ds.md` (symlink) | `/ds <task>` slash command for explicit delegation |
| `~/.zshrc` `pure` alias | One-shot start with delegation disabled |
| `~/.deepseek-mcp/config.json` | DeepSeek API key + model settings (workspace auto-follows `claude` cwd) |

The skill and command are **symlinks** to files in the repo — editing the repo
files takes effect immediately, no reinstall.

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
your `~/.deepseek-mcp/config.json` (preserves your API key) or
`~/.zshrc` alias (you remove that manually if you want).

## Per-feature control

| Feature | How to disable |
|---|---|
| Auto-delegation in main conversation | Tell Claude "do it yourself, don't delegate" |
| Whole session, no DeepSeek | Start with `pure` (alias from `~/.zshrc`) |
| Permanently | `./uninstall.sh` |

## Files in this adapter (relative to repo root)

- `../../skills/delegate-to-deepseek/SKILL.md` — delegation decision rules
- `../../commands/ds.md` — `/ds` slash command
- `../../install.sh` — installer that wires this adapter into `~/.claude/`
- `../../uninstall.sh` — removes everything this adapter added
