# Codex Adapter

Use `delegate_to_deepseek` from [OpenAI Codex CLI](https://developers.openai.com/codex/cli).

## Install

### 1. Build the MCP server

From repo root:
```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install 'httpx[socks]'    # if you use a SOCKS proxy
```

### 2. Configure DeepSeek API key

```bash
mkdir -p ~/.deepseek-mcp
cat > ~/.deepseek-mcp/config.json <<'EOF'
{
  "api_key": "PASTE_YOUR_DEEPSEEK_KEY_HERE",
  "model": "deepseek-v4-pro",
  "max_turns": 50,
  "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
}
EOF
chmod 600 ~/.deepseek-mcp/config.json
# Edit the file to paste your real DeepSeek API key
```

Get a key at [platform.deepseek.com](https://platform.deepseek.com).

**Note**: workspace (sandbox root) auto-follows the directory where you launch
`codex` — no need to configure it. To lock the sandbox to a specific path
regardless of cwd, add `"workspace": "/abs/path"` to the config.

### 3. Register MCP server with Codex

Pick one:

**Option A — CLI**:
```bash
codex mcp add deepseek -- /absolute/path/to/deepseek-as-subagent/.venv/bin/deepseek-mcp
```

**Option B — Edit `~/.codex/config.toml`** (copy from `config.toml.example`):
```toml
[mcp_servers.deepseek]
command = "/absolute/path/to/deepseek-as-subagent/.venv/bin/deepseek-mcp"
```

### 4. Teach Codex when to delegate

Codex doesn't have Claude Code's skill system. Copy `instructions.md` content
into one of these (in order of preference):

- **Project-scoped** (recommended): paste into `AGENTS.md` at your project root
- **Global**: append to `~/.codex/instructions.md`

Without this step, Codex *can* invoke the tool, but won't know *when* to —
delegation quality drops to "user must explicitly request it every time".

## Verify

```bash
codex mcp list                                 # deepseek should appear
codex
> please call the deepseek delegate_to_deepseek tool to write a hello world Python
  script to /tmp/codex-hello.py
```

Expected: tool invokes, DeepSeek runs its agent loop, returns a summary with
turns/tokens/duration. Then run `cat /tmp/codex-hello.py` to verify the output.

## Differences vs Claude Code adapter

| Feature | Claude Code | Codex |
|---|---|---|
| MCP tool `delegate_to_deepseek` | ✅ | ✅ identical |
| Auto-decide when to delegate | ✅ via skill | ⚠️ via your `AGENTS.md` |
| Slash command `/ds` | ✅ | ❌ (Codex has no equivalent) |
| Pure mode (disable for one session) | ✅ via `pure` alias | ⚠️ run `DEEPSEEK_MODE=off codex` manually |
| One-shot installer | ✅ `./install.sh` (root) | ❌ manual steps above |

PRs welcome to add a Codex auto-installer (`adapters/codex/install.sh`).
