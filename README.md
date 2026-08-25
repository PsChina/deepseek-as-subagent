# deepseek-as-subagent

**English** · [简体中文](README.zh-CN.md)

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/PsChina/deepseek-as-subagent?style=social)](https://github.com/PsChina/deepseek-as-subagent)
[![Glama MCP server](https://glama.ai/mcp/servers/PsChina/deepseek-as-subagent/badges/score.svg)](https://glama.ai/mcp/servers/PsChina/deepseek-as-subagent)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)](https://modelcontextprotocol.io/)
[![Mentioned in Awesome MCP Servers](https://awesome.re/mentioned-badge.svg)](https://github.com/punkpeye/awesome-mcp-servers)
[![Platforms](https://img.shields.io/badge/platforms-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)](https://github.com/PsChina/deepseek-as-subagent)

> Run DeepSeek as a **real sub-agent** inside Claude Code / Codex CLI — not just an LLM endpoint.
> The host agent keeps the main conversation, planning, judgment, and verification.
> DeepSeek gets its own Read / Write / Edit / Bash / Glob / Grep / NotebookEdit agent loop for execution-heavy work.

```text
       Claude / Codex (main agent)
         │
         ├─ simple task → delegate_to_deepseek(task, context)
         │
         └─ steerable task → start_deepseek(task, context) → job_id
                                │
                                ├─ send_deepseek_message(job_id, ...)
                                ├─ get_deepseek_status(job_id)
                                ├─ cancel_deepseek(job_id)
                                └─ get_deepseek_result(job_id)
         ▼
       DeepSeek sub-agent
         │  Read / Write / Edit / Bash / Glob / Grep / NotebookEdit — all local
         │  iterates autonomously inside the workspace
         ▼
       Result returns to the host
       Host verifies representative output / tests
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

Re-run the same `curl | bash` later to upgrade. For Codex or other MCP clients,
see [Install](#install) below.

## How is this different from existing DeepSeek MCP servers?

Most `deepseek-mcp-server` projects expose DeepSeek as a **single LLM call** (`create_chat_completion`, `create_anthropic_message`). The host has to read every file itself and feed content into the prompt — DeepSeek only saves the "thinking" cost, not the "reading/writing" cost.

This project gives DeepSeek **its own full agent loop**: tool dispatch, file I/O, command execution, and multi-turn reasoning inside a sandboxed workspace. The host hands off a complete logical unit and gets a result back. Token savings are end-to-end.

## What's in the box

- **MCP server** (Python, stdio transport)
- **Simple synchronous delegation**: `delegate_to_deepseek(task, context)`
- **Steerable background jobs**: `start_deepseek`, `send_deepseek_message`, `get_deepseek_status`, `cancel_deepseek`, `get_deepseek_result`
- **Local DeepSeek agent loop** (`agent_loop.py`) with OpenAI-compatible function calling
- **7 sandboxed tools**: Read / Write / Edit / Bash / Glob / Grep / NotebookEdit
- **Path sandbox + command blacklist** (`safety.py`)
- **Single-execution V1 guard** so synchronous/background DeepSeek runs cannot concurrently modify the same workspace through one server
- **Explicit network retry policy** with OpenAI SDK internal retries disabled to avoid nested retry amplification in proxy/TLS-timeout environments
- **Claude Code skill + `/ds` command** for delegation policy and forced delegation

## Install

### Claude Code (default)

```bash
git clone https://github.com/PsChina/deepseek-as-subagent
cd deepseek-as-subagent
./install.sh
```

Then edit `~/.deepseek-mcp/config.json` and paste your DeepSeek API key.

### Codex CLI

```bash
git clone https://github.com/PsChina/deepseek-as-subagent
cd deepseek-as-subagent
bash adapters/codex/install.sh
```

See [adapters/codex/README.md](adapters/codex/README.md) for the Codex-specific install, delegation policy, and background-job workflow.

### Cursor / Cline / Claude Desktop / other MCP clients

The MCP server itself is client-agnostic. After `pip install -e .`, point your client's MCP config at `<repo>/.venv/bin/deepseek-mcp`.

## Usage

### Simple delegation

Use `delegate_to_deepseek(task, context)` when the task can run to completion without mid-flight intervention. The MCP request remains open until DeepSeek finishes.

### Steerable background delegation

For longer tasks that may need new instructions or cancellation:

```text
start_deepseek(task, context) -> job_id
send_deepseek_message(job_id, message)
get_deepseek_status(job_id)
cancel_deepseek(job_id)
get_deepseek_result(job_id)
```

`start_deepseek` returns quickly while the DeepSeek agent continues in a background worker. Steering and cancellation are cooperative: they take effect at safe points between model/tool operations and do not force-interrupt an already in-flight model request or currently executing tool command.

If a steering message arrives after DeepSeek has planned tool calls but before a not-yet-executed tool runs, the stale tool call is skipped and DeepSeek re-plans from the new parent instruction.

V1 intentionally permits only **one DeepSeek execution at a time** across synchronous and background APIs.

### Claude Code helpers

- `delegate_to_deepseek` — Claude auto-invokes it when the task fits
- `/ds <task>` — force synchronous delegation
- `pure` shell alias — start Claude with DeepSeek disabled for that session

## When delegation actually saves money

The delegation decision should happen **before the host reads large amounts of source**. If the host reads first and then delegates, both agents pay the repository-reading cost.

Sweet spot:
- ✅ Multi-file implementation / mechanical refactors / test generation
- ✅ Large data + simple processing (log scan, file conversion, ETL)
- ✅ Tasks that may benefit from a cheap independent execution loop
- ❌ Tiny edits where orchestration overhead dominates
- ❌ Cross-domain architecture / ambiguous root-cause analysis / security-sensitive judgment

## Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│  Claude Code / Codex CLI (main agent)                           │
│    ↓ stdio (MCP protocol, local)                                │
│  deepseek-as-subagent (Python MCP process)                      │
│    ├─ synchronous delegate                                      │
│    └─ steerable background job manager                          │
│         ↓                                                       │
│       DeepSeek agent loop + local sandbox tools                  │
│    ↓ HTTPS                                                      │
│  api.deepseek.com                                               │
└─────────────────────────────────────────────────────────────────┘
```

Everything except the actual DeepSeek API call stays on your machine. No third-party proxy or cloud relay is introduced by this project.

## Configuration

`~/.deepseek-mcp/config.json`:

```json
{
  "api_key": "sk-...",
  "model": "deepseek-v4-pro",
  "max_turns": 50,
  "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "NotebookEdit"]
}
```

**Workspace (sandbox root)** auto-follows the directory where you launch the host client. To lock the sandbox to a fixed path regardless of cwd, add `"workspace": "/abs/path"` to the config.

Override at runtime with env vars: `DEEPSEEK_API_KEY`, `DEEPSEEK_WORKSPACE`, `DEEPSEEK_MODE=off`.

## Uninstall

```bash
./uninstall.sh
```

Removes the Claude Code MCP registration, skill, and slash command. It does not touch your projects or DeepSeek API account.

## License

MIT
