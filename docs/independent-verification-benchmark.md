# Independent verification and deterministic code-task benchmark

This work is intentionally separated from Task Contract PR #38. Its initial
base is \`feat/task-contract-policy-engine\`, not \`main\`. It must not be merged
ahead of the Task Contract dependency.

## Purpose

The first host-side verifier compares **trusted, predeclared file expectations**
with securely reopened workspace files. It never decides functional correctness
from the model's final answer, its own confidence, or a commit SHA alone.

Three independent pieces of evidence must remain distinct:

1. **Mutation evidence:** A task wrote a particular digest and the host observed it.
2. **Artifact expectations:** Trusted expected outputs match exact SHA-256 digests,
   and files that were supposed to remain unchanged still match their baselines.
3. **Functional checks:** Explicitly approved tests actually executed and their
   outcomes were observed. **The general-purpose verifier does not run these.**

The verifier returns \`artifact_verified\`, \`incomplete\`, \`failed\`, or
\`invalid_plan\`; even \`artifact_verified\` is **not** a blanket declaration of
functional correctness. Declared success checks remain \`not_run\`.

## API

A trusted host creates a version-1 verification plan, independently of any
model-authored output:

\`\`\`python
from deepseek_mcp.independent_verifier import verify_artifacts

plan = {
    "version": 1,
    "contract_sha256": config.effective_policy.contract_hash,
    "artifacts": [
        {"path": "src/result.py", "role": "output", "sha256": expected_sha256},
        {"path": "src/public_api.py", "role": "unchanged", "sha256": baseline_sha256},
    ],
    "success_checks": ["python -m unittest discover"],
}
report = verify_artifacts(config, plan)
\`\`\`

The test name above must also be declared in the Task Contract. It is a
**label, not an executable command**: the verifier reports \`not_run\` and does
not invoke a shell, interpreter, or model-provided command.

Plan validation enforces an exact schema, size limits, normalized relative
paths, unique artifacts, and lowercase SHA-256 hashes. Every read uses the
existing workspace-identity guard, scoped path policy, no-symlink canonical path
check, and bounded descriptor/handle-based reader. Missing, altered, or unsafe
files never produce a verified result.

### Trust boundary

A plan is a host-authored fixture or signed-off acceptance manifest. Do **not**
accept a plan, its expected digest, or benchmark success status from the model
whose work is being evaluated. No cryptographic plan signature or remote
attestation is provided in this initial version.

## Offline code-task benchmark

From a trusted development checkout or an ephemeral CI runner:

\`\`\`sh
python scripts/run_contract_benchmark.py
python -m unittest discover -s tests -p "test_independent_verifier.py" -v
\`\`\`

The benchmark uses a localhost OpenAI-compatible scripted provider, the real
agent loop, real isolated tool subprocesses, and the actual workspace mutation
journal. Its current fixed tasks are:

- Repair an off-by-one Python function using Read → Edit → Read.
- Repair case normalization in a Python function using Read → Edit → Read.

For each task it verifies the expected code digest, an untouched guard file,
the structured handoff's policy and readback evidence, and a tiny **trusted
fixture-specific behavioral test**. It executes only source that has just been
securely read and whose SHA-256 matches the fixed trusted fixture. No arbitrary
model output is executed, and no model-controlled shell command is accepted.

CI runs this suite on Ubuntu/Python 3.11; ordinary unit tests cover the verifier
and score aggregation on all nine OS/Python combinations.

### Metrics

\`summarize_benchmark\` reports exact counts, fail-closed task success rate,
policy pass rate, total tool calls, median runtime, and p95 runtime. An incomplete
or policy-unverified case is **never** counted as a successful task.

These fixed scripted cases measure the *integration pipeline*, not DeepSeek's
reasoning quality or real-world coding ability. They do not establish statistically
significant benchmark performance. They are suitable as deterministic regressions.

## Follow-on acceptance gates

- Run a separate **credential-gated real DeepSeek API** test and record only the
  model, endpoint, timestamp, and results (never keys or raw headers).
- Add a preapproved functional-test executor in an OS-level isolated CI worker
  before running arbitrary model-produced code. Shell allowlists without isolation
  cannot enforce Task Contract filesystem restrictions.
- Add representative repository bug fixes, held-out tests, multiple model runs,
  confidence intervals, and controlled latency/cost comparisons.
- Obtain independent maintainer security review before a production merge.

This does not change the existing four legacy MCP schemas and does not expose
a new Agent tool that could execute tests or expand permissions.
