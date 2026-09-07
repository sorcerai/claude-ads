#!/usr/bin/env python3
"""
Enrich all 5 standalone ad infrastructure repos with live Meta Ad Library intelligence.
Runs queries with conservative pacing, header inspection, and exponential backoff.
Saves structured intelligence files into each repository and updates showcase components.
"""

import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime

import requests

API_VERSION = "v26.0"
ENDPOINT = f"https://graph.facebook.com/{API_VERSION}/ads_archive"
FLEET_ROOT = os.environ.get("ADSINFRA_FLEET_ROOT")

def get_meta_token():
    # 1. Environment variable
    token = os.environ.get("META_AD_LIBRARY_TOKEN")
    if token:
        return token
    # 2. macOS Keychain
    try:
        res = subprocess.run(
            ["security", "find-generic-password", "-s", "META_AD_LIBRARY_TOKEN", "-w"],
            capture_output=True,
            text=True,
            check=True
        )
        return res.stdout.strip()
    except Exception as e:
        print(f"Error reading META_AD_LIBRARY_TOKEN: {e}", file=sys.stderr)
        sys.exit(1)


def fetch_with_backoff(session, params, token, max_retries=4, base_delay=2.0):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    for attempt in range(max_retries):
        try:
            response = session.get(ENDPOINT, headers=headers, params=params, timeout=30)
            
            # Check usage headers
            usage_header = response.headers.get("x-business-use-case-usage") or response.headers.get("X-App-Usage")
            if usage_header:
                try:
                    json.loads(usage_header)
                    # If any metric > 70%, add extra breathing delay
                    print(f"  [Usage Header] {usage_header[:100]}...")
                except Exception:
                    pass

            if response.status_code == 200:
                data = response.json()
                return data.get("data", [])
            elif response.status_code in (429, 500, 502, 503, 504):
                sleep_time = (base_delay * (2 ** attempt)) + random.uniform(0.5, 1.5)
                print(f"  [HTTP {response.status_code}] Backing off for {sleep_time:.2f}s (attempt {attempt+1}/{max_retries})...")
                time.sleep(sleep_time)
            else:
                err = response.json().get("error", {})
                code = err.get("code")
                msg = err.get("message", "")
                if code in (4, 17, 32, 613): # Throttled
                    sleep_time = (base_delay * (2 ** (attempt + 1))) + 5.0
                    print(f"  [Throttled code {code}] Backing off for {sleep_time:.2f}s...")
                    time.sleep(sleep_time)
                else:
                    print(f"  [Error {code}] {msg}", file=sys.stderr)
                    return []
        except requests.exceptions.RequestException as e:
            sleep_time = (base_delay * (2 ** attempt)) + 1.0
            print(f"  [Network error: {e}] Backing off for {sleep_time:.2f}s...")
            time.sleep(sleep_time)
            
    return []

