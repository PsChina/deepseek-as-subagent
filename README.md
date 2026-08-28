# deepseek-as-subagent

**English** · [简体中文](README.zh-CN.md)

[![Python](https://img.shields.io/badge/python-3.10--3.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/PsChina/deepseek-as-subagent?style=social)](https://github.com/PsChina/deepseek-as-subagent)
[![Glama MCP server](https://glama.ai/mcp/servers/PsChina/deepseek-as-subagent/badges/score.svg)](https://glama.ai/mcp/servers/PsChina/deepseek-as-subagent)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)](https://modelcontextprotocol.io/)
[![Mentioned in Awesome MCP Servers](https://awesome.re/mentioned-badge.svg)](https://github.com/punkpeye/awesome-mcp-servers)
[![Platforms](https://img.shields.io/badge/platforms-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)](https://github.com/PsChina/deepseek-as-subagent)

> Run DeepSeek as a **real sub-agent** inside Claude Code / Codex CLI — not just an LLM endpoint.
> The host agent keeps the main conversation, planning, judgment, and verification.
> DeepSeek gets its own workspace-scoped agent loop for execution-heavy work.
> Coding APIs use workspace-scoped writes and bounded trusted-host Bash; separate read-only APIs provide pure file analysis without command execution.

```text
       Claude / Codex (main agent)
         │
         ├─ coding → delegate_to_deepseek / start_deepseek
         │
         └─ read-only → delegate_to_deepseek_readonly / start_deepseek_readonly
                                │
                                ├─ send_deepseek_message(job_id, ...)
                                ├─ get_deepseek_status(job_id)
                                ├─ cancel_deepseek(job_id)
                                └─ get_deepseek_result(job_id)
         ▼
       DeepSeek sub-agent
        │  coding: Read / Write / Edit / Bash / Glob / Grep / NotebookEdit
        │  read-only: Read / Glob / Grep
         │  iterates autonomously inside the workspace
         ▼
       Result returns to the host
       Host verifies representative output / tests
```

## Quick start

```bash
git clone https://github.com/PsChina/deepseek-as-subagent.git
cd deepseek-as-subagent
git checkout REVIEWED_TAG_OR_COMMIT
# Inspect install.sh and requirements.lock, then:
./install.sh
```

Python 3.10–3.12 must already be installed. The installer never pipes a remote
bootstrap script into a shell. It installs the exact, hash-verified dependency
set in `requirements.lock`, registers the MCP server with Claude Code, deploys
protected generation copies of the skill + `/ds` slash command. It does not
modify shell startup files. Helper deployment is best-effort after the core MCP
registration commits; a foreign destination is preserved and reported.

After install, edit `~/.deepseek-mcp/config.json` to paste your DeepSeek API
key on POSIX, or set `DEEPSEEK_API_KEY` on Windows (get one at
[platform.deepseek.com](https://platform.deepseek.com)). Then
run `claude` and try `/ds inspect this workspace and summarize its structure`.

To upgrade, fetch and inspect an explicit tag or commit, then re-run the local
installer. Coding always uses `trusted_host`; read-only APIs need neither Bash
nor Docker/Podman. For
Codex or other MCP clients, see [Install](#install) below.

## How is this different from existing DeepSeek MCP servers?

Most `deepseek-mcp-server` projects expose DeepSeek as a **single LLM call** (`create_chat_completion`, `create_anthropic_message`). The host has to read every file itself and feed content into the prompt — DeepSeek only saves the "thinking" cost, not the "reading/writing" cost.

This project gives DeepSeek **its own full agent loop**: tool dispatch, file I/O, command execution, and multi-turn reasoning inside a sandboxed workspace. The host hands off a complete logical unit and gets a result back. Token savings are end-to-end.

## What's in the box

- **MCP server** (Python, stdio transport)
- **Coding and read-only delegation**: `delegate_to_deepseek` / `delegate_to_deepseek_readonly`
- **Steerable background jobs**: `start_deepseek` / `start_deepseek_readonly` plus shared controls
- **Local DeepSeek agent loop** (`agent_loop.py`) with OpenAI-compatible function calling
- **Coding-ready tools**: Read / Write / Edit / Bash / Glob / Grep / NotebookEdit by default
- **Bash execution**: bounded credential-isolated trusted-host commands through the tool-child boundary
- **Workspace path boundary** for file tools, with outbound symlinks rejected
- **Cross-process execution lease** so two MCP servers cannot run DeepSeek concurrently against the same workspace
- **Crash-safe mutation journal** with recovery query, file verification, and exact acknowledgement before another delegation
- **Explicit network retry policy** with OpenAI SDK internal retries disabled to avoid nested retry amplification in proxy/TLS-timeout environments
- **Claude Code skill + `/ds` command** for delegation policy and forced delegation

## Compatibility

The existing MCP entry points `ping()` and
`delegate_to_deepseek(task, context="")` keep their input schema; the
background-job and recovery tools are additive. This is input-schema compatible,
but mutation-capable legacy hosts must adopt the additive recovery query/verify/ack
handshake before starting another delegation; read-only use needs no change.
Clients should not parse health/error text byte-for-byte because diagnostics are now more specific. Provider calls still
use DeepSeek's [OpenAI-compatible Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/).
Local Python module signatures are implementation details rather than a stable
public API.

## Install

### Claude Code (default)

```bash
git clone https://github.com/PsChina/deepseek-as-subagent
cd deepseek-as-subagent
./install.sh
```

Then edit `~/.deepseek-mcp/config.json` on POSIX, or set
`DEEPSEEK_API_KEY` on Windows.

### Codex CLI

```bash
git clone https://github.com/PsChina/deepseek-as-subagent
cd deepseek-as-subagent
bash adapters/codex/install.sh
```

See [adapters/codex/README.md](adapters/codex/README.md) for the Codex-specific install, delegation policy, and background-job workflow.

The Claude and Codex installers build a fresh isolated runtime, validate its
configuration and MCP protocol, and only then switch the host registration.
They keep the active generation plus one previous generation for recovery. Any
manual runtime must stay outside a delegated workspace when file-mutation tools
are enabled; unsafe layouts are rejected at startup.
Both installers serialize install/uninstall transactions. A hard-killed
installer intentionally leaves an empty fail-closed lock that must be removed
only after confirming no installer is running.

### Cursor / Cline / Claude Desktop / other MCP clients

The MCP server itself is client-agnostic. Install `requirements.lock` with
`pip --require-hashes`, install this project with dependency resolution disabled,
then point your client's MCP config at the generated `deepseek-mcp` entrypoint.

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

`start_deepseek` returns quickly while the DeepSeek agent continues in a background worker. Steering takes effect at safe points between model/tool operations. Cancellation wakes retry backoff and promptly terminates an in-flight provider or local-tool subprocess.

If a steering message arrives after DeepSeek has planned tool calls but before a not-yet-executed tool runs, the stale tool call is skipped and DeepSeek re-plans from the new parent instruction.

Only **one DeepSeek execution per canonical workspace** may run at a time, including executions started by separate MCP server processes. Background job IDs and results are session-scoped; collect the result before closing the host session.

### Mutation recovery

Every file mutation is journaled before commit. After a result reports
mutations—or after cancellation, disconnection, or MCP restart—run:

```text
get_deepseek_recovery()
# verify every reported file
acknowledge_deepseek_mutations(transaction_ids)
```

New delegation fails closed until the exact reviewed IDs are acknowledged.
Recovery works without a valid DeepSeek API credential and never deletes or
rolls back workspace files.

### Claude Code helpers

- `delegate_to_deepseek` — Claude auto-invokes it when the task fits
- `/ds <task>` — force synchronous delegation
- `DEEPSEEK_MODE=off claude` — start one session with DeepSeek disabled

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
│       DeepSeek agent loop + workspace-scoped tools              │
│    ↓ HTTPS                                                      │
│  api.deepseek.com                                               │
└─────────────────────────────────────────────────────────────────┘
```

No third-party proxy or cloud relay is introduced by this project. Delegated prompts and tool/file outputs selected by the agent are sent to the configured DeepSeek-compatible API, so only delegate data that endpoint is permitted to receive.

## Configuration

`~/.deepseek-mcp/config.json`:

```json
{
  "api_key": "sk-...",
  "model": "deepseek-v4-pro",
  "max_turns": 50,
  "max_run_seconds": 18000,
  "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "NotebookEdit"]
}
```

`max_run_seconds` is the wall-clock limit for one delegated run. Its default is
18,000 seconds (5 hours), it may be increased explicitly, and its absolute
accepted maximum is 172,800 seconds (48 hours). Individual provider requests
remain bounded to 180 seconds within that run budget.
For synchronous delegation, the MCP client's tool timeout must be at least the
configured run limit plus cleanup grace; Codex installs with an 18,060-second
default (five hours plus 60 seconds).

**Workspace (sandbox root)** auto-follows the directory where you launch the host client. To lock the sandbox to a fixed path regardless of cwd, add `"workspace": "/abs/path"` to the config.

`delegate_to_deepseek` and `start_deepseek` always use full coding tools and
bounded `trusted_host` Bash. `delegate_to_deepseek_readonly` and
`start_deepseek_readonly` always use only Read/Glob/Grep and never expose Bash.
See [SECURITY.md](SECURITY.md) for boundaries and platform limitations.

Override at runtime with env vars: `DEEPSEEK_API_KEY`, `DEEPSEEK_WORKSPACE`, `DEEPSEEK_MODE=off`.

## Uninstall

Claude Code: `./uninstall.sh`. Codex: `bash adapters/codex/uninstall.sh`.

Each uninstaller removes only its owned host registration. Neither deletes your
projects, DeepSeek config/API key, logs, or account.

## License

MIT
