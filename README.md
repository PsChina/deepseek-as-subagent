# deepseek-as-subagent

> Run DeepSeek as a **real sub-agent** inside Claude Code — not just an LLM endpoint.
> Claude stays on the main loop (your Max OAuth, your context, your judgment).
> DeepSeek gets its own Read / Edit / Bash agent loop for the batch / mechanical work.

```
       Claude (your main agent, Max OAuth, expensive but smart)
         │
         │  decides this is a batch task
         │  →  delegate_to_deepseek(task, context)
         ▼
       DeepSeek sub-agent (cheap, runs its own loop inside the workspace)
         │  Read / Write / Edit / Bash / Glob / Grep — all local
         │  iterates until done
         ▼
       Final message bubbles back to Claude
       Claude verifies a sample of the output, reports to you
```

## Quick start

```bash
curl -sSL https://raw.githubusercontent.com/PsChina/deepseek-as-subagent/main/curl-install.sh | bash
```

One line. Clones the repo to `~/.local/share/deepseek-as-subagent`, installs
the Python package in an isolated venv, registers the MCP server with Claude
Code, deploys the skill + `/ds` slash command, and adds a `pure` shell alias.

After install, edit `~/.deepseek-mcp/config.json` to paste your DeepSeek API
key (get one at [platform.deepseek.com](https://platform.deepseek.com)). Then
run `claude` and try `/ds write a python hello world to /tmp/hi.py`.

Re-run the same `curl | bash` later to upgrade. For other clients (Codex,
Cursor, Cline) or manual install, see [Install](#install) below.

## How is this different from existing DeepSeek MCP servers?

Most `deepseek-mcp-server` projects expose DeepSeek as a **single LLM call** (`create_chat_completion`, `create_anthropic_message`). Claude has to read every file itself and feed content into the prompt — DeepSeek only saves you the "thinking" cost, not the "reading/writing" cost. Same for [Composio's DeepSeek toolkit](https://composio.dev/toolkits/deepseek/framework/claude-code).

This project gives DeepSeek **its own full agent loop**: tool dispatch, file I/O, command execution, multi-turn reasoning — all inside a sandboxed workspace. Claude hands off the entire task and gets a single summary back. The token savings are end-to-end.

## What's in the box

- **MCP server** (Python, stdio transport) exposing one real tool: `delegate_to_deepseek(task, context)`
- **Local agent loop** for DeepSeek (`agent_loop.py`) with OpenAI-compatible function calling
- **6 sandboxed tools** for DeepSeek to use: Read / Write / Edit / Bash / Glob / Grep
- **Path sandbox + command blacklist** (`safety.py`) — DeepSeek can't escape your workspace or run `rm -rf /`
- **Skill + slash command** so Claude knows *when* to delegate (and when not to)
- **`pure` shell alias** for one-shot "no DeepSeek today" runs

## Install

### Claude Code (default)

```bash
git clone https://github.com/PsChina/deepseek-as-subagent
cd deepseek-as-subagent
./install.sh
```

Then edit `~/.deepseek-mcp/config.json` and paste your DeepSeek API key.

Get a key at https://platform.deepseek.com (CNY ¥20 lasts a long time).

### Codex CLI

See [adapters/codex/](adapters/codex/README.md) — registers the MCP server
with `codex` and provides instructions you can paste into `AGENTS.md`.

### Cursor / Cline / Claude Desktop / other MCP clients

The MCP server itself is client-agnostic. After `pip install -e .`, point
your client's MCP config at `<repo>/.venv/bin/deepseek-mcp`. For
client-specific "when to delegate" hints, see [adapters/](adapters/) —
PRs welcome for new clients.

## Usage

After install, just use `claude` normally. The plugin adds:

- `delegate_to_deepseek` — Claude auto-invokes it when the task fits (see `skills/delegate-to-deepseek/SKILL.md`)
- `/ds <task>` — force delegation, skip Claude's own judgment
- `pure` shell alias — start Claude with DeepSeek disabled for this session

## When delegation actually saves money

**Critical rule** (encoded in the skill): the delegation decision must happen **before Claude reads any source files**. If Claude reads first then delegates, both Claude and DeepSeek read the same files — net cost goes up, not down.

The skill enforces: only `Glob` / `LS` / read-only `Bash` are allowed before the delegation decision. If you can't decide without reading, you shouldn't delegate.

Sweet spot for delegation:
- ✅ 10–50 files, mechanical pattern (i18n extract, batch refactor, ETL)
- ✅ Large data + simple processing (log scan, file conversion)
- ❌ Single file < 500 lines (DeepSeek thinking-token overhead > savings)
- ❌ Cross-file design / architectural judgment
- ❌ Tasks needing project-specific idioms from `CLAUDE.md`

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Claude Code (CLI or VSCode extension, your Max OAuth)          │
│    ↓ stdio (MCP protocol, no network)                           │
│  deepseek-as-subagent (this project, Python subprocess)         │
│    ↓ HTTPS                                                       │
│  api.deepseek.com (your API key, paid per token)                │
└─────────────────────────────────────────────────────────────────┘

Everything except the actual DeepSeek API call stays on your machine.
No third-party proxy. No cloud relay. Your code never leaves your laptop.
```

## Configuration

`~/.deepseek-mcp/config.json`:

```json
{
  "api_key": "sk-...",
  "model": "deepseek-v4-pro",
  "max_turns": 50,
  "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
}
```

**Workspace (sandbox root)** auto-follows the directory where you launch
`claude` — DeepSeek shares the same scope as Claude itself, no manual config
needed. To lock the sandbox to a fixed path regardless of cwd, add
`"workspace": "/abs/path"` to the config.

Override at runtime with env vars: `DEEPSEEK_API_KEY`, `DEEPSEEK_WORKSPACE`, `DEEPSEEK_MODE=off`.

## Uninstall

```bash
./uninstall.sh
```

Removes the MCP registration, skill, and slash command. Doesn't touch your Claude Code, your Max OAuth, or your projects.

## License

MIT
