# Sandboxed hidden-test execution and real-model benchmark

## Why a separate executor exists

The ordinary workspace tool child is killable and policy-scoped, but it is **not**
an OS security sandbox. Task Contract rules constrain the Agent's MCP tools; they
do not make arbitrary Python or shell code safe to execute as the host user.

This stage therefore keeps test execution outside the Agent tool surface. There
is no new Bash capability and no command string is accepted from the model.

## Execution boundary

~~~text
Credentialed model worker (ephemeral GitHub Actions VM)
  DeepSeek API key -> provider client
  Agent tools: Read / Write / Edit only
  Temporary workspace -> exact source snapshot + hashes
  No model-generated source is executed
                 |
                 | one bounded JSON artifact
                 v
No-secret verification worker (different ephemeral VM)
  No DeepSeek/OpenAI API key
  Checkout trusted hidden tests
  Validate artifact schema, contract hash and source digest
  Stage only fixed-path, allowlisted source files
                 |
                 v
Docker container (pre-pulled image pinned by SHA-256)
  --network=none             --read-only root filesystem
  --cap-drop=ALL              --no-new-privileges
  seccomp default             UID/GID 65534 (non-root)
  512 MiB memory              1 CPU / 64 PIDs
  read-only /workspace        read-only /tests
  bounded /tmp tmpfs          60-second test deadline
  fixed Python unittest command; no shell
~~~

The GitHub workflow is manual (workflow_dispatch) and splits model preparation
and test execution into **separate hosted workers**. The API key exists only in
the model job. That job never invokes Docker or runs the generated source. The
verification job does not receive provider credentials and has read-only
repository permissions. The container receives neither credentials nor a
GitHub token.

The JSON artifact contains no file paths selected by the model. It contains only
the task ID, repeat number, Task Contract hash, source SHA-256, base64 source
bytes, and non-secret Agent metrics. The verifier maps task IDs to fixed
workspace paths and trusted hidden suites. Duplicate JSON keys, unknown fields,
duplicate runs, path traversal, malformed base64, oversized files and hash
mismatches fail closed.

## Container runner API

The host caller supplies a trusted plan, not a shell command:

~~~python
from deepseek_mcp.sandbox_executor import (
    compute_test_suite_digest,
    execute_sandboxed_tests,
)

plan = {
    "version": 1,
    "contract_sha256": config.effective_policy.contract_hash,
    "test_suite_sha256": compute_test_suite_digest(hidden_tests),
    "source_files": [
        {"path": "src/task.py", "sha256": source_sha256},
    ],
}
result = execute_sandboxed_tests(
    config, plan, hidden_tests,
    image="python:3.11-slim-bookworm@sha256:<64 lowercase hex digits>",
    timeout_seconds=60,
)
~~~

The image must already be present locally and be referenced by an immutable
SHA-256 digest. The runner uses --pull=never; it does not download images while
handling a task. The workflow pulls the operator-supplied digest before running
tests. The source snapshot includes only exact paths in the trusted plan, and
each source path must be readable under the active Task Contract. It is reopened
with the existing workspace identity and no-symlink checks, checked against its
expected digest, and copied to a fresh temporary directory. The hidden suite is
read from a separate host-owned directory, hashed deterministically, and
mounted separately. Both mounts are read-only.

The test command is fixed to Python's unittest discover over /tests; there
is no command field in the plan. Execution is Linux-only and fails closed when
Docker is missing, the image is not preloaded, the image is not digest-pinned,
or the hidden suite overlaps the Agent workspace. Output is capped at 64 KiB.
Timeout, output overflow, Docker daemon errors and test failures are distinct
non-success outcomes.

## Run a live DeepSeek benchmark

Prerequisites:

- A trusted checkout of this branch (or the merged revision).
- A DEEPSEEK_API_KEY available only to the model-preparation phase.
- Docker Engine on Linux.
- A pre-pulled and audited Python image referenced by name@sha256:<digest>.
- A valid DeepSeek model ID supported by the configured endpoint.

For a local two-process run:

~~~sh
docker pull 'python:3.11-slim-bookworm@sha256:<audited-digest>'
export DEEPSEEK_API_KEY='...'
export DEEPSEEK_LIVE_CODE_BENCHMARK=1
export DEEPSEEK_TEST_SANDBOX_IMAGE='python:3.11-slim-bookworm@sha256:<audited-digest>'
export DEEPSEEK_LIVE_MODEL='deepseek-chat'
export DEEPSEEK_BENCHMARK_REPEATS=2
python scripts/run_live_code_benchmark.py
~~~

The wrapper runs prepare_live_code_benchmark.py first, then starts a second
process with DEEPSEEK_API_KEY and OPENAI_API_KEY removed. For stronger
separation, use the manual GitHub Actions workflow after it is available on the
repository's default branch. Add DEEPSEEK_API_KEY as a repository Actions
secret, then dispatch **Live DeepSeek Hidden-Test Benchmark** with a model ID,
repeat count and audited image digest. The model job uploads only the prepared
source artifact; the second job performs the hidden-test evaluation.

The benchmark currently contains two small tasks (off-by-one summation and
string case normalization), each repeated 1–5 times. Hidden tests cover edge
cases plus the isolation contract itself: non-root execution, no provider
credentials, read-only source mounts and no outbound network. The provider
never receives the hidden test source. These tasks are a first integration
benchmark, not a statistically meaningful measure of general coding ability.

## Security limits and remaining work

Docker containers share the host kernel. Capabilities, seccomp, namespaces,
read-only mounts and network isolation are defense in depth, **not a proof
against kernel or container-runtime vulnerabilities**. The Docker daemon and
image digest are trusted infrastructure. For hostile multi-tenant workloads,
run the no-secret verification job inside a disposable VM and consider a
stronger isolation runtime such as gVisor or Kata Containers. Never mount the
Docker socket, host home directory, credential files or writable repository
checkout into the test container.

The live API benchmark has not been run by CI automatically. It is deliberately
manual and cost-bearing. CI tests the plan parser, snapshot policy, fixed command
construction, and failure handling without provider credentials. A manual run
must record the exact model ID, provider host, run count, token usage, task
success rate, policy pass rate, p50/p95 duration, sandbox exit status and image
digest. Do not treat a small sample as a model leaderboard.
