<p align="center">
  <img src="assets/banner.svg" alt="Claude Ads, Claude-first paid-media operations across twelve advertising platforms" width="100%">
</p>

# Claude Ads

Claude-first, portable paid-media operations for agencies, consultants, and
in-house performance teams.

Claude Ads turns authorized exports or account reads into source-grounded
audits, plans, creative workflows, experiments, monitoring, and reports. It is
read-only by default. Live changes stay disabled until the exact platform and
operation pass approval, idempotency, verification, audit, and rollback gates.

> [!NOTE]
> Claude Ads ships from two homes: the **public release** at
> [`AgriciDaniel/claude-ads`](https://github.com/AgriciDaniel/claude-ads)
> (MIT, no membership required) and the **community mirror** at
> `AI-Marketing-Hub/claude-ads`, where
> [AI Marketing Hub Pro](https://www.skool.com/ai-marketing-hub-pro) members
> get early access and direct collaboration.

<p align="center">
  <img src="assets/diagrams/how-it-works.svg" alt="Validated inputs flow through bounded workers into schema-valid findings and deterministic reports" width="100%">
</p>

## What it does

- Audits paid-media accounts with dated evidence and explicit confidence.
- Plans campaigns, channels, budgets, measurement, and experiments.
- Creates copy, image, video, and product-photo briefs and assets.
- Monitors pacing, delivery, tracking, fatigue, policy, and performance.
- Produces versioned JSON, then renders Markdown, HTML, and optional PDF.
- Drafts safe account changes without applying them by default.
- Reports missing data, stale sources, contradictions, and partial failures.

## Platforms

<p align="center">
  <img src="assets/diagrams/platform-coverage.svg" alt="Twelve first-class paid-advertising platform surfaces" width="100%">
</p>

| Segment | Platforms |
| --- | --- |
| Search, video, and social | Google Ads, Meta Ads, YouTube Ads, LinkedIn Ads, TikTok Ads, Microsoft Advertising, Reddit Ads, Snapchat Ads, X Ads |
| Commerce and retail media | Apple Ads, Amazon Ads, Pinterest Ads |

Each platform has a focused skill, audit worker, control reference, capability
declaration, and testable routing surface. The
[capability manifest](control-plane/manifests/capability-manifest.json) is the
authoritative record for live reads and writes.

## Commands

Standalone installs use `/ads`. Claude Code plugins are namespaced and use
`/claude-ads:ads`. Both load the same `ads/SKILL.md` contract.

| Command | Outcome |
| --- | --- |
| `/ads setup` | Create the client, account, KPI, privacy, and guardrail profile |
| `/ads audit [all\|platform\|scope]` | Run a complete or scoped evidence-backed audit |
| `/ads plan` | Build channel, campaign, budget, competitor, and measurement plans |
| `/ads create` | Produce copy, image, video, or product-photo assets |
| `/ads launch --draft` | Draft a campaign mutation plan without changing the account |
| `/ads monitor` | Review pacing, delivery, tracking, fatigue, policy, and performance |
| `/ads optimize --draft` | Draft evidence-backed optimization changes |
| `/ads experiment` | Design or read out a controlled test |
| `/ads report` | Render a validated JSON run bundle |
| `/ads research refresh` | Refresh platform, policy, API, benchmark, and ecosystem evidence |
| `/ads validate` | Validate contracts, runs, capabilities, maturity, or release readiness |
| `/ads status`, `/ads next` | Show current status and the highest-priority blocker |

Platform shortcuts such as `/ads google`, `/ads meta`, `/ads amazon`, and
`/ads reddit` route to the matching platform audit.

## Demo

<p align="center">
  <img src="assets/demo.gif" alt="Claude Ads command discovery inside Claude Code" width="800">
</p>

The GIF shows the original command-discovery experience. The v2 command table
above and the platform table are current and authoritative.

## Installation

Claude Code is the canonical runtime. Codex, Gemini, Cursor, Windsurf, Goose,
and compatible Agent Skills hosts can consume the same skill files where their
runtime supports them.

Prefer the host's native plugin flow or a tagged release archive with a verified
SHA-256 checksum. Never pipe a remote installer directly to a shell.

For Claude Code, the native plugin flow is:

```text
/plugin marketplace add AgriciDaniel/claude-ads
/plugin install claude-ads@ai-marketing-hub-claude-ads
```

Or install from a local clone of the public repository:

```bash
git clone https://github.com/AgriciDaniel/claude-ads.git
cd claude-ads
bash install.sh --source=local
```

Select another standalone host explicitly:

```bash
bash install.sh --target=codex --source=local
bash install.sh --target=gemini --source=local --no-deps
```

PowerShell uses the same managed ownership model:

```powershell
git clone https://github.com/AgriciDaniel/claude-ads.git
Set-Location claude-ads
.\install.ps1 -Source local
```

Managed dependencies support CPython 3.11 and 3.12 on the declared Linux,
macOS, and Windows wheel matrix. Unsupported interpreters fail before the
destination changes. Use `--no-deps` or `-NoDeps` for a skill-only install.

Browser capture requires an operator-installed Playwright browser payload. PDF
rendering requires the host's WeasyPrint and Pango system libraries. These are
documented in the
[external runtime dependency manifest](control-plane/manifests/external-runtime-dependencies.json).

Uninstall only manifest-owned files:

```bash
bash uninstall.sh --target=claude
```

The PowerShell equivalent is `uninstall.ps1`.

## Architecture

<p align="center">
  <img src="assets/diagrams/architecture.svg" alt="One conductor dispatches platform and cross-platform workers, then validates and renders a canonical JSON bundle" width="100%">
</p>

One conductor owns scope, policy, aggregation, and final artifacts. Workers
analyze bounded slices and return schema-valid findings. Required-worker failure
makes the run partial. It is never silently presented as a complete audit.

The canonical result is versioned JSON. Markdown, HTML, and PDF are renderings
of the same validated run bundle.

## Scoring and evidence

<p align="center">
  <img src="assets/diagrams/ads-health-score.svg" alt="Health and evidence coverage remain separate; scoring requires an approved platform profile" width="100%">
</p>

Controls use `pass`, `fail`, `unknown`, or `not_applicable`.

- Health, evidence coverage, regulatory exposure, and opportunities stay separate.
- Unknown controls reduce evidence coverage without changing known health.
- Coverage of 80% or more is graded, 60 to 79% is provisional, and below 60%
  is insufficient evidence.
- Optional, beta, premium, unavailable, and ineligible features stay unscored.
- A disabled or unapproved platform profile produces no health score.
- A failed platform is excluded from portfolio scoring and makes the run partial.

See the [scoring reference](ads/references/scoring-system.md) and production
implementation in `claude_ads_core/scoring.py`.

## Resumable Meta Ad Library collection

Use the official API for keyword discovery, then verify Page identities before
collecting advertiser archives. `KEYWORD_EXACT_PHRASE` reduces broad keyword
matches; it does not verify advertiser identity.

```bash
python scripts/fetch_ad_library.py --countries DE \
  --search-terms "example product" --search-type KEYWORD_EXACT_PHRASE \
  --max-pages 1 --output .claude-ads/runs/discovery.json

# Replace the example IDs with verified Facebook Page IDs.
python scripts/fetch_ad_library.py --countries DE \
  --search-page-ids 123456789,987654321 --ad-active-status ALL \
  --run-id advertiser-research --client-id example-client \
  --checkpoint .claude-ads/runs/advertisers.checkpoint.json \
  --output .claude-ads/runs/advertisers.json --max-pages 5
```

Repeat the advertiser command with `--resume` to continue. Keep the same client,
purpose, countries, Page IDs, creative fields, page size, and delivery filters.
An omitted run ID is recovered from the checkpoint on resume, including across
midnight; an explicitly conflicting ID is rejected. The page cap bounds the
whole invocation, continuing the current advertiser before starting the next.
It is not an archive completeness claim. The result exposes an overall `status`
and each advertiser's `exhausted`, `paginated`, `queued`, `quota-deferred`, or
`failed` state. Intentional page-cap completion exits zero with `paginated`;
failure or quota deferral exits nonzero. Exhausted resumes make no requests.
Cursor loops, authentication failures, and other non-retryable API errors remain
terminal across resume. Inspect and correct the cause before starting a new
checkpoint; do not repeatedly resume or overwrite the failed checkpoint.
Quota deferrals remain resumable after the recovery deadline.

Checkpoints store deduplicated normalized observations, compact per-page
provenance receipts, and opaque cursors, not raw API responses or token-bearing
pagination URLs. Keep checkpoints outside published source, and do not reuse an
existing checkpoint for a different query. Checkpoints and exports are written
atomically. File and directory synchronization is used on supported POSIX
filesystems; Windows does not offer the same directory-fsync durability guarantee.

Every request attempt, including retries and failed calls, uses the shared
`~/.claude-ads/ad-library-quota.json` ledger. Defaults are at most **60 attempts
per rolling hour**, at least **65 seconds apart**, and a **50% usage cutoff**.
`--hourly-limit` can only lower the local attempt ceiling. Provider recovery
signals can stop collection earlier; missing or malformed usage signals stop
further collection rather than imply available quota. Wait until the reported
`retry_at` before resuming. Do not delete the quota ledger to clear a deferral.

These are conservative local controls, **not Meta's published Ads Archive
quota**. Meta does not disclose the actual user call ceiling, and a user's
usage can span multiple apps. Other machines, older collectors, and unrelated
apps are not coordinated by this local ledger. Stop or coordinate those callers
before a deep collection; never claim an account-wide hourly guarantee from a
local counter. See Meta's [rate-limit documentation](https://developers.facebook.com/docs/graph-api/overview/rate-limiting/).

Country coverage and disclosed fields remain API-limited. Exhaustion means only
that this query's pagination ended, not that all advertising in a market was
retrieved. Keep EU and non-EU evidence separate; do not substitute one market for
another. The collector does not scrape the Ad Library UI or fetch creative media.

## Account safety

All adapters are read-only by default. Applying a change requires:

1. A tested and enabled capability for the exact operation.
2. Explicit account and object IDs.
3. A human-readable before and after diff with blast radius.
4. Owner approval within account-defined ceilings.
5. An idempotency key, audit destination, rollback, and verification window.
6. Verification that remote state still matches the mutation precondition.

Missing ceilings mean no write. Permanent deletion is not supported in v2.
Credentials belong in environment variables, an OS keychain, or an approved
secret manager. They never belong in the repository, profiles, reports, or logs.

## Evidence and release controls

The public-safe `control-plane/` records product boundaries, dated sources,
claims, capabilities, safety rules, privacy rules, ecosystem decisions, and
release requirements.

- No source means no current platform claim.
- No implementation, fixture, and test means no capability claim.
- No approval and rollback means no account mutation.
- No independent verification means no release.

See the [release requirements](control-plane/RELEASE_REQUIREMENTS.md) and
[publishing policy](control-plane/PUBLISHING_POLICY.md).

## Development

Create a virtual environment and run the complete suite:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements.lock
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
```

Useful focused checks:

```bash
python -m claude_ads_core --version
python -m claude_ads_core validate finding path/to/finding.json
bash -n install.sh uninstall.sh
```

## Repository map

```text
ads/                  main skill, interface metadata, and shared references
skills/               platform and lifecycle skills
agents/               platform, cross-platform, research, and verifier workers
claude_ads_core/      typed contracts, adapters, validation, and scoring
control-plane/        evidence, capability, safety, maturity, and release state
scripts/              browser, creative, reporting, and release helpers
evals/                routing and behavioral evaluation cases
tests/                deterministic, security, installer, and adapter tests
```

## Privacy

Client data, raw private research, captured prompts, credentials, account
exports, and agent transcripts must never enter Git history, reports, or release
archives. Keep credentials in environment variables, an OS keychain, or an
approved secret manager.

## License

Original Claude Ads code and documentation are available under the MIT License.
Third-party APIs, trademarks, documentation, and cited artifacts remain subject
to their own terms. Review the [source ledger](control-plane/manifests/source-ledger.json)
and [third-party notices](THIRD_PARTY_NOTICES.md) before importing external work.
