# Claude Ads Audit Report

> Run completeness: **Partial** · Evidence status: **Provisional**

## Run summary

- Run ID: fixture\-run\-20260711\-001
- Started: 2026\-07\-11T16:00:00Z
- Platform: Google
- Account: Northstar Demo
- Window: 2026\-06\-01 to 2026\-06\-30
- Privacy class: Internal

## Measurement context

- Profile ID: google\-fixture\-health\-v1
- Source format: sanitized\-report\-fixture
- Source IDs: official\-google\-help, sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
- Report grain: date, campaign\_id
- Timezone: None supplied
- Currency: USD
- Conversion definition: primary\_conversion\_action
- Conversion actions: primary\_conversion\_action
- Attribution model: None supplied
- Click attribution window: None supplied
- View attribution window: None supplied
- Counting behavior: None supplied
- As of: 2026\-06\-30
- Data finalization: Unknown
- Modeled-data treatment: Unknown
- Missing fields: attribution\_model, click\_attribution\_window, counting\_behavior, data\_finalization, modeled\_data\_treatment, timezone, view\_attribution\_window
- Unsupported fields: budget, creative\_id, creative\_name

## Decision status

- Run completeness: **Partial**
- Evidence status: **Provisional**
- Health score: **37.50 / 100**
- Evidence coverage: **61.54%**

> WARNING: Required work did not complete; this report must not be presented as a complete audit.

> WARNING: The health score is provisional because evidence coverage is below the normal threshold.

## Category health

- **Creative:** Not scored; evidence 0.00%
- **Policy:** 100.00 / 100; evidence 100.00%
- **Tracking:** 0.00 / 100; evidence 100.00%

## Findings

### [UNKNOWN] G\-CREATIVE\-001 — Creative

- Severity: Critical
- Confidence: None
- Source classification: Evidence Based

**Observation:** Creative rows were not included in the export\.

**Diagnosis:** Creative coverage cannot be assessed from this run\.

**Recommended action:** Export asset\-level creative performance for the same date window\.

**Evidence:**

No evidence was supplied.

### [PASS] G\-POLICY\-001 — Policy

- Severity: High
- Confidence: Medium
- Source classification: Evidence Based

**Observation:** Included campaign rows are eligible\.

**Diagnosis:** No policy restriction is visible in the supplied rows\.

**Recommended action:** Continue monitoring policy status\.

**Evidence:**

1.

        {"evidence_id":"evidence-google-policy-001","input_field":"policy_status","locator":"input:sanitized-google-export.csv","observation_ref":null,"observed_at":"2026-07-11T16:00:00Z","proof_kind":"observation","query_id":null,"redacted_value":"eligible","report_grain":["date","campaign_id"],"report_id":null,"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_id":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","window":{"end":"2026-06-30","start":"2026-06-01"}}

### [FAIL] G\-TRACK\-001 — Tracking

- Severity: Critical
- Confidence: High
- Source classification: Evidence Based

**Observation:** The sanitized export marks the primary conversion action inactive\.

**Diagnosis:** Optimization cannot use the intended primary outcome\.

**Recommended action:** Have the measurement owner verify the conversion action before bid changes\.

**Evidence:**

1.

        {"evidence_id":"evidence-google-tracking-001","input_field":"primary_conversion_status","locator":"input:sanitized-google-export.csv","observation_ref":null,"observed_at":"2026-07-11T16:00:00Z","proof_kind":"observation","query_id":null,"redacted_value":"inactive","report_grain":["date","campaign_id"],"report_id":null,"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_id":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","window":{"end":"2026-06-30","start":"2026-06-01"}}

## Contradictions

- Campaign eligibility is present, but conversion readiness is not\. \(Source: sanitized export; Status: unresolved\)

## Prioritized actions

1. Verify and repair the primary conversion action\. \(Confidence: high; Owner: measurement owner; Success Measure: A controlled test conversion appears in the platform\.; Timing: before bid changes\)
2. Supply asset\-level creative performance\. \(Confidence: high; Owner: media buyer; Success Measure: Creative controls have sufficient evidence\.; Timing: next audit run\)

---

Generated deterministically from ReportBundle JSON. Scores were recomputed from the supplied control registry and verified against this ReportBundle before rendering.
