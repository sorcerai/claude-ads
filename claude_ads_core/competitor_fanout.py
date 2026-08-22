"""Deterministic slice planning for competitor ad-research fanout.

The conductor dispatches one bounded worker per slice and owns the merged
artifact. This module only decides what the slices are and emits schema-valid
`orchestration-task` packets, so a rerun with the same inputs produces the same
task IDs and the supersedes chain stays meaningful.

It also owns the Meta Ad Library coverage rules, because `scripts/
fetch_ad_library.py` and the planner must agree on when a slice can return
nothing for reasons that are not evidence of absence.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "1.0.0"

# EU member states. Meta discloses non-political ads only where an ad reached the
# EU, so this set decides whether a commercial slice can return rows at all.
EU_COUNTRIES = frozenset(
    """AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK
    SI ES SE""".split()
)

# Meta's US special ad categories. The reference documents no geographic limit
# for them either way, so an empty result is unconfirmed coverage, not absence.
SPECIAL_AD_CATEGORIES = frozenset(
    {"EMPLOYMENT_ADS", "FINANCIAL_PRODUCTS_AND_SERVICES_ADS", "HOUSING_ADS"}
)

SOURCES: dict[str, dict[str, Any]] = {
    "meta-ad-library": {
        "label": "Meta Ad Library API",
        "secret_ref": "META_AD_LIBRARY_TOKEN",
        "tool": "scripts/fetch_ad_library.py",
        "evidence_policy": [
            "Query the official ads_archive endpoint only; never scrape the Ad Library UI.",
            "Record ad_snapshot_url, page_name, and capture date for every observation.",
            "Report an empty in-scope result as zero observations, never as absence of ads.",
        ],
    },
    "google-ads-transparency": {
        "label": "Google Ads Transparency Center",
        "secret_ref": None,
        "tool": "WebFetch",
        "evidence_policy": [
            "Use the public Transparency Center only; respect robots and access controls.",
            "Record advertiser identity, format, capture date, and source URL.",
        ],
    },
    "serp-paid": {
        "label": "Paid SERP competitor comparison",
        "secret_ref": None,
        "tool": "mcp__search-ops__find_serp_competitors",
        "evidence_policy": [
            "Request resultTypes ['paid'] and label output as a third-party estimate.",
            "Never present provider visibility estimates as competitor account facts.",
        ],
    },
}


def slugify(value: str) -> str:
    """Reduce a free-text name to the workflow-common id character set."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-")
    if not slug:
        raise ValueError(f"cannot derive an id from {value!r}")
    return slug.lower()


def meta_coverage_note(ad_type: str, countries: Iterable[str]) -> str | None:
    """Explain why a Meta slice may return nothing, or None when fully in scope.

    An unexplained empty list reads as "this competitor runs no ads", which is the
    single most expensive misreading in competitor work.
    """
    upper = [str(country).upper() for country in countries]
    if ad_type == "POLITICAL_AND_ISSUE_ADS":
        return None
    if any(country in EU_COUNTRIES for country in upper):
        return None

    scope = ",".join(upper)
    if ad_type in SPECIAL_AD_CATEGORIES:
        return (
            f"ad_type={ad_type} with no EU country in {scope}: the reference does not "
            "state whether special ad category disclosure extends outside the EU. "
            "Treat an empty result as unconfirmed coverage, not as absence of ads."
        )
    return (
        f"ad_type={ad_type} with no EU country in {scope}: Meta returns only social "
        "issue, election, and political ads outside the EU, so commercial results are "
        "expected to be empty. Add an EU country or use POLITICAL_AND_ISSUE_ADS."
    )


def plan_slices(
    *,
    run_id: str,
    competitors: Iterable[str],
    countries: Iterable[str],
    sources: Iterable[str],
    created_at: str,
    ad_type: str = "ALL",
) -> list[dict[str, Any]]:
    """Emit one orchestration-task packet per competitor x country x source.

    Slices are independent by construction: no packet declares depends_on, and
    each names a distinct single-writer destination, so no two workers can race
    the same file.
    """
    competitors = list(competitors)
    countries = list(countries)
    sources = list(sources)
    if not competitors or not countries or not sources:
        raise ValueError("competitors, countries, and sources must each be non-empty")

    unknown = sorted(set(sources) - set(SOURCES))
    if unknown:
        raise ValueError(f"unknown source(s): {', '.join(unknown)}")

    tasks: list[dict[str, Any]] = []
    for source in sources:
        profile = SOURCES[source]
        for competitor in competitors:
            for country in countries:
                task_id = f"{run_id}.{slugify(source)}.{slugify(competitor)}.{country.upper()}"
                scope = [
                    f"Competitor: {competitor}",
                    f"Country: {country.upper()}",
                    f"Source: {profile['label']}",
                ]
                recovery = [
                    "Return status blocked with the missing capability; do not substitute another source.",
                ]
                if profile["secret_ref"]:
                    recovery.append(
                        f"If {profile['secret_ref']} is absent, return needs_input rather than an empty result."
                    )
                if source == "meta-ad-library":
                    note = meta_coverage_note(ad_type, [country])
                    if note:
                        scope.append(f"Coverage limit: {note}")

                tasks.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "artifact_type": "orchestration-task",
                        "task_id": task_id,
                        "run_id": run_id,
                        "role": "research-worker",
                        "objective": (
                            f"Collect observable paid-ad evidence for {competitor} in "
                            f"{country.upper()} from {profile['label']}."
                        ),
                        "scope": scope,
                        "exclusions": [
                            "Do not infer spend, audience, or performance from observed creative.",
                            "Do not copy protected creative text beyond short quotation.",
                            "Do not write the merged competitor artifact.",
                        ],
                        "evidence_policy": list(profile["evidence_policy"]),
                        "privacy_class": "public",
                        "mutation_authority": "none",
                        "inputs": [],
                        "output_contract": {
                            "contract": "orchestration-result",
                            "destination": f"results/{task_id}.json",
                            "single_writer": True,
                        },
                        "verification": [
                            "Every observation carries a capture date and source URL.",
                            "Observations are separated from inferences.",
                            "An empty result states its coverage reason.",
                        ],
                        "recovery": recovery,
                        "depends_on": [],
                        "created_at": created_at,
                        "status": "queued",
                    }
                )
    return tasks


def coverage_summary(results: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Fold worker results into dispatch counts the conductor must disclose.

    A partial fanout that reads as complete is the failure this guards against.
    """
    counts = {"ok": 0, "needs_input": 0, "blocked": 0, "failed": 0}
    for result in results:
        status = str(result.get("status", "failed"))
        counts[status] = counts.get(status, 0) + 1
    total = sum(counts.values())
    return {
        "slices": total,
        "by_status": counts,
        "complete": total > 0 and counts["ok"] == total,
    }
