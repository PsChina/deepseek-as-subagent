# Task Contracts

Task Contracts provide a versioned, per-delegation capability boundary. They are
optional and only affect the new contract-aware MCP entrypoints. Legacy tool
schemas and calls remain unchanged.

## Example

~~~json
{
  "version": 1,
  "intent": "implement",
  "permissions": {
    "tools": ["Read", "Glob", "Grep", "Write", "Edit"],
    "read_paths": ["src/**", "tests/**"],
    "write_paths": ["src/**", "tests/**"],
    "deny_paths": ["**/generated/**"]
  },
  "constraints": {
    "must_not_change": ["Public API behavior"],
    "success_checks": ["python -m unittest discover -s tests -v"],
    "expected_outputs": ["changed_files", "test_results", "risks"]
  },
  "limits": {
    "max_tool_calls": 40,
    "max_mutation_bytes": 10485760
  }
}
~~~

Pass the object as the required `contract` argument to
`delegate_to_deepseek_with_contract`,
`delegate_to_deepseek_readonly_with_contract`,
`start_deepseek_with_contract`, or
`start_deepseek_readonly_with_contract`.

## Enforcement model

The effective tool set is the intersection of the administrator's
`config.json` `allowed_tools`, the immutable API execution profile, and the
contract's optional `permissions.tools` list. The host compiles this into an
immutable `EffectivePolicy`, passes the same snapshot across the isolated
tool-process boundary, and checks it again at dispatch time. Read, Write, Edit,
and NotebookEdit paths are also rechecked against the canonical path produced
by the file-I/O resolver immediately before descriptor-based access or commit.

- `read_paths` limits Read, Glob, and Grep results. Edit and NotebookEdit also
  require read access because they inspect existing file contents.
- `write_paths` limits Write, Edit, and NotebookEdit. Mutation tools are
  removed when no write paths are declared.
- `deny_paths` always includes built-in credential and secret patterns.
  User-provided patterns can add restrictions but cannot remove built-ins.
- Resource limits can lower, never raise, process-wide hard limits.
- Read and Grep reject files with multiple hard links, even if all links appear to be within the workspace. Link count and file identity are checked before and after reading; copy a needed file to a normal single-link file rather than weakening this boundary.
- Patterns are workspace-relative, slash-separated globs. `**` matches zero
  or more path segments. Built-in deny rules are always case-insensitive;
  allowed-path matching is case-insensitive on Windows and case-sensitive on
  POSIX to avoid broadening scopes on case-sensitive filesystems. Absolute paths, parent traversal, backslashes, and
  overlong patterns are rejected. Built-in denies cover common environment,
  package-manager, SSH, cloud, Kubernetes, Docker, Git internal metadata
  (including remote configuration), and private-key credentials.

## Current limitations

Bash is intentionally unavailable whenever a Task Contract is supplied. A
shell command can bypass file-tool path scopes; exposing Bash before a real
command allowlist or OS-level sandbox exists would make the contract misleading.
Calls without a contract retain existing Bash behavior.

`must_not_change` and `success_checks` are supplied to the model as task
instructions. They are not proof of compliance. The handoff records evidence
but does not automatically execute `success_checks`; a model's final text is
not test evidence.


## Execution audit and structured handoff

Contract-bound results include a versioned handoff object. It records the
effective policy, transaction IDs, canonical workspace-relative changed-file
paths, operation, commit status, and the SHA-256 digest observed by the host
after each mutation. The parent process compares the tool's mutation digest
with the file identity before recording a committed mutation.

The handoff also reports deterministic path-policy checks, resource-limit
checks (observed tool calls and reserved mutation bytes), Bash attempt versus
execution counts, and mutation-record integrity. Edit and NotebookEdit
mutations are checked against both read and write scopes. It deliberately
distinguishes these from functional verification:

- `success_checks` are listed as `not_run` unless a separate verifier has
  actually executed them.
- `must_not_change` constraints are marked `not_verified` and require
  independent review.
- `expected_outputs` are marked `not_validated` until a reviewer confirms
  them.
- `verification_status=policy_verified` means only that the recorded
  mutations satisfy the effective path policy and have commit-time digest
  evidence. It is not a claim that the code is functionally correct.
- `verification_status=partial` means one or more evidence sources are missing
  or declared checks and semantic constraints remain unverified.
- `verification_status=failed` means a recorded path-policy or resource-limit
  violation was detected.
