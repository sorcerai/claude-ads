# ARCH.md — Construction blueprint

> **For builder agents.** Short by design: negatives (forbidden edges/patterns)
> don't rot; positive specs do. Inject into agent context at session start. A
> change that breaks one of these is DRIFT, not a fix — stop and update this
> file (with a reason + beads issue) first.

## What this is (2-line positive anchor)

A deterministic, source-grounded paid-media operating system across 12 advertising platforms,
turning authorized exports and reads into normalized observations, findings, and client-ready reports.

## Negative invariants (forbidden — breaking one is drift, not a fix)

1. `claude_ads_core` must not perform network I/O, background HTTP fetches, or write outside configured output paths (innermost read-only core ring).
2. Parallel workers must not write or mutate the canonical report bundle directly (single conductor-writer ownership rule).
3. Account-mutation operations must not execute without an exact before/after diff, explicit approval, idempotency key, verification window, and rollback procedure.
4. Missing, unsupported, or unobserved fields must never be converted into zeroes, passes, or artificial health scores (fail-closed truth integrity).
5. Platform, policy, and API assertions must not claim current behavior without dated, verified entries in `claim-ledger.json` and `source-ledger.json`.
6. Credentials, tokens, client data, and raw private research must never be stored in repository manifests, commits, or test fixtures.
7. Disabled scoring profiles must yield no health score (`health_score: null`, `status: insufficient_evidence`).
8. Successor consolidation must preserve pure read-only core boundaries, strict secret transport, and complete mutation disabling (see `control-plane/CONSOLIDATION_SEAM.md`).

## When this file is wrong

If a task genuinely requires breaking an invariant, update THIS file first (with
a beads issue + reason), then change the code. A silent violation is the exact
late-caught drift this file exists to prevent.

