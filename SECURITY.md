# Security model

`deepseek-as-subagent` treats model-generated tool calls as untrusted input.
The production default enables workspace-scoped reads, searches, and file
mutation so a newly installed sub-agent can implement code. Command execution
remains an opt-in capability.

## Data sent to DeepSeek

Delegated task/context text, model messages, and the outputs of files/tools that
DeepSeek chooses to read are sent to the configured OpenAI-compatible API
endpoint. Do not delegate a workspace containing secrets or data that the
configured endpoint is not permitted to receive. The default endpoint is
`https://api.deepseek.com`; non-loopback plain HTTP endpoints are rejected.

The server never passes the host API key, proxy variables, or host `HOME` into
the optional command container. Logs redact file contents, edit strings, and
commands; usage records contain counts/timings rather than task text.
Provider calls run in an isolated Python child with private stdio pipes. Its
credential is sent through the private input pipe, not command-line arguments
or inherited environment. Workspace cwd/PYTHONPATH/user-site and `.pth`
processing are excluded from fresh helper imports. If file-mutation tools are
enabled, configuration fails closed when the Python runtime or installed
package is inside the delegated workspace.
On POSIX, both the long-lived server and provider child disable core dumps
before reading credentials. On Linux, the provider child also has a 1 GiB hard
address-space limit. Darwin does not document `RLIMIT_AS` as a supported limit,
and modern macOS processes reserve sparse address ranges much larger than
1 GiB, so macOS relies on the platform-independent incremental decoded-response
cap. Successful decoded HTTP bodies have a 2 MiB cap before JSON parsing.
On Windows, both processes set WER's `NOHEAP` flag before accepting work so
ordinary crash and non-response reports do not collect heap contents.

Persistent POSIX logs are private and bounded: `server.log` and `usage.log`
rotate at 10 MiB and complete new records are dropped once the active server
log reaches its cap. Persistent logging is disabled on Windows because the
same descriptor-based no-follow boundary is unavailable there.

Configuration reads are capped at 1 MiB. JSON objects reject duplicate keys,
and the top-level object rejects unsupported keys so a misspelled capability
setting cannot silently fall back to a more permissive default. Likewise,
`DEEPSEEK_MODE` accepts only the exact values `auto` and `off`; other values
fail closed.

On POSIX, installers require the config directory and file to be private
(`0700` / `0600`) and fail if those permissions cannot be applied. On Windows,
store a real API key only in `DEEPSEEK_API_KEY`; `config.json` may contain the
placeholder but not a persisted real key. The Codex config transaction opens
each local-drive path component with no reparse traversal, verifies final
handle paths, requires current-user ownership with no foreign write-capable
DACL entry, and performs replacement/deletion through anchored handles.

## Installation supply chain

Install from an explicitly reviewed tag or commit. The retired
`curl-install.sh` compatibility endpoint only prints migration instructions;
it never clones, pulls, or executes repository code. Both supported installers
and CI use the same `requirements.lock` with exact versions and distribution
hashes across Python 3.10–3.12 on macOS, Linux, and Windows. Project installation
then disables dependency resolution and build isolation. CI installs
`pip-audit` only from the separate hash-locked `requirements-audit.lock` and
audits the installed environment; production installers never install it.
Both host installers stage a fresh generation and validate it before changing
the active registration. A failed package, config, or MCP smoke check therefore
leaves the previously registered runtime untouched.
Install/uninstall flows hold a host-specific mkdir lease through registration
verification and pruning. Claude helper links point only to private copied
generations under `~/.deepseek-mcp`, never back into the install checkout.
The registry/introspection `Dockerfile` also pins the official Python base image
by manifest digest, installs from the same runtime hash lock, keeps application
files root-owned, and runs the MCP entrypoint as an unprivileged fixed UID.

## Capability levels

The default coding profile permits workspace-scoped file edits without command
execution:

```json
{
  "allowed_tools": ["Read", "Write", "Edit", "Glob", "Grep", "NotebookEdit"]
}
```

Operators who require a read-only reviewer can explicitly reduce the tool set:

```json
{
  "allowed_tools": ["Read", "Glob", "Grep"]
}
```

Path tools resolve every target under the configured workspace and reject
symlinks that resolve outside it. They protect against model mistakes; they do
not attempt to isolate a hostile process already running as the same OS user.
Bounded Glob/Grep traversal never follows file or directory symlinks; Grep
reads regular files through no-follow descriptors. Host agent/config roots and
VCS control directories (`.git`, `.hg`, `.svn`) are excluded from all model file
tools. Windows traversal additionally rejects Win32 alias/reserved components
and validates held directory/file handles against their expected final paths.

## Opt-in Bash container

There is no host-shell fallback. Enabling `Bash` requires macOS or Linux, a
local Docker/Podman engine, and an image pinned by registry digest:

For upgrade compatibility, an old config that contains `Bash` but contains no
`bash_*` keys is treated in memory as Bash disabled. The config file is not
rewritten. Any explicit or partial Bash configuration remains subject to the
strict checks below and fails closed unless it is complete.

```json
{
  "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
  "bash_backend": "container",
  "bash_runtime": "docker",
  "bash_image": "registry.example/team/deepseek-shell@sha256:<64-lowercase-hex-digest>",
  "bash_memory": "512m",
  "bash_cpus": 1,
  "bash_pids_limit": 128
}
```

