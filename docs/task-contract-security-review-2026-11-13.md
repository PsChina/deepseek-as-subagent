# Task Contract — Code-Level Security Review

Reviewed: 2026-11-13; updated: 2026-11-14  
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
| SEC-05 | The direct Read path rejected pre-existing hard links, but the separate Grep descriptor reader did not apply the same check; both readers also needed a post-read link-count recheck to detect changes during I/O. | A workspace hard link could expose content from a file also linked outside the workspace. | Fixed with shared descriptor-level validation before and after Read/Grep; added pre-existing-link and during-read race regression tests |

## Controls inspected

1. **Contract schema and policy compilation.** Strict input shape, bounded JSON, profile/tool intersection, immutable policy, read-only intent restrictions, Bash disabled in contract mode.
2. **Path authorization and file identity.** Root and nested sensitive-path deny patterns; dispatch-time checks; canonical path rechecks before built-in file I/O; no-follow and descriptor/handle-based file access; Read/Grep reject multi-hardlinked files and recheck link count plus file identity after reading.
3. **Mutation and interruption safety.** Durable transaction intent, baseline digest, no-retry recovery handshake, post-commit warnings, and parent readback of the exact canonical mutation path.
4. **Process and resource boundaries.** Provider key excluded from tool-subprocess payload, bounded tool calls and writes, cancellation/deadlines, and local subprocess cleanup.
5. **Handoff evidence.** Commit-time digest and host-observed final readback are reported separately from functionality. Declared success checks remain `not_run` unless independently executed.

## Tests and evidence

- CI #323 passed all nine Ubuntu/macOS/Windows × Python 3.10–3.12 jobs for the pre-hardlink-hardening revision `23535e47`.
- **CI #336 passed all 9/9 jobs on source revision `14469f1853a17686ff673541e278652788c656af`.** Each job ran 620 tests; platform-specific skips were 19 on Ubuntu, 18 on macOS, and 115 on Windows. All nine dependency-audit steps reported `No known vulnerabilities found`.
- **CI #337 passed all 9/9 jobs on documentation revision `8d57a225f0867ce7c559baeb06c53e232e5d8c1f`.** Runtime source was unchanged from the tested hard-link fix in CI #336.
- The 620-test suite includes contract JSON edge cases, local scripted OpenAI-compatible provider E2E tests using real provider and tool subprocesses, file commits, denied access, mutation budgets, structured handoff, and hard-link regressions for Read/Grep.
- Opt-in real-API smoke test: `tests/test_live_deepseek_api.py`. It requires `DEEPSEEK_LIVE_TEST=1` and an independently configured `DEEPSEEK_API_KEY`; PR CI intentionally does not supply a credential. **A live production API result has not been recorded.**
- The security review remains a code-level review, not an independent third-party penetration test. Keep live provider validation and a separate maintainer review as merge gates.

## Residual risks / explicit limitations

- Same-UID untrusted code or an attacker that compromises the tool process is **not** confined by an OS-level sandbox. Filesystem race defense is layered, not a proof of absolute containment.
- A final SHA-256 readback is a point-in-time observation, not a signed attestation or guarantee that contents remain unchanged.
- User-declared `success_checks`, `must_not_change`, and `expected_outputs` are not automatically verified by this feature. The handoff labels these limits; it does not claim production correctness.
- Dependency vulnerability scanning is not source-code penetration testing. No claim is made that all vulnerabilities are absent.
- The real production DeepSeek endpoint, latency, function-calling semantics, and live model behavior cannot be validated without the separate credential-gated smoke test.

## Merge gates

1. Re-run the full 9/9 CI matrix against the exact current PR head after this documentation update, including source checks and dependency auditing.
2. Run the optional real DeepSeek API smoke test in a trusted environment, recording only model, endpoint, timestamp, and pass/fail—not headers, secrets, or raw model responses.
3. Obtain a separate security/maintainer review for changes with durable file mutation and recovery semantics.
4. Keep the PR in Draft until the above conditions and expected outputs have been reviewed; do not merge it based solely on this document.

This is a traceable **code-level audit record**, not an assertion of third-party independence, formal verification, or comprehensive adversarial penetration testing.
