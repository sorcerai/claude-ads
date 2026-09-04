# Successor Consolidation Seam

## Purpose

Defines the explicit architectural boundary and integration seam for consolidating
`sorcerai/claude-ads` into its successor project alongside `sorcerai/arcads-claude-code`
(AI creative generation).

## Core Seam Architecture

The consolidation boundary is kept deliberately loose and unidirectional:

1. **`claude_ads_core` remains pure and read-only**:
   - Zero network I/O, no runtime dependencies beyond the standard library.
   - Innermost computational ring: strictly deterministic normalization, validation, scoring, and contract generation.
   - Operates purely in-memory and outputs validated JSON data structures.

2. **I/O and Environment boundaries live in `scripts/` and CLI**:
   - All network fetching, Keychain secret retrieval, pagination, and file writing live outside `claude_ads_core`.
   - Outbound requests adhere to bounded timeouts, exponential backoff, rate limits, and egress sandbox rules.

3. **Creative generation is decoupled**:
   - Creative generation and asset synthesis live entirely on the `arcads` side.
   - `claude-ads` produces briefs, competitor observations, audit findings, and compliance specs, but does not synthesize creative assets or mutate campaign ads.

4. **Integration via canonical contracts**:
   - The successor project consumes `claude-ads` outputs via versioned schema artifacts (`orchestration-task`, `orchestration-result`, `competitor-observation`, `report-bundle`, `account-snapshot`).
   - Neither project imports internals from the other that presume stateful coupling.

## Explicitly Rejected Patterns

The following patterns from `arcads/meta_api.py` were reviewed and are explicitly **rejected** from the consolidated surface:

1. **Unprotected Secret Transport**:
   - *Rejected*: Sending credentials in GET query parameters or unvalidated POST bodies.
   - *Requirement*: Secrets must be managed via OS Keychain / secure credential stores and transferred only via authenticated request headers or secure POST payloads.

2. **Bare `requests.post` Without SSRF / Egress Safeguards**:
   - *Rejected*: Unbounded network requests without destination validation or timeout governance.
   - *Requirement*: All external egress must pass target host verification and enforced socket timeouts.

3. **Environment-Overridable `API_VERSION`**:
   - *Rejected*: Allowing runtime environment variables to override platform API versions arbitrarily.
   - *Requirement*: Platform API versions are explicitly locked and attested against source ledgers (e.g., Meta Graph API v26.0 for CLM-0210/0212/0216). Changing an API version requires verifying and updating ledger claims.

4. **Account Mutation / `create_ad` Write Path**:
   - *Rejected*: Importing unverified `create_ad` or mutation routines directly into the audit plane.
   - *Requirement*: Account mutations remain completely disabled across all twelve platforms in `claude-ads`. Importing write paths without passing the six-item mutation gate (before/after diff, explicit approval, idempotency key, verification window, audit record, rollback procedure) falsifies the capability manifest.