- `bash_summary.calls` counts attempts; `bash_summary.executed_calls` counts
  commands for which the host recorded an actual shell exit status. A rejected
  Bash request is not misreported as executed.

### Final workspace readback (host observation)

When the task returns successfully, the host reopens the **last recorded version**
of each mutated path through the race-resistant workspace file reader, subject
to the write scope and built-in denied paths. The handoff includes
`workspace_evidence` and a `verification.workspace_readback` summary:

- `matched`: the final observed file SHA-256 matched the last mutation.
- `changed`: content differed at readback time (possibly concurrent changes).
- `not_verifiable`: the file was missing, unsafe, inaccessible, or its record
  was incomplete. Never interpret this as a successful verification.
- `not_applicable`: there were no recorded mutations.

The report cross-checks observation paths and expected digests against mutation
records. Duplicate paths, missing observations, and contradictory evidence do
not pass verification. Only the last version of a file is checked; earlier
mutations remain covered by commit-time readback and the transaction journal.

**Trust boundary:** this evidence is generated by the host and is not
cryptographically signed or independently attested. A fabricated entire result
object could fabricate both the mutation record and its observation. The
readback is a point-in-time snapshot, not proof of continued file integrity or
functional correctness. Keep `needs_review=true` for code review.

### Concurrency, filesystem races, and cancellation

The per-task mutation budget uses atomic reservations and refuses child-process
budget regressions. Budget reservations are not refunded if a write raises
after possibly committing. The execution manager also enforces a workspace
lease and recovery acknowledgement after uncertain mutations.

POSIX file access uses directory descriptors and no-follow file opening;
Windows uses handle checks, reparse-point rejection, and before/after path
identity checks. Read and Grep additionally reject multiply-linked files and
recheck link count and file identity after reading to detect changes during I/O.
The task scope remains bound through canonical file resolution,
parent-side mutation verification, and final workspace readback. Dedicated tests cover a Windows parent rename between handle
observations and POSIX ancestor swapping. These measures do **not** provide
a general OS-level sandbox or prove the absence of all adversarial concurrent
filesystem races. Running an untrusted process under the same OS identity
requires additional isolation outside this Task Contract implementation.

Contract-bound Bash remains disabled, so `commands_run` is empty. Raw shell
commands and tool output are not retained in the handoff.

## Mutation recovery

Every new durable mutation intent stores the intended file SHA-256 and, when
available, the pre-mutation baseline: either that the target did not exist or
the previous file SHA-256. The recovery query reports one of three states:

- `committed`: the current file matches the intended replacement digest.
- `not_committed`: the target is still absent as expected, or its content still
  matches the recorded pre-mutation baseline.
- `uncertain`: the current file matches neither state, cannot be read safely,
  or belongs to a legacy journal record without baseline evidence.

The acknowledgement endpoint removes only `committed` and `not_committed`
records. An `uncertain` record remains blocked for manual inspection. Legacy
version-1 records remain readable; they can be acknowledged only when the
current file matches the recorded replacement digest. Batch acknowledgement
validates every requested record before beginning deletion. If storage fails
during deletion, the result is explicitly uncertain; query recovery again
before retrying rather than assuming the batch was rolled back.

## Validation: deterministic E2E vs. live provider

The normal CI matrix tests the complete Python package on Ubuntu, Windows, and
macOS (Python 3.10, 3.11, and 3.12). It also exercises a local OpenAI-compatible
HTTP endpoint over the real provider/agent/tool-child processes, including a
contract-bound multi-step Read → Write → Edit task, denied out-of-scope tool
requests, and final host workspace digest evidence. This is an **offline,
deterministic integration test**, not a claim that the DeepSeek production API
has been exercised.

For an optional real-API smoke test, run this only in a trusted environment
where the credential is already available as an environment variable:

```sh
export DEEPSEEK_LIVE_TEST=1
export DEEPSEEK_API_KEY=...  # Set privately; never commit to Git
# Optional: DEEPSEEK_LIVE_MODEL and DEEPSEEK_LIVE_BASE_URL
python -m unittest discover -s tests -p 'test_live_deepseek_api.py' -v
```

The real-API test uses a temporary read-only workspace, a task contract limited
to `src/**`, and a short turn/token budget. If the opt-in flag is absent, it
is skipped. GitHub PR CI intentionally does not receive a real provider key.
A green offline matrix therefore does **not** establish real API compatibility;
record a separate live test result, timestamp, model, and endpoint before
considering that gate passed. Never log or attach the API key, full request
headers, or raw secret-bearing model output.