The digest-pinned image must already exist locally (`--pull=never`) and provide
`/usr/bin/env` plus `/bin/sh`. Each command runs in a fresh container with:

- no network;
- a read-only container root;
- a disposable read-only snapshot containing only no-follow-opened regular
  files and real directories from the workspace;
- private temporary and home directories;
- all Linux capabilities dropped and `no-new-privileges` enabled;
- bounded CPU, memory, process count, runtime, captured output, snapshot size,
  entry count, depth, and preparation time;
- no persisted container log driver and a zero core-dump ulimit.

`bash_memory` accepts Docker-style byte/kilobyte/megabyte/gigabyte values up to
an absolute 64 GiB configuration ceiling.

The host workspace itself is never bind-mounted. Symlinks, Unix sockets,
FIFOs, devices, and other special entries are omitted from the snapshot, so a
workspace-mounted engine socket cannot cross the boundary. The workspace
snapshot cannot be modified. Commands may use only the bounded 64 MiB `/tmp`
and temporary home tmpfs mounts for scratch output; both are discarded after
the managed container is confirmed removed. Use the explicitly enabled
`Write`, `Edit`, or `NotebookEdit` tools for workspace changes.

For Git workspaces, the snapshot contains a synthetic single root commit made
only from the visible regular files after the no-follow copy. Host workspace
Git metadata, object stores, configuration, refs, history, and deleted blobs
are never inputs to a Git process. Nested `.git`/`.GIT` metadata is excluded at
every depth. The synthetic repository starts clean, so it does not preserve the
host repository's staged/dirty distinction. A command can inspect the baseline
but cannot recover the source repository's prior object history.

Remote Docker contexts/daemons are rejected because they cannot enforce a
local-workspace boundary. Windows remains fully supported for path tools, but
configuring `Bash` fails closed.

Podman requires an explicit local Unix service endpoint. On rootless Linux,
start the user socket (for example, `systemctl --user enable --now
podman.socket`) and set `CONTAINER_HOST` to its absolute `unix://` socket path.
On macOS, start the Podman machine and set `CONTAINER_HOST` to the machine's
local forwarded Unix socket. The installer performs a bounded `podman info`
probe; TCP/SSH endpoints and implicit remote connections are rejected. Docker
binds use private SELinux relabeling (`:Z`); Podman mounts use
`relabel=private`.

Per run, the agent accepts at most 32 tool calls in one turn, 128 total tool
calls, 64 MiB of requested file mutations, and one million cumulative model
tokens. Each provider response is capped at 16,384 output tokens, provider
decoded HTTP/pipe payloads are byte-bounded, and individual
file/tool/traversal/snapshot outputs also have independent hard limits.

## Concurrency and lifecycle

An OS-backed lease keyed by filesystem identity prevents two MCP server
processes from running DeepSeek concurrently against the same workspace,
including path aliases to the same directory. Different workspaces can run
independently. A watchdog—not the OCI runtime or its control commands—inherits
the lease. If the parent crashes or cleanup cannot yet be confirmed, the
watchdog retains the lease and retries container and snapshot removal with a
bounded exponential interval. The lease is released only after cleanup is
confirmed; persistent engine failure can therefore intentionally leave the
workspace busy until an operator restores the local engine.

Before `Write`, `Edit`, or `NotebookEdit` publishes an atomic replacement, the
tool child synchronously persists a private mutation intent under
`~/.deepseek-mcp/transactions/`. Records are scoped by a hash of workspace
filesystem identity and contain only transaction ID, tool, relative path,
replacement SHA-256, and bounded recovery warnings—not task text, file content,
or credentials. A crash or lost MCP response leaves the record pending.
`get_deepseek_recovery` audits the current file as `committed` or `uncertain`;
the host must verify it and pass the exact ID to
`acknowledge_deepseek_mutations`. Query/ack and execution share the workspace
lease, ack never changes workspace files, and new delegation fails closed while
records remain. POSIX storage enforces private no-follow descriptors and native
locks; Windows storage enforces local non-reparse handles and current-user ACLs.

Codex config installation, rollback, and uninstall use a separate advisory
cross-process lease and recheck inode, modification time, and content hash just
before atomic replacement. This serializes cooperating adapter processes.
An unrelated editor that ignores the lease can still race in the tiny interval
between the final check and the platform's atomic replace; there is no portable
filesystem compare-and-swap primitive, so newer post-install edits are detected
and rollback fails closed instead of overwriting them.

Background job state/results are intentionally session-scoped and in memory.
Restarting the MCP server ends its daemon worker and makes its old job IDs
unavailable. Use background mode for steering/cancellation, and collect the
result before closing the host session.

Each delegated run has a configurable wall-clock limit: 18,000 seconds (5
hours) by default and an absolute configuration maximum of 172,800 seconds (48
hours). Each provider HTTP request is additionally isolated in a fresh private-
pipe subprocess and capped at 180 seconds. Cancellation wakes retry backoff and
promptly terminates an in-flight provider or local-tool subprocess. If the tool
started a container, pipe EOF tells its independent watchdog to begin forced
container and snapshot cleanup immediately.

## Reporting vulnerabilities

Do not include credentials or private workspace content in a public issue.
Use the repository's private security-reporting channel when available.
