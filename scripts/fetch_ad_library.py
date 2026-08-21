#!/usr/bin/env python3
"""
Search the Meta Ad Library (`ads_archive`) for public ad creative.

Scope is set by Meta, not by this script. Per the official reference, "Ads that
did not reach any location in the EU will only return if they are about social
issues, elections or politics." A commercial competitor search therefore returns
rows only when `--countries` includes an EU member state.

Usage:
    python fetch_ad_library.py --search-terms "project management" --countries DE,FR
    python fetch_ad_library.py --search-page-ids 12345 --countries US \
        --ad-type POLITICAL_AND_ISSUE_ADS --include-political-fields

The access token is read from the META_AD_LIBRARY_TOKEN environment variable and
sent as a Bearer header so it never enters a URL, log line, or error string.
"""

import argparse
import json
import os
import sys
from datetime import date
from urllib.parse import urlparse

from url_utils import (
    guarded_request,
    resolve_output_path,
    sanitize_error,
)

try:
    import requests
except ImportError:
    print("Error: requests library required. Install with: pip install -r requirements.txt")
    sys.exit(1)

API_VERSION = "v21.0"
API_HOST = "graph.facebook.com"
ENDPOINT = f"https://{API_HOST}/{API_VERSION}/ads_archive"

# Returned for every ad the archive discloses, commercial or political.
DEFAULT_FIELDS = (
    "id",
    "ad_creation_time",
    "ad_creative_bodies",
    "ad_creative_link_captions",
    "ad_creative_link_descriptions",
    "ad_creative_link_titles",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_snapshot_url",
    "languages",
    "page_id",
    "page_name",
    "publisher_platforms",
)

# Documented as populated for social issue, election, and political ads only.
POLITICAL_FIELDS = (
    "bylines",
    "currency",
    "spend",
    "impressions",
    "demographic_distribution",
    "delivery_by_region",
    "estimated_audience_size",
)

# Documented as populated for UK and EU delivery only.
EU_FIELDS = (
    "age_country_gender_reach_breakdown",
    "beneficiary_payers",
    "eu_total_reach",
    "target_ages",
    "target_gender",
    "target_locations",
    "total_reach_by_location",
)

AD_TYPES = (
    "ALL",
    "EMPLOYMENT_ADS",
    "FINANCIAL_PRODUCTS_AND_SERVICES_ADS",
    "HOUSING_ADS",
    "POLITICAL_AND_ISSUE_ADS",
)

EU_COUNTRIES = frozenset(
    """AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK
    SI ES SE""".split()
)


def build_fields(include_political: bool, include_eu: bool) -> list[str]:
    """Assemble the requested field list, widest-scope fields last."""
    fields = list(DEFAULT_FIELDS)
    if include_political:
        fields.extend(POLITICAL_FIELDS)
    if include_eu:
        fields.extend(EU_FIELDS)
    return fields


def scope_warning(ad_type: str, countries: list[str]) -> str | None:
    """Return the documented empty-result explanation, or None if in scope.

    Meta restricts non-political disclosure to EU-reached ads. Callers hit this
    as a silent empty list, so name the cause before the request rather than
    letting an operator read zero rows as "this competitor runs no ads".
    """
    if ad_type == "POLITICAL_AND_ISSUE_ADS":
        return None
    if any(country.upper() in EU_COUNTRIES for country in countries):
        return None
    return (
        f"ad_type={ad_type} with no EU country in {','.join(countries)}: Meta returns "
        "only social issue, election, and political ads outside the EU, so commercial "
        "results are expected to be empty. Add an EU country or use "
        "--ad-type POLITICAL_AND_ISSUE_ADS."
    )


