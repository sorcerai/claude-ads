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

# Meta's US special ad categories. Live verification on 2026-08-23 showed they
# follow the same EU-reach disclosure rule as ALL, so they need no separate branch.
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
    """Explain what a non-EU query does and does not cover, or None when full.

    Live verification on 2026-08-23 corrected an earlier reading of the docs. A
    non-EU query does NOT return nothing: the archive discloses commercial ads
    that also reached the EU or UK, so a US search returns real rows, biased
    toward advertisers who also run EU/UK delivery. Special ad categories behave
    identically. Calling that "expected to be empty" was wrong in the more
    dangerous direction, since it invites an operator to skip the query or to
    read partial coverage as a complete picture.
    """
    upper = [str(country).upper() for country in countries]
    if ad_type == "POLITICAL_AND_ISSUE_ADS":
        return None
    if any(country in EU_COUNTRIES for country in upper):
        return None

    return (
        f"ad_type={ad_type} with no EU country in {','.join(upper)}: outside the EU the "
        "archive discloses commercial ads only where they also reached the EU or UK, plus "
        "social issue, election, and political ads. Expect real but partial results biased "
        "toward advertisers with EU/UK delivery; this is not a complete view of that market."
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


# Fields Meta populates only for political and issue ads. Present-but-absent here
# means "not disclosed for this ad", never "this advertiser spent nothing".
POLITICAL_ONLY_FIELDS = (
    "bylines",
    "currency",
    "spend",
    "impressions",
    "demographic_distribution",
    "delivery_by_region",
    "estimated_audience_size",
)

PROVENANCE = ("ad-library-api", "operator-supplied")


def normalize_archived_ads(
    ads: Iterable[Mapping[str, Any]],
    *,
    captured_at: str,
    provenance: str,
) -> list[dict[str, Any]]:
    """Fold ArchivedAd rows into one canonical competitor observation shape.

    Both supported routes land here: the API client's `ads` list, and rows an
    operator transcribed by hand from the public Ad Library. Downstream
    clustering and reporting therefore never branch on where evidence came from,
    only on how well attested it is.

    Creative text is advertiser-authored and stays under `untrusted_creative`, so
    no caller can mistake it for instructions or for verified claims.
    """
    if provenance not in PROVENANCE:
        raise ValueError(f"provenance must be one of {PROVENANCE}, got {provenance!r}")

    observations: list[dict[str, Any]] = []
    for ad in ads:
        ad_id = str(ad.get("id") or "").strip()
        if not ad_id:
            raise ValueError("every ArchivedAd row requires an id")

        disclosed = {key: ad[key] for key in POLITICAL_ONLY_FIELDS if key in ad}
        observations.append(
            {
                "observation_id": f"meta-ad-library.{ad_id}",
                "platform": "meta",
                "advertiser": ad.get("page_name"),
                "advertiser_page_id": ad.get("page_id"),
                "snapshot_url": ad.get("ad_snapshot_url"),
                "publisher_platforms": list(ad.get("publisher_platforms") or []),
                "languages": list(ad.get("languages") or []),
                "delivery_start": ad.get("ad_delivery_start_time"),
                "delivery_stop": ad.get("ad_delivery_stop_time") or None,
                "untrusted_creative": {
                    "bodies": list(ad.get("ad_creative_bodies") or []),
                    "titles": list(ad.get("ad_creative_link_titles") or []),
                    "descriptions": list(ad.get("ad_creative_link_descriptions") or []),
                    "captions": list(ad.get("ad_creative_link_captions") or []),
                },
                # Absent keys are undisclosed for this ad's category, not zero.
                "disclosed_political_metrics": disclosed or None,
                "captured_at": captured_at,
                "provenance": provenance,
            }
        )
    return observations


# Scripts whose letterforms are visually confusable with Latin. Mixing these into
# an otherwise Latin name is the signature of homoglyph impersonation. CJK,
# Arabic, Hebrew and similar are deliberately excluded: a name mixing them with
# Latin is ordinary multilingual branding, not evasion.
CONFUSABLE_SCRIPTS = ("CYRILLIC", "GREEK")


def _scripts(text: str) -> set[str]:
    import unicodedata

    return {
        unicodedata.name(ch, "").split(" ")[0]
        for ch in text
        if ch.isalpha() and unicodedata.name(ch, "")
    }


def mixed_script_advertisers(observations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Flag advertiser names that blend Latin with a confusable alphabet.

    Substituting Cyrillic or Greek lookalikes into a brand name renders
    identically to a human while defeating exact-match moderation and any
    keyword search an analyst runs. Observed live: eight advertisers spoofing a
    German television brand across 16% of a category's ads, drawing roughly six
    times the median reach of everyone else in the same result set.

    This is a signal for review, never a verdict. A name can mix scripts for
    innocent reasons, and confirming impersonation needs the creative, the
    landing destination, and the real brand's own advertising.
    """
    flagged: dict[str, dict[str, Any]] = {}
    for obs in observations:
        name = str(obs.get("advertiser") or "")
        if not name:
            continue
        found = _scripts(name)
        if "LATIN" not in found:
            continue
        confusable = sorted(s for s in found if s in CONFUSABLE_SCRIPTS)
        if not confusable:
            continue
        entry = flagged.setdefault(
            name, {"advertiser": name, "scripts": confusable, "ad_count": 0}
        )
        entry["ad_count"] += 1
    return sorted(flagged.values(), key=lambda e: -e["ad_count"])
