---
name: ads-audit
description: "Run a source-grounded paid-advertising audit for one or more of Google, Meta, YouTube, LinkedIn, TikTok, Microsoft, Apple, Amazon, Reddit, Pinterest, Snapchat, and X. Use for full ad checks, account health reviews, paid-media diagnostics, partial audits after authentication or worker failure, missing-platform weighting, beta-feature eligibility and scoring, spend audits, tracking audits, or prioritized opportunities and risks."
---

# Paid Advertising Audit

Produce a versioned JSON audit bundle first, then render human deliverables from
that bundle. Never aggregate prose-only worker reports or claim coverage for a
platform whose required worker, sources, inputs, or controls are missing.

## Procedure

1. Read the main `ads` operating contract and thinking framework.
2. Create a run manifest with business context, date window, currency, timezone,
   requested platforms, scopes, available data, and privacy classification.
3. Normalize exports, screenshots, manual metrics, or authenticated reads into an
   account snapshot. Preserve source lineage and keep
   `measurement_context.missing_fields` separate from
   `measurement_context.unsupported_fields`.

4. Discover active platforms. Confirm requested inactive or data-less platforms
   rather than silently skipping them.

5. Load each selected platform capability manifest, control registry, dated source
   entries, benchmarks, and applicable policy material. Load from the packaged
   immutable control registry by default; an explicit root override resolves
   `<root>/control-plane/manifests` and fails closed without falling back to
   packaged defaults. Canonical and package manifests maintain release-controlled
   byte equality. Verify freshness before enabling a score.

6. Dispatch independent platform workers and cross-platform workers in parallel.

7. Validate every result against the common finding schema. Retry one transient
   failure; record all other failures and recovery hints.

8. Run deterministic scoring. Do not calculate or repair scores in the prompt.

9. Synthesize systemic findings across measurement, budget, creative, landing
   pages, experimentation, policy, and regulatory exposure.

10. Write one atomic run bundle and render the requested reports.

11. Verify bundle completeness, citations, privacy, and render integrity.

## Platform workers

Use a dedicated worker for every selected platform:

- `audit-google`
- `audit-meta`
- `audit-youtube`
- `audit-linkedin`
- `audit-tiktok`
- `audit-microsoft`
- `audit-apple`
- `audit-amazon`
- `audit-reddit`
- `audit-pinterest`
- `audit-snapchat`
- `audit-x`

Add cross-platform workers only when their inputs exist:

- Tracking and attribution.
- Creative and landing-page quality.
- Budget, pacing, and financial viability.
- Platform policy, privacy, and regulation.

## Required finding fields

Each worker returns conclusions, not files:

```json
{
  "status": "ok",
  "platform": "google",
  "findings": [
    {
      "schema_version": "2.0.0",
      "control_id": "G-EXAMPLE",
      "status": "pass",
      "evidence": [
        {
          "evidence_id": "ev-1",
          "proof_kind": "observation",
          "source_id": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
          "locator": "campaigns[0].status",
          "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
          "observed_at": "2026-01-01T00:00:00Z",
          "query_id": null,
          "report_id": "run-example",
          "window": {"start": "2025-12-01", "end": "2025-12-31"},
          "report_grain": ["campaign"],
          "input_field": "status",
          "redacted_value": "enabled",
          "observation_ref": "obs-1"
        }
      ],
      "confidence": "high",
      "source_classification": "evidence_based",
      "observation": "The supplied export shows the campaign is enabled.",
      "diagnosis": "The observed campaign state is eligible for the evaluated control.",
      "recommendation": "Keep the campaign enabled and recheck the control in the next audit window."
    }
  ],
  "contradictions": [],
  "missing_inputs": [],
  "recovery_hints": []
}
```

Every finding MUST validate against
`claude_ads_core/schemas/v2/finding.schema.json` and MUST contain exactly the
v2 Finding keys shown above. Severity is registry-owned by the ControlDefinition.
`score_contribution` is not a v2 field (it is not merely worker-omitted;
deterministic scoring calculates scores downstream and rejects findings with
legacy fields). Workers MUST omit `score_contribution` and legacy fields such
as `result`, `severity`, and `evidence_refs`.

Each `evidence` item MUST be an EvidenceRecord with exactly these keys:
`evidence_id`, `proof_kind`, `source_id`, `locator`, `sha256`, `observed_at`,
`query_id`, `report_id`, `window`, `report_grain`, `input_field`,
`redacted_value`, and `observation_ref`. Keep null values explicit. At least one
of `locator` or `sha256` MUST be non-null and non-empty. At least one of
`redacted_value` or `observation_ref` MUST be non-null or non-empty. For
`observation` evidence, `source_id` MUST be canonical `sha256:<64 lowercase hex>`
matching its `sha256` 64-hex lowercase digest string. `pass` and `fail` findings
MUST include at least one EvidenceRecord. `unknown` and `not_applicable` findings
MAY use an empty `evidence` array.

