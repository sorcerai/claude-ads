---
name: ads-report
description: "Render Markdown, HTML, or PDF paid-advertising reports from a validated Claude Ads JSON run bundle. Use for ads report, client report, audit PDF, executive audience reporting, or exporting prior audit and plan results."
---

# Render Paid Media Reports

1. Accept one validated `ReportBundle` v2.0.0 as the canonical source; never
   loose worker prose or format-specific aggregates.
2. Confirm run completeness, evidence coverage, privacy class, branding choice,
   requested audience, and output formats.
3. Render Markdown, HTML, and PDF from that same canonical bundle and deterministic
   templates.
4. Preserve every typed `EvidenceRecord` exactly in every format. Do not reduce
   records to citation strings or omit provenance, null fields, source binding,
   exact observation digests (`source_id` as canonical `sha256:<64 lowercase hex>`
   matching `sha256`), or measurement windows.
5. Preserve the full `MeasurementContext`, rendering
   `missing_fields` and `unsupported_fields` as separate disclosures, even when
   either array is empty.
6. Preserve findings, confidence, contradictions, missing inputs, recovery hints,
   actions, owners, and measurement windows; do not flatten or infer omitted
   values.
7. Render every supplied category and all category fields and counts:
   `category`, `category_weight`, `health_score`, `evidence_coverage`,
   `applicable_controls`, `known_controls`, `passed_controls`,
   `failed_controls`, and `unknown_controls`. Preserve the count invariants and
   reconcile the complete category set with canonical scoring against the packaged
   immutable control registry (or explicit root override fail-closed; canonical/package
   byte equality is release-controlled).
8. Run structural checks, then visual/layout checks for HTML and PDF.
9. Write report artifacts atomically inside the existing run directory. The
   conductor owns run and artifact registration and manifest updates; the report
   writer MUST NOT update the run manifest.

Do not include credentials, customer lists, private paths, raw research, internal
agent transcripts, or promotional copy unless the operator explicitly enabled
branding. Never hide partial or insufficient-evidence status in an executive
summary.
