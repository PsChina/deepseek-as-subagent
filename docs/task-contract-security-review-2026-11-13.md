# Task Contract — Code-Level Security Review

Reviewed: 2026-11-13  
Scope: PR #38, `feat/task-contract-policy-engine`  
Status: **review in progress; not a certification or a third-party penetration test**

## Threat model and trust boundaries

- Treat the model's instructions, tool names, JSON arguments, and provider responses as untrusted.
- Treat workspace filenames, symlinks/reparse points, and concurrent file changes as potentially adversarial.
- A tool process can be killed after committing a file but before reporting completion.
- API credentials belong only to the provider boundary; tool subprocesses must not inherit provider credentials.
- Task Contract authorization is built-in-tool enforcement **not** an OS-level filesystem or network sandbox.

## Findings and changes

| ID | Finding | Impact | Status |
| --- | --- | --- | --- |
| SEC-01 | The bounded JSON validator called `math.isfinite` without importing `math`. Float-shaped input could raise `NameError` rather than a controlled `ValueError`. | Unhandled validation failure; not an observed permission bypass | Fixed; finite and nonfinite float regression test added |
| SEC-02 | Defense-in-depth deny patterns did not explicitly cover Git metadata such as `.git/config`, which may contain credential-bearing remote URLs. | Potential secret exposure if broader path restrictions change or are bypassed | Explicit root and nested Git metadata denies and regression assertions added |
| SEC-03 | An optional live API smoke test could succeed without proving the model actually used a tool. | False-positive provider integration result | Hardened; requires at least one tool call and exact random fixture marker in final response |
| SEC-04 | The new canonical-path logic required independent validation through the actual child-process boundary and across multiple turns. | Regression or incomplete evidence could be missed by unit tests alone | Added deterministic local HTTP-provider, real subprocess, mutation, denial, and budget E2E coverage |

## Controls inspected

1. **Contract schema and policy compilation.** Strict input shape, bounded JSON, profile/tool intersection, immutable policy, read-only intent restrictions, Bash disabled in contract mode.
2. **Path authorization.** Root and nested sensitive-path deny patterns; dispatch-time checks; canonical path rechecks before built-in file I/O; no-follow and descriptor/handle-based file access.
3. **Mutation and interruption safety.** Durable transaction intent, baseline digest, no-retry recovery handshake, post-commit warnings, and parent readback of the exact canonical mutation path.
4. **Process and resource boundaries.** Provider key excluded from tool-subprocess payload, bounded tool calls and writes, cancellation/deadlines, and local subprocess cleanup.
5. **Handoff evidence.** Commit-time digest and host-observed final readback are reported separately from functionality. Declared success checks remain `not_run` unless independently executed.

## Tests and evidence

- CI #310 passed all nine Ubuntu/macOS/Windows × Python 3.10–3.12 jobs for commit `be3c9f5`. That is historical baseline evidence only.
- New commits add contract JSON edge-case tests and local scripted OpenAI-compatible provider E2E tests using **real provider and tool subprocesses**, file commits, denied access, mutation budgets, and structured handoff.
- Opt-in real-API smoke test: `tests/test_live_deepseek_api.py`. It requires `DEEPSEEK_LIVE_TEST=1` and an independently configured `DEEPSEEK_API_KEY`; PR CI intentionally does not supply a credential.
- **The latest-head CI and the live DeepSeek API gate remain pending until independently recorded as passed.** Never infer either from an older green run or a skipped test.

## Residual risks / explicit limitations

- Same-UID untrusted code or an attacker that compromises the tool process is **not** confined by an OS-level sandbox. Filesystem race defense is layered, not a proof of absolute containment.
- A final SHA-256 readback is a point-in-time observation, not a signed attestation or guarantee that contents remain unchanged.
- User-declared `success_checks`, `must_not_change`, and `expected_outputs` are not automatically verified by this feature. The handoff labels these limits; it does not claim production correctness.
- Dependency vulnerability scanning is not source-code penetration testing. No claim is made that all vulnerabilities are absent.
- The real production DeepSeek endpoint, latency, function-calling semantics, and live model behavior cannot be validated without the separate credential-gated smoke test.

## Merge gates

1. Re-run the full 9/9 CI matrix against the **exact current PR head**, including source checks and dependency auditing.
2. Run the optional real DeepSeek API smoke test in a trusted environment, recording only model, endpoint, timestamp, and pass/fail—not headers, secrets, or raw model responses.
3. Obtain a separate security/maintainer review for changes with durable file mutation and recovery semantics.
4. Keep the PR in Draft until the above conditions and expected outputs have been reviewed; do not merge it based solely on this document.

This is a traceable **code-level audit record**, not an assertion of third-party independence, formal verification, or comprehensive adversarial penetration testing.