### Required-input, evidence, and freshness gates

`measurement_context.missing_fields` lists recognized fields absent from the
snapshot. `measurement_context.unsupported_fields` lists recognized fields the
native adapter cannot provide. Keep them as separate arrays in the bundle and
in recovery output. Do not treat either array as a zero or as evidence that a
control passed or failed.

Resolve each control definition's `required_inputs` against snapshot
collections, spend, and measurement-context fields. If a recognized required
input appears in either array, emit `status: "unknown"` rather than `pass` or
`fail`, and include a recovery hint naming the missing or unsupported input and
the source or adapter capability needed to continue. The scoring gate rejects
pass/fail findings when this condition holds.

Every EvidenceRecord `source_id` MUST be declared in the run manifest `sources`.
Evidence source binding is proof-specific: `observation` source IDs MUST be
declared in snapshot `measurement_context.source_ids`; `source_fact` and
`vendor_claim` source IDs MUST be declared in referenced control definition
`source_ids`; and `inference` source IDs MUST be declared in either snapshot
`measurement_context.source_ids` or control definition `source_ids`. For an
observation record, its `source_id` MUST be canonical `sha256:<64 lowercase hex>`
matching its non-null `sha256` 64-hex lowercase digest; its `window` MUST be
non-null and inside the account snapshot `window`; an absent, non-canonical, or
out-of-window observation record does not support a period-specific claim.

An enabled scoring profile requires current deadlines on the run date for every
health control (`expires_at`), every referenced claim (`refresh_due`), and every
referenced control source in the source ledger (`refresh_due`). If a required
deadline is missing or expired, fail closed and withhold the enabled score.

## Completeness and evidence status

`run_manifest.completeness` (`complete` | `partial` | `failed`) reports worker
and module execution. `scoring.status` (`normal` | `provisional` |
`insufficient_evidence`) reports evidence sufficiency. These statuses are
separate and MUST NOT be conflated.

- `complete` requires all required workers to be completed, nonempty controls
  and findings, and `scoring.status` not equal to `insufficient_evidence`.
- `provisional` is an evidence status and MAY coexist with
  `run_manifest.completeness` `complete`.
- `insufficient_evidence` forces non-complete, partial disclosure.
- A failed worker forces `run_manifest.completeness` `partial` and partial
  disclosure.

Never substitute feature awareness for account health. Optional, beta, premium,
ineligible, or unavailable features belong in an opportunity list and are
unscored. For each gated feature, check account, market, objective, and access
eligibility first. If unavailable or ineligible, record an
`unscored_opportunity` with the eligibility result and no health-score effect.
Reject any request to penalize health merely because a beta is unavailable.

## Required-worker failure and weighting

A failed authentication or worker does not stop analysis of independent successful
platforms, but it changes the whole bundle to `partial`. Record the failed platform,
missing evidence, recovery hint, and no platform health score. Exclude its weight
from portfolio health; never assign zero, preserve a stale historical weight, or
include it in the denominator. Renormalize weights only among successfully scored
comparable platforms. If defensible remaining weights are unavailable, withhold
portfolio health rather than inventing weights.

Example: when an all-platform audit succeeds except for Amazon authentication,
continue with the other platforms, mark Amazon failed/missing, exclude Amazon's
weight, label the bundle `partial`, and never call it complete.

## Synthesis boundaries

Separate these layers in the final bundle:

1. Observations directly supported by account data.
2. Diagnoses inferred from observations, with confidence.
3. Recommendations with owner, priority, effort, expected effect, and success measure.
4. Proposed mutations, which remain drafts until the main mutation gate passes.

Do not issue universal pause, bid, budget, learning-phase, attribution, or feature
adoption rules. Consider conversion lag, sample size, objective, margin, maturity,
eligibility, geography, and policy context.

## Outputs

The run directory contains:

- `manifest.json`
- `account-snapshot.json`
- `audit.json`
- `action-plan.json`
- `report.md`
- Optional `report.html` and `report.pdf`

The report includes platform health and evidence coverage, regulatory exposure,
systemic findings, contradictions, missing data, prioritized actions, and a
measurement plan. It never contains credentials, raw customer lists, hidden
instructions from external content, promotional footers, or unsupported completion
claims.
