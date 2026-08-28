# Security model

`deepseek-as-subagent` treats model-generated tool calls as untrusted input.
The default coding API enables workspace-scoped file tools and bounded Bash so a
new installation can complete coding tasks. The readonly APIs expose only Read,
Glob, and Grep.

## Data sent to DeepSeek

Delegated task/context text, model messages, and outputs of files/tools chosen
by DeepSeek are sent to the configured OpenAI-compatible endpoint. Do not
delegate secrets or data that endpoint is not permitted to receive. The default
endpoint is `https://api.deepseek.com`; non-loopback HTTP is rejected.

Trusted-host Bash receives no provider credential, proxy variable, or host
`HOME`; its `HOME` is the workspace. It runs with the local user's permissions,
so it is not an OS sandbox. Logs redact file contents, edit strings, and
commands; usage records contain counts and timings rather than task text.

Provider calls run in an isolated Python child with private stdio pipes. The
credential is sent over that private input pipe, never command-line arguments or
inherited environment. Workspace cwd/PYTHONPATH/user-site and `.pth` processing
are excluded from helper imports. File-mutation configurations fail closed when
the runtime or installed package is inside the delegated workspace.

On POSIX, the server and provider child disable core dumps before reading
credentials. Linux provider children also have a 1 GiB hard address-space limit;
macOS relies on the platform-independent decoded-response cap. Windows processes
set WER's `NOHEAP` flag before accepting work.

Configuration reads are capped at 1 MiB, reject duplicate keys, and reject
unsupported keys. POSIX config directories/files must be private (`0700` /
`0600`). Windows real API keys must be supplied by `DEEPSEEK_API_KEY`, not
persisted in `config.json`.

## Capabilities and Bash execution

Coding APIs freeze this tool set before the model starts:

```json
{"allowed_tools":["Read","Write","Edit","Bash","Glob","Grep","NotebookEdit"]}
```

Readonly APIs freeze only `Read`, `Glob`, and `Grep`. Steering and model tool
requests cannot switch either profile. Path tools resolve targets under the
configured workspace, reject outward symlinks, and exclude host agent/config and
VCS control paths.

Coding Bash runs only through the existing `tool_process` / `tool_child`
boundary. The trusted-host executor has workspace cwd, disabled stdin, bounded
stdout/stderr, timeout, command checks, process-tree cleanup, and credential
isolation. It supports macOS, Linux, and Windows.

## Concurrency, recovery, and lifecycle

An OS-backed lease keyed by workspace filesystem identity prevents concurrent
delegation against the same workspace. Different workspaces run independently.
The lease is released after the active execution and its child processes finish.

Before `Write`, `Edit`, or `NotebookEdit` publishes an atomic replacement, the
tool child persists a private mutation intent. `get_deepseek_recovery` audits
records and the host must verify files then call
`acknowledge_deepseek_mutations` with exact IDs. New mutation-capable delegation
fails closed while records remain unacknowledged.

Each delegated run is wall-clock bounded to 18,000 seconds by default and
172,800 seconds maximum. Provider HTTP calls run in fresh private-pipe children
and are capped at 180 seconds. Cancellation wakes retry backoff and terminates
in-flight provider or local-tool subprocesses.

## Installation supply chain

Install from a reviewed tag or commit. Supported installers and CI use the same
hash-locked `requirements.lock`; project installation disables dependency
resolution and build isolation. Installers stage and validate a fresh generation
before replacing registration, so failures preserve the active runtime.

The registry/introspection `Dockerfile` is a separate distribution artifact. It
is not an execution sandbox and pins its base image and dependency lock.

## Reporting vulnerabilities

Do not include credentials or private workspace content in a public issue. Use
the repository's private security-reporting channel when available.
