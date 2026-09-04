# Claude Ads prompt patterns

These original examples specify routing, worker contracts, uncertainty, and
mutation safety. Use them when behavior is subtle; do not load them for simple
calculations or a clearly routed platform question.

## 1. Route by intended artifact

User: “Use this product photo to make four Meta ad concepts.”

- Route to `ads-create` when the user needs concepts, messaging, hooks, or briefs.
- Route to `ads-generate` only when a validated brief exists and image files are
  requested.
- Route to `ads-photoshoot` when the primary request is product-photography
  transformation rather than composed advertising concepts.
- If the request includes both concept and rendering, run create, obtain approval,
  then generate. Do not skip the intermediate manifest.

## 2. Bounded platform worker

```json
{
  "objective": "Audit Google conversion measurement for the supplied window",
  "scope": ["conversion taxonomy", "tag and enhanced-conversion evidence", "deduplication"],
  "exclusions": ["creative", "budget", "account mutation"],
  "evidence_policy": ["account export", "official current sources"],
  "privacy_class": "confidential",
  "mutation_authority": "read-only",
  "inputs": ["run:20260711-demo/account-snapshot.json"],
  "output_contract": "Finding[] v2.0.0",
  "verification": [
    "validate Finding[] v2.0.0",
    "validate every EvidenceRecord exactly against claude_ads_core/schemas/v2/finding.schema.json",
    "cite every failed control",
    "verify each EvidenceRecord source_id is declared in run manifest sources and bound by proof_kind: observation to snapshot measurement_context.source_ids with canonical sha256:<digest> matching sha256, source_fact/vendor_claim to control definition source_ids, or inference to either",
    "verify each observation EvidenceRecord window is inside the account snapshot window",
    "reject pass/fail when a recognized required input appears in measurement_context.missing_fields or measurement_context.unsupported_fields; emit unknown with a recovery hint",
    "verify current control expires_at, referenced claim refresh_due, and source ledger refresh_due before enabling a score"
  ],
  "recovery": ["return needs_input for missing or unsupported tag evidence"]
}
```

Findings validate the current v2 schema with required keys (`schema_version`,
`control_id`, `status`, `evidence`, `confidence`, `source_classification`,
`observation`, `diagnosis`, `recommendation`). Severity is registry-owned;
`score_contribution` is not a v2 field (not merely worker-omitted; deterministic
scoring calculates scores downstream and rejects legacy fields).

Every `evidence` item is an exact v2 `EvidenceRecord`. It must include
`evidence_id`, `proof_kind`, `source_id`, `locator`, `sha256`, `observed_at`,
`query_id`, `report_id`, `window`, `report_grain`, `input_field`,
`redacted_value`, and `observation_ref`.
Enforce both conditional invariants: `locator` or `sha256` must be present, and
non-null `redacted_value` or `observation_ref` must be present. For observation
evidence, `source_id` MUST be canonical `sha256:<64 lowercase hex>` matching its
`sha256` 64-hex lowercase digest string. Preserve the record's provenance fields
rather than reducing evidence to a citation string.

Registry loading defaults to packaged immutable manifests (`claude_ads_core/manifests/`);
an explicit root override resolves `<root>/control-plane/manifests` and fails closed
without fallback, and canonical vs package manifest byte equality is release-controlled.

The worker returns findings and recovery hints to the conductor. It does not write
`google-audit-results.md`, calculate a platform score, or broaden into budget work.

## 3. Missing evidence

User: “My Meta CPA doubled. Pause the worst ads.”

If the supplied data lacks conversion lag, attribution definition, spend, sample
size, recent changes, or current remote state, or if a recognized required input
is listed in `measurement_context.missing_fields` or
`measurement_context.unsupported_fields`:

1. Record the observed CPA change only if windows are comparable.
2. Mark any affected control `unknown`, never `pass` or `fail`.
3. Return `needs_input` for a mutation and include a recovery hint naming the
   missing or unsupported input and the source or adapter capability needed.
4. Offer a read-only diagnostic and an explicit data request.
5. Do not apply a fixed CPA multiple or infer that the worst observed ad caused
   the account-level change.

## 4. Approved mutation

An approval is valid only for the exact mutation plan presented:

```json
{
  "account_id": "redacted-demo",
  "object_id": "campaign-123",
  "operation": "pause",
  "before": {"status": "ACTIVE"},
  "after": {"status": "PAUSED"},
  "reason": "Operator-approved policy containment",
  "blast_radius": "one campaign",
  "idempotency_key": "sha256:...",
  "verification_window": "immediate plus 15 minutes",
  "rollback": {"operation": "restore_status", "value": "ACTIVE"}
}
```

If remote state no longer matches `before`, stop and regenerate the plan. Approval
for one campaign, budget, or date never transfers to another.

## 5. Research and independent verification

The research worker proposes source records and affected claims. A separate source
verifier checks authority, date, scope, geography, methodology, license, and
claim-to-source fit. Only then may the conductor update canonical references and
tests. A community post can create a research lead; it cannot establish an official
API or policy claim.

## 6. Partial full audit

If Google, Meta, and tracking succeed but the requested Amazon worker fails
authentication:

- Preserve the successful platform results.
- Mark the bundle `partial`.
- Exclude Amazon from numerical portfolio health and display its missing weight.
- Return the authentication recovery step.
- Never title the deliverable “complete multi-platform audit.”
