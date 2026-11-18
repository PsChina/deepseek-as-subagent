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
  Run host-side acceptance checks with expected values and fixtures
  Validate artifact schema, contract hash and source digest
  Send function calls over bounded JSON lines
                 |
                 v
Docker container (pre-pulled image pinned by SHA-256)
  --network=none             --read-only root filesystem
  --cap-drop=ALL              --no-new-privileges
  seccomp default             UID/GID 65534 (non-root)
  512 MiB memory              1 CPU / 64 PIDs
  read-only /workspace        candidate source only
  bounded /tmp tmpfs          60-second test deadline
  fixed Python worker
~~~

The GitHub workflow runs manually on the default branch and splits model preparation
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
SHA-256 digest. The runner uses --pull=never. The workflow pulls the
operator-supplied digest before running
tests. The source snapshot includes only exact paths in the trusted plan, and
each source path must be readable under the active Task Contract. It is reopened
with the existing workspace identity and no-symlink checks, checked against its
expected digest, and copied to a fresh temporary directory. Docker mounts that
snapshot read-only. The verifier reads and hashes the suite on the host and
keeps its test files, expected values, and fixture bytes there.

The host runs the suite's run(candidate, fixtures) function. It sends only
function names and arguments to the fixed Python worker. The worker reports
filesystem, network, user, and seccomp settings before loading candidate code.
Execution requires Linux, Docker, a preloaded digest-pinned image, and a suite
outside the Agent workspace. Requests and responses share a deadline, and total
protocol output is capped at 64 KiB. Timeout, output overflow, runner errors,
and failed assertions produce distinct results.

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
process with a minimal environment for local verification. For stronger
separation, use the manual GitHub Actions workflow after it is available on the
repository's default branch. Add DEEPSEEK_API_KEY as a repository Actions
secret, then dispatch **Live DeepSeek Hidden-Test Benchmark** with a model ID,
repeat count and audited image digest. The model job uploads only the prepared
source artifact; the second job performs the hidden-test evaluation.

The benchmark contains two small tasks (off-by-one summation and string case
normalization), each repeated 1–5 times. The provider receives the task and the
candidate file. The verifier checks edge cases from the host suite, which is
published in this repository and withheld from the task prompt and container.
The worker checks its isolation separately. These cases measure the test
pipeline and task outcomes for a small sample.

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
manual and cost-bearing. CI tests the plan parser, snapshot policy, and failure
handling without provider credentials. The Ubuntu/Python 3.11 job also runs
both task suites and a known failing candidate in Docker. A manual run
must record the exact model ID, provider host, run count, token usage, task
success rate, policy pass rate, p50/p95 duration, sandbox exit status and image
digest. Do not treat a small sample as a model leaderboard.
