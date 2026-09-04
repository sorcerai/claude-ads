from __future__ import annotations

import datetime as dt
import re
from pathlib import Path


REFERENCE_NAMES = (
    "benchmarks.md",
    "platform-specs.md",
    "compliance.md",
    "compliance-requirements.md",
    "conversion-tracking.md",
    "bidding-strategies.md",
    "budget-allocation.md",
    "mcp-integration.md",
    "automation-tier-classifier.md",
    "meta-ai-stack.md",
)

LEGACY_BRITTLE_FRAGMENTS = (
    "all cited figures verified current",
    "70/20/10",
    "3× kill rule",
    "20% rule (scale up)",
    "50 conversions per week per ad set",
    "200 calls/hour",
    "22-state map",
    "server-side conversion apis are now mandatory",
    "dda (data-driven attribution) is now mandatory",
    "creative similarity score >60%",
    "always overclaims 20-40%",
)

REQUIRED_SOURCE_IDS = {
    "google-smart-bidding-official",
    "google-rsa-official",
    "google-consent-modeling-official",
    "tiktok-events-api-official",
    "eu-ai-act-article-50-official",
    "eu-dsa-official",
    "hhs-tracking-guidance-official",
    "ftc-endorsement-guides-official",
    "google-ads-mcp-official",
    "amazon-ads-mcp-official",
    "tiktok-ads-mcp-official",
    "microsoft-ads-mcp-official",
    "meta-andromeda-engineering-official",
    "meta-ai-ads-ranking-official",
    "meta-marketing-api-rate-limits-official",
    "meta-marketing-api-troubleshooting-official",
    "meta-insights-best-practices-official",
    "meta-platform-terms-official",
    "meta-developer-policies-official",
    "meta-mcp-protected-resource-metadata",
    "meta-mcp-authorization-metadata",
}


def _reference_texts(repo_root: Path) -> dict[str, str]:
    root = repo_root / "ads" / "references"
    return {
        name: (root / name).read_text(encoding="utf-8") for name in REFERENCE_NAMES
    }


def test_cross_platform_references_have_iso_verification_and_refresh_metadata(repo_root):
    iso_date = re.compile(r"\d{4}-\d{2}-\d{2}")
    verified_date = re.compile(r"\*\*Verified:\*\* (\d{4}-\d{2}-\d{2})")
    refresh_field = re.compile(r"\*\*Refresh(?: due)?:\*\* ([^\n]+)")
    for name, text in _reference_texts(repo_root).items():
        verified = verified_date.search(text)
        refresh = refresh_field.search(text)
        assert verified, name
        assert refresh, name
        refresh_date = iso_date.search(refresh.group(1))
        if refresh_date:
            assert dt.date.fromisoformat(refresh_date.group(0)) >= dt.date.fromisoformat(
                verified.group(1)
            ), name
        else:
            # Quarterly, foundational, and event-driven refresh cadences are valid.
            assert any(
                cadence in refresh.group(1).lower()
                for cadence in ("quarterly", "foundational", "event-driven")
            ), name


def test_cross_platform_references_do_not_restore_legacy_universal_claims(repo_root):
    corpus = "\n".join(_reference_texts(repo_root).values()).lower()
    for fragment in LEGACY_BRITTLE_FRAGMENTS:
        assert fragment not in corpus


def test_precise_current_claims_use_named_first_party_or_regulator_sources(repo_root):
    corpus = "\n".join(_reference_texts(repo_root).values())
    cited_ids = set(re.findall(r"`([a-z0-9][a-z0-9-]+)`", corpus))
    assert REQUIRED_SOURCE_IDS <= cited_ids

    markdown_links = re.findall(r"\[[^]]+\]\((https://[^)]+)\)", corpus)
    assert len(markdown_links) >= len(REQUIRED_SOURCE_IDS)
    assert all(" " not in link for link in markdown_links)


def test_benchmarks_are_contextual_and_vendor_evidence_is_labeled(repo_root):
    texts = _reference_texts(repo_root)
    assert "Benchmarks are comparison evidence, not pass/fail controls" in texts[
        "benchmarks.md"
    ]
    assert "vendor-supplied" in texts["benchmarks.md"]
    assert "vendor-supplied" in texts["meta-ai-stack.md"]


def test_automation_and_mcp_do_not_imply_write_authority(repo_root):
    texts = _reference_texts(repo_root)
    assert "disabled by default" in texts["mcp-integration.md"]
    assert "A server's write capability does not upgrade" in texts[
        "mcp-integration.md"
    ]
    assert "not an account-health score" in texts["automation-tier-classifier.md"]

def test_meta_mcp_uses_warehouse_first_direct_read_boundaries(repo_root):
    mcp = _reference_texts(repo_root)["mcp-integration.md"].lower()
    skill = (repo_root / "skills" / "ads-meta" / "SKILL.md").read_text(encoding="utf-8").lower()
    for text in (mcp, skill):
        normalized = " ".join(text.split())
        assert "warehouse-first" in normalized
        assert "warehouse storage is recommended, not mandated" in normalized
        assert "run/client-bound snapshots" in normalized
        assert (
            "direct meta reads are limited to bounded ingestion, cache recovery, or future mutation pre/post verification"
            in normalized
        )
        assert "data separation" in normalized
        assert "retention" in normalized
        assert "rate-limit and abuse controls" in normalized
        assert "high call volume automatically violates platform terms" in normalized
    assert "current account live-read is not installed" in skill
    assert "account mutation disabled" in skill