def _validate_next_url(url: str) -> str:
    """Confirm a paging cursor still points at the Graph API host.

    `paging.next` is server-supplied. guarded_request blocks private addresses,
    but the token travels in our Authorization header, so an off-host cursor
    would forward that credential to whatever host the response named.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != API_HOST:
        raise ValueError(f"Paging cursor left {API_HOST}; refusing to send credentials.")
    return url


def search_ad_library(
    *,
    token: str,
    countries: list[str],
    search_terms: str | None = None,
    search_page_ids: str | None = None,
    ad_type: str = "ALL",
    fields: list[str] | None = None,
    delivery_date_min: str | None = None,
    delivery_date_max: str | None = None,
    limit: int = 100,
    max_pages: int = 5,
    timeout: int = 30,
) -> dict:
    """Query ads_archive and page through results.

    Returns a dict with query metadata, ads, pages_fetched, warning, and error.
    """
    result = {
        "source": "meta-ad-library-api",
        "endpoint": ENDPOINT,
        "retrieved_at": date.today().isoformat(),
        "query": {
            "search_terms": search_terms,
            "search_page_ids": search_page_ids,
            "ad_reached_countries": countries,
            "ad_type": ad_type,
        },
        "warning": scope_warning(ad_type, countries),
        "ads": [],
        "pages_fetched": 0,
        "error": None,
    }

    if not search_terms and not search_page_ids:
        result["error"] = "Provide --search-terms or --search-page-ids."
        return result

    params = {
        "ad_reached_countries": json.dumps([c.upper() for c in countries]),
        "ad_type": ad_type,
        "fields": ",".join(fields or DEFAULT_FIELDS),
        "limit": limit,
    }
    if search_terms:
        params["search_terms"] = search_terms
    if search_page_ids:
        params["search_page_ids"] = search_page_ids
    if delivery_date_min:
        params["ad_delivery_date_min"] = delivery_date_min
    if delivery_date_max:
        params["ad_delivery_date_max"] = delivery_date_max

    session = requests.Session()
    # Ambient proxy settings would see the Authorization header.
    session.trust_env = False
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    url: str | None = ENDPOINT
    try:
        while url and result["pages_fetched"] < max_pages:
            response = guarded_request(
                session,
                "GET",
                url,
                headers=headers,
                params=params if result["pages_fetched"] == 0 else None,
                timeout=timeout,
            )
            if response.status_code != 200:
                result["error"] = (
                    f"Ad Library API returned HTTP {response.status_code}. "
                    "Confirm identity verification at facebook.com/ID and token validity."
                )
                return result

            payload = response.json()
            result["ads"].extend(payload.get("data", []))
            result["pages_fetched"] += 1

            next_url = (payload.get("paging") or {}).get("next")
            url = _validate_next_url(next_url) if next_url else None

    except ValueError as exc:
        result["error"] = sanitize_error(exc)
    except requests.exceptions.Timeout:
        result["error"] = f"Request timed out after {timeout} seconds"
    except requests.exceptions.RequestException as exc:
        result["error"] = f"Request failed: {sanitize_error(exc)}"

    return result


def main():
    parser = argparse.ArgumentParser(description="Search the Meta Ad Library")
    parser.add_argument("--search-terms", help="Keyword query")
    parser.add_argument("--search-page-ids", help="Comma-separated Facebook Page IDs")
    parser.add_argument(
        "--countries",
        required=True,
        help="Comma-separated ISO country codes (required by the API)",
    )
    parser.add_argument("--ad-type", default="ALL", choices=AD_TYPES)
    parser.add_argument("--delivery-date-min", help="YYYY-MM-DD")
    parser.add_argument("--delivery-date-max", help="YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=100, help="Results per page")
    parser.add_argument("--max-pages", type=int, default=5, help="Maximum pages to fetch")
    parser.add_argument("--include-political-fields", action="store_true")
    parser.add_argument("--include-eu-fields", action="store_true")
    parser.add_argument("--output", "-o", help="Write JSON here instead of stdout")

    args = parser.parse_args()

    token = os.environ.get("META_AD_LIBRARY_TOKEN")
    if not token:
        print(
            "Error: META_AD_LIBRARY_TOKEN is not set. Store the token in the "
            "environment or an OS keychain; never in a profile or the repository.",
            file=sys.stderr,
        )
        sys.exit(1)

    result = search_ad_library(
        token=token,
        countries=[c.strip() for c in args.countries.split(",") if c.strip()],
        search_terms=args.search_terms,
        search_page_ids=args.search_page_ids,
        ad_type=args.ad_type,
        fields=build_fields(args.include_political_fields, args.include_eu_fields),
        delivery_date_min=args.delivery_date_min,
        delivery_date_max=args.delivery_date_max,
        limit=args.limit,
        max_pages=args.max_pages,
    )

    if result["warning"]:
        print(f"Warning: {result['warning']}", file=sys.stderr)

    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        try:
            output_path = resolve_output_path(args.output, create_parent=True)
        except ValueError as exc:
            print(f"Error: {sanitize_error(exc)}", file=sys.stderr)
            sys.exit(1)
        output_path.write_text(payload, encoding="utf-8")
        print(f"Saved {len(result['ads'])} ads to {output_path}", file=sys.stderr)
    else:
        print(payload)

    if result["error"]:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