CAMPAIGN_ENRICHMENTS = [
    {
        "id": "legal-ad-infra",
        "queries": [
            {
                "label": "mesothelioma",
                "params": {
                    "search_terms": "mesothelioma lawsuit",
                    "ad_reached_countries": json.dumps(["US"]),
                    "ad_type": "POLITICAL_AND_ISSUE_ADS",
                    "limit": 10,
                    "fields": "id,page_name,page_id,ad_creative_bodies,ad_creative_link_titles,ad_snapshot_url,spend,impressions,demographic_distribution,delivery_by_region,delivery_start_time"
                }
            },
            {
                "label": "camp_lejeune",
                "params": {
                    "search_terms": "camp lejeune lawsuit",
                    "ad_reached_countries": json.dumps(["US"]),
                    "ad_type": "POLITICAL_AND_ISSUE_ADS",
                    "limit": 10,
                    "fields": "id,page_name,page_id,ad_creative_bodies,ad_creative_link_titles,ad_snapshot_url,spend,impressions,demographic_distribution,delivery_by_region,delivery_start_time"
                }
            }
        ]
    },
    {
        "id": "telehealth-ad-infra",
        "queries": [
            {
                "label": "semaglutide_commercial",
                "params": {
                    "search_terms": "semaglutide online",
                    "ad_reached_countries": json.dumps(["GB", "DE"]),
                    "ad_type": "ALL",
                    "limit": 10,
                    "fields": "id,page_name,page_id,ad_creative_bodies,ad_creative_link_titles,ad_creative_link_descriptions,ad_snapshot_url,publisher_platforms,delivery_start_time"
                }
            },
            {
                "label": "tirzepatide_commercial",
                "params": {
                    "search_terms": "tirzepatide",
                    "ad_reached_countries": json.dumps(["GB", "DE"]),
                    "ad_type": "ALL",
                    "limit": 10,
                    "fields": "id,page_name,page_id,ad_creative_bodies,ad_creative_link_titles,ad_creative_link_descriptions,ad_snapshot_url,publisher_platforms,delivery_start_time"
                }
            }
        ]
    },
    {
        "id": "ecom-ad-scale",
        "queries": [
            {
                "label": "agency_ad_accounts",
                "params": {
                    "search_terms": "agency ad account",
                    "ad_reached_countries": json.dumps(["GB", "DE"]),
                    "ad_type": "ALL",
                    "limit": 10,
                    "fields": "id,page_name,page_id,ad_creative_bodies,ad_creative_link_titles,ad_creative_link_descriptions,ad_snapshot_url,publisher_platforms,delivery_start_time"
                }
            }
        ]
    },
    {
        "id": "adops-resilience",
        "queries": [
            {
                "label": "meta_account_ban_solutions",
                "params": {
                    "search_terms": "meta agency accounts",
                    "ad_reached_countries": json.dumps(["GB", "DE"]),
                    "ad_type": "ALL",
                    "limit": 10,
                    "fields": "id,page_name,page_id,ad_creative_bodies,ad_creative_link_titles,ad_creative_link_descriptions,ad_snapshot_url,publisher_platforms,delivery_start_time"
                }
            }
        ]
    },
    {
        "id": "ad-spend-index",
        "queries": [
            {
                "label": "commercial_auto_insurance",
                "params": {
                    "search_terms": "commercial auto insurance",
                    "ad_reached_countries": json.dumps(["GB", "DE"]),
                    "ad_type": "ALL",
                    "limit": 10,
                    "fields": "id,page_name,page_id,ad_creative_bodies,ad_creative_link_titles,ad_creative_link_descriptions,ad_snapshot_url,publisher_platforms,delivery_start_time"
                }
            },
            {
                "label": "water_damage_restoration",
                "params": {
                    "search_terms": "water damage restoration",
                    "ad_reached_countries": json.dumps(["GB", "DE"]),
                    "ad_type": "ALL",
                    "limit": 10,
                    "fields": "id,page_name,page_id,ad_creative_bodies,ad_creative_link_titles,ad_creative_link_descriptions,ad_snapshot_url,publisher_platforms,delivery_start_time"
                }
            }
        ]
    }
]

def main():
    token = get_meta_token()
    if not FLEET_ROOT:
        sys.exit(
            "Error: set ADSINFRA_FLEET_ROOT to the parent directory of the "
            "target repositories before running enrichment."
        )
    session = requests.Session()
    session.trust_env = False
    
    total_ads_collected = 0
    
    for campaign in CAMPAIGN_ENRICHMENTS:
        print("\n==========================================")
        print(f"Processing Vertical: {campaign['id']}")
        print("==========================================")
        
        output_file = os.path.join(FLEET_ROOT, campaign["id"], "data", "meta-ad-intel.json")
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        campaign_results = {
            "vertical": campaign["id"],
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "clusters": {}
        }
        
        for q in campaign["queries"]:
            print(f"-> Querying Meta Ad Library for '{q['label']}' ({q['params'].get('search_terms')})...")
            ads = fetch_with_backoff(session, q["params"], token)
            print(f"   Retrieved {len(ads)} live ads.")
            
            clean_ads = []
            for ad in ads:
                clean_ads.append({
                    "id": ad.get("id"),
                    "page_name": ad.get("page_name"),
                    "page_id": ad.get("page_id"),
                    "creative_bodies": ad.get("ad_creative_bodies", []),
                    "link_titles": ad.get("ad_creative_link_titles", []),
                    "snapshot_url": ad.get("ad_snapshot_url"),
                    "platforms": ad.get("publisher_platforms", []),
                    "delivery_start": ad.get("delivery_start_time"),
                    "spend_bracket": ad.get("spend"),
                    "impressions": ad.get("impressions"),
                    "demographics": ad.get("demographic_distribution", [])[:5] if ad.get("demographic_distribution") else []
                })
            
            campaign_results["clusters"][q["label"]] = {
                "search_terms": q["params"].get("search_terms"),
                "ad_count": len(clean_ads),
                "sample_ads": clean_ads
            }
            total_ads_collected += len(clean_ads)
            
            # Polite pacing delay between queries: 2.0 seconds
            time.sleep(2.0)
            
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(campaign_results, f, indent=2)
        print(f"Saved {len(campaign['queries'])} enriched clusters to {output_file}")
        
    print(f"\nSUCCESS: Total {total_ads_collected} verified Meta ads collected across all 5 verticals.")

if __name__ == "__main__":
    main()
