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
        "collection_mode": "automated",
        "evidence_policy": [
            "Query the official ads_archive endpoint only; never scrape the Ad Library UI.",
            "Record ad_snapshot_url, page_name, and capture date for every observation.",
            "Report an empty in-scope result as zero observations, never as absence of ads.",
        ],
    },
    "google-ads-transparency": {
        "label": "Google Ads Transparency Center",
        "secret_ref": None,
        "tool": None,
        "collection_mode": "operator-capture-only",
        "evidence_policy": [
            "A person opens the public Transparency Center and supplies the observations.",
            "Do not fetch, crawl, render, or reverse-engineer undocumented UI endpoints.",
            "Record the operator's query, advertiser identity, format, capture date, and source URL.",
        ],
    },
    "serp-paid": {
        "label": "Paid SERP competitor comparison",
        "secret_ref": None,
        "tool": "mcp__search-ops__find_serp_competitors",
        "collection_mode": "automated",
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


MAX_COMPETITORS = 10
MAX_COUNTRIES = 10
MAX_TOTAL_SLICES = 50


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
    seen_comp: set[str] = set()
    dedup_competitors: list[str] = []
    for c in competitors:
        c_str = str(c).strip()
        if c_str and c_str.lower() not in seen_comp:
            seen_comp.add(c_str.lower())
            dedup_competitors.append(c_str)

    seen_cntry: set[str] = set()
    dedup_countries: list[str] = []
    for c in countries:
        c_str = str(c).strip().upper()
        if c_str and c_str not in seen_cntry:
            seen_cntry.add(c_str)
            dedup_countries.append(c_str)

    sources = list(sources)
    if not dedup_competitors or not dedup_countries or not sources:
        raise ValueError("competitors, countries, and sources must each be non-empty")

    if len(dedup_competitors) > MAX_COMPETITORS:
        raise ValueError(
            f"competitors list exceeds maximum budget ({len(dedup_competitors)} > {MAX_COMPETITORS})"
        )
    if len(dedup_countries) > MAX_COUNTRIES:
        raise ValueError(
            f"countries list exceeds maximum budget ({len(dedup_countries)} > {MAX_COUNTRIES})"
        )

    unknown = sorted(set(sources) - set(SOURCES))
    if unknown:
        raise ValueError(f"unknown source(s): {', '.join(unknown)}")

    tasks: list[dict[str, Any]] = []
    for source in sources:
        profile = SOURCES[source]
        for competitor in dedup_competitors:
            for country in dedup_countries:
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

                if profile["collection_mode"] == "operator-capture-only":
                    scope.append(
                        "Operator capture required: this repository has no approved automated client "
                        "for the source."
                    )
                    recovery.append(
                        "Ask the operator to browse the public source manually and supply the observed "
                        "fields, query, capture date, and source URL."
                    )
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
    if len(tasks) > MAX_TOTAL_SLICES:
        raise ValueError(
            f"total planned slices exceed maximum budget ({len(tasks)} > {MAX_TOTAL_SLICES})"
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


# The archive appends the caller's credential to every ad_snapshot_url it
# returns. Today Meta sends the bare parameter name with no value, but that is
# Meta's choice on the day, not a contract — and these observations get embedded
# in other repositories, where a populated one would be committed. Strip it at
# the point the row becomes an observation, so no downstream caller has to
# remember. Found because adsinfra's committed-record guard test rejected an
# overlay that carried it through from here.
_CREDENTIAL_PARAM_RE = re.compile(r"([?&])access_token(=[^&]*)?(&|$)")


def _strip_snapshot_credential(url: Any) -> Any:
    """Return a snapshot URL with the credential parameter removed.

    Non-strings pass through untouched: a missing ad_snapshot_url is None, and
    turning that into "" would make an absent snapshot look like an empty one.
    """
    if not isinstance(url, str):
        return url
    return _CREDENTIAL_PARAM_RE.sub(
        lambda match: match.group(1) if match.group(3) == "&" else "", url
    )


def normalize_archived_ads(
    ads: Iterable[Mapping[str, Any]],
    *,
    captured_at: str,
    provenance: str,
    platform: str = "meta",
) -> list[dict[str, Any]]:
    """Fold ArchivedAd or operator-captured ad rows into one canonical competitor observation shape.

    Supports both automated API rows and operator-supplied rows across platforms
    (e.g. Meta, TikTok, Google Ads Transparency). Downstream clustering and reporting
    never branch on where evidence came from, only on how well attested it is.

    Creative text is advertiser-authored and stays strictly quarantined under
    `untrusted_creative`, so no caller can mistake it for instructions or for verified claims.
    Undisclosed metrics normalize to None rather than zero.
    Provenance is strictly restricted to 'ad-library-api' or 'operator-supplied'; scraped values are rejected.
    """
    if provenance not in PROVENANCE:
        raise ValueError(f"provenance must be one of {PROVENANCE}, got {provenance!r}")

    observations: list[dict[str, Any]] = []
    for ad in ads:
        ad_id = str(ad.get("id") or "").strip()
        if not ad_id:
            raise ValueError("every ArchivedAd row requires an id")

        row_platform = str(ad.get("platform") or platform).lower()

        # Extract creative content strictly quarantined under untrusted_creative
        creative_input = ad.get("untrusted_creative")
        if isinstance(creative_input, Mapping):
            bodies = list(creative_input.get("bodies") or [])
            titles = list(creative_input.get("titles") or [])
            descriptions = list(creative_input.get("descriptions") or [])
            captions = list(creative_input.get("captions") or [])
        else:
            bodies = list(
                ad.get("ad_creative_bodies")
                or ([ad["body"]] if "body" in ad and ad["body"] else [])
            )
            titles = list(
                ad.get("ad_creative_link_titles")
                or ([ad["title"]] if "title" in ad and ad["title"] else [])
            )
            descriptions = list(
                ad.get("ad_creative_link_descriptions")
                or ([ad["description"]] if "description" in ad and ad["description"] else [])
            )
            captions = list(
                ad.get("ad_creative_link_captions")
                or ([ad["caption"]] if "caption" in ad and ad["caption"] else [])
            )

        untrusted_creative = {
            "bodies": bodies,
            "titles": titles,
            "descriptions": descriptions,
            "captions": captions,
        }

        # Normalize disclosed metrics: absent keys are undisclosed, never zero
        disclosed = {key: ad[key] for key in POLITICAL_ONLY_FIELDS if key in ad}
        disclosed_metrics = (
            ad.get("disclosed_political_metrics")
            or ad.get("disclosed_metrics")
            or (disclosed if disclosed else None)
        )

        advertiser = ad.get("page_name") or ad.get("advertiser") or ad.get("advertiser_name")
        advertiser_page_id = (
            ad.get("page_id") or ad.get("advertiser_page_id") or ad.get("advertiser_id")
        )
        snapshot_raw = (
            ad.get("ad_snapshot_url") or ad.get("snapshot_url") or ad.get("source_url")
        )
        snapshot_url = _strip_snapshot_credential(snapshot_raw)

        publisher_platforms = list(
            ad.get("publisher_platforms")
            or ([row_platform] if row_platform != "meta" else [])
        )
        languages = list(ad.get("languages") or [])
        delivery_start = (
            ad.get("ad_delivery_start_time") or ad.get("delivery_start") or ad.get("first_shown")
        )
        delivery_stop = (
            ad.get("ad_delivery_stop_time") or ad.get("delivery_stop") or ad.get("last_shown") or None
        )

        observation_id = f"{row_platform}-ad-library.{ad_id}"

        observations.append(
            {
                "observation_id": observation_id,
                "platform": row_platform,
                "advertiser": advertiser,
                "advertiser_page_id": advertiser_page_id,
                "snapshot_url": snapshot_url,
                "publisher_platforms": publisher_platforms,
                "languages": languages,
                "delivery_start": delivery_start,
                "delivery_stop": delivery_stop,
                "untrusted_creative": untrusted_creative,
                # Absent keys are undisclosed for this ad's category, not zero.
                "disclosed_political_metrics": disclosed_metrics,
                "captured_at": captured_at,
                "provenance": provenance,
            }
        )
    return observations


VISUAL_MEDIA_TYPES = ("single_image", "video", "carousel", "other")



_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _require_rfc3339(value: str) -> None:
    if not isinstance(value, str) or not _RFC3339_RE.match(value):
        raise ValueError(f"captured_at must be an RFC3339 timestamp, got {value!r}")

def merge_operator_visuals(
    observations: Iterable[Mapping[str, Any]],
    captures: Iterable[Mapping[str, Any]],
    *,
    captured_at: str,
) -> dict[str, Any]:
    """Return operator visual captures as an overlay, never inside observations.

    The Ad Library API returns no media (CLM-0216), so a human's visual
    record is a separate evidence artifact with its own provenance. Base
    observations stay byte-identical whether they came from the API or from
    an operator; downstream reporting keeps the two lists side by side and
    attributes visual findings only to this overlay.
    """
    base = [dict(observation) for observation in observations]
    by_id = {observation["observation_id"]: observation for observation in base}
    _require_rfc3339(captured_at)
    overlay: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for capture in captures:
        observation_id = str(capture.get("observation_id") or "").strip()
        if not observation_id or observation_id not in by_id:
            raise ValueError(f"unknown observation: {observation_id!r}")
        if observation_id in seen_ids:
            raise ValueError(f"duplicate visual capture for {observation_id}")
        seen_ids.add(observation_id)
        media_type = str(capture.get("media_type") or "").strip()
        if media_type not in VISUAL_MEDIA_TYPES:
            raise ValueError(f"media_type must be one of {VISUAL_MEDIA_TYPES}")
        overlay.append(
            {
                "observation_id": observation_id,
                "snapshot_url": by_id[observation_id].get("snapshot_url"),
                "media_type": media_type,
                # Operator-typed browser text is untrusted input, same as ad
                # copy: quarantined so no caller mistakes it for verified data.
                "untrusted_visual": {
                    "on_screen_text": str(capture.get("on_screen_text") or ""),
                    "visual_notes": str(capture.get("visual_notes") or ""),
                },
                "provenance": "operator-supplied",
                "captured_at": captured_at,
            }
        )
    return {"observations": base, "operator_visual_captures": overlay}


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
