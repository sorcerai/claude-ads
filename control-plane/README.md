# Claude Ads Control Plane

This directory is the public-safe contract layer for Claude Ads v2. It records
what the product is allowed to claim, what it can actually do, which evidence
supports it, how work is coordinated, and what must pass before release.

The design is a clean-room synthesis of source-first research discipline,
capability honesty, progressive disclosure, reversible operations, and
fresh-context verification. It contains no copied private prompts, captured
system text, account exports, credentials, or raw private corpus.

## Contracts

- `PRODUCT_BOUNDARIES.md`: buyer, promise, supported workflows, and explicit
  non-promises.
- `PUBLISHING_POLICY.md`: private/public classification and publication gate.
- `RELEASE_REQUIREMENTS.md`: maturity, testing, security, packaging, and remote
  CI requirements.
- `claude_ads_core/schemas/v1/`, `v2/`, and `v3/`: canonical JSON Schema
  Draft 2020-12 resources. Historical v1 lifecycle and workflow artifacts
  remain strict. Current lifecycle, setup profile, run manifest, account
  snapshot, and finding contracts use v2; current report bundles use v3.
- `schemas/independent-review-evidence.schema.json` and
  `schemas/review-trust-bundle.schema.json`: externally signed release-review
  contracts; repository templates cannot satisfy the gate.
- `manifests/`: current product, evidence, capability, safety, orchestration,
  maturity, and ecosystem-review state.
- `manifests/data-lifecycle-policy.json`: versioned classification, minimum
  retention, encryption, access, deletion-verification, and incident rules. The
  matching schema is `schemas/data-lifecycle-policy.schema.json`. Current
  per-run lifecycle records use `claude_ads_core/schemas/v2/data-lifecycle.schema.json`;
  historical v1 records retain their strict v1 contract. Current v2 records
  report encryption as unknown, retention as unassigned when no deadline is
  set, and deletion as pending until verified. Non-public persistence is
  refused without independently authenticated encryption and scheduler proof.
  Public-classified setup and audit output still requires a validated private
  directory (POSIX mode 0700 or a restrictive Windows ACL). A permissive
  preexisting file may be replaced atomically with a private one inside that
  directory; permissive parents and links fail closed.
- `manifests/control-registry.json` and `manifests/scoring-profiles.json`: the
  exhaustive typed audit catalog and fail-closed platform health state. A named
  check is not scoreable unless its versioned profile is enabled.

Schema `$id` values use stable `urn:ai-marketing-hub:claude-ads:schema:*`
identifiers. They are identifiers, not network locations. The tracked files in
`claude_ads_core/schemas/v1/`, `claude_ads_core/schemas/v2/`,
`claude_ads_core/schemas/v3/`, `control-plane/schemas/`, and `evals/schemas/`
are the canonical validation resources.

## Doctrine

1. No source, no current claim.
2. No implementation, fixture, and test, no capability claim.
3. No approval and rollback, no account mutation.
4. No independent verification path, no release.
5. Staleness, security failures, or broken evaluations demote maturity.

Manifests are release inputs, not marketing copy. A planned capability must be
marked `declared` or `disabled`; only verified behavior may be marked
`fixture-verified` or `live-verified`.

The dependency-free core also validates historical strict v1 workflow
artifacts for setup, brand, planning, creative/copy, generation, monitoring,
experiments, and mutation plans. Current setup profiles, lifecycle records,
and run manifests use v2; current report bundles use v3. These contracts
establish structure only; they do not prove source truth, platform eligibility,
provider availability, account authority, or approval.
File-backed orchestration uses immutable run, task, result, and artifact-only gate
packets. Reruns require an explicit `supersedes` chain.

The current catalog is operational for discovery findings, not account-health
grading. All twelve scoring profiles are disabled until per-control source
support, severity decisions, typed inputs, weights, and regression evidence are
approved together.

Run `python scripts/release.py gate` with the exact canonical-model report,
external signed review directory, reviewer public-key trust bundle, implementation
principal list, and GitHub Actions run ID. The emitted assessment conforms to
`schemas/release-gate-report.schema.json` and fails closed when any input is
missing, stale, unsigned, mismatched, incomplete, or unsuccessful.

Canonical model evidence uses separate externally trusted runner and evaluator
keys. Supply the model trust bundle and implementation-principal exclusions via
the `CLAUDE_ADS_MODEL_EVAL_*_JSON` environment variables documented in
`RELEASE_REQUIREMENTS.md`; no repository file is a trust root.

Run `python -m claude_ads_core status --root . --as-of YYYY-MM-DD` for the
repository-artifact status, or replace `status` with `next` for exactly one
deterministically selected blocker. Both commands are offline and fail closed.
