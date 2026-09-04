from __future__ import annotations

import json
import runpy
from importlib import resources
from pathlib import Path

import pytest

from claude_ads_core.contracts import PLATFORMS
from claude_ads_core.control_registry import (
    ControlRegistry,
    RegistryEntry,
    RegistryError,
    ScoringProfile,
    load_control_registry,
)
from tests.core.test_contracts import control, finding as _finding, report_bundle as _report_bundle


_OBSERVATION_DIGEST = "a" * 64
_OBSERVATION_SOURCE = f"sha256:{_OBSERVATION_DIGEST}"


def finding(control_id: str = "G-1") -> dict:
    payload = _finding(control_id)
    for evidence in payload["evidence"]:
        if evidence["proof_kind"] == "observation":
            evidence["source_id"] = _OBSERVATION_SOURCE
            evidence["sha256"] = _OBSERVATION_DIGEST
    return payload


def report_bundle() -> dict:
    payload = _report_bundle()
    payload["run_manifest"]["sources"] = ["export.csv", _OBSERVATION_SOURCE]
    payload["account_snapshot"]["measurement_context"]["source_ids"] = [
        "source-1",
        _OBSERVATION_SOURCE,
    ]
    for finding_payload in payload["findings"]:
        for evidence in finding_payload["evidence"]:
            if evidence["proof_kind"] == "observation":
                evidence["source_id"] = _OBSERVATION_SOURCE
                evidence["sha256"] = _OBSERVATION_DIGEST
    return payload


def test_packaged_manifests_match_canonical_files(repo_root: Path):
    package_root = resources.files("claude_ads_core").joinpath("manifests")
    canonical_root = repo_root / "control-plane" / "manifests"
    for name in (
        "control-registry.json",
        "scoring-profiles.json",
        "claim-ledger.json",
        "source-ledger.json",
    ):
        assert package_root.joinpath(name).read_bytes() == (canonical_root / name).read_bytes()


def test_default_registry_loads_from_package_outside_checkout(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    registry = load_control_registry()
    assert {entry.platform for entry in registry.entries} == PLATFORMS


def test_explicit_registry_root_does_not_fall_back_to_package(tmp_path: Path):
    with pytest.raises(RegistryError, match="control-registry.json"):
        load_control_registry(tmp_path)


def test_registry_covers_every_catalog_id_exactly_once(check_catalog, repo_root: Path):
    registry = load_control_registry(repo_root)
    expected = {
        (platform, control_id)
        for platform, data in check_catalog["platforms"].items()
        for control_id in data["check_ids"]
    }
    actual = {(entry.platform, entry.control_id) for entry in registry.entries}
    assert actual == expected
    assert len(actual) == sum(data["total_checks"] for data in check_catalog["platforms"].values())


def test_registry_is_reproducibly_generated(repo_root: Path):
    build = runpy.run_path(str(repo_root / "scripts" / "build_control_registry.py"))["build"]
    expected_registry, expected_profiles = build(repo_root)
    manifests = repo_root / "control-plane" / "manifests"
    assert json.loads((manifests / "control-registry.json").read_text(encoding="utf-8")) == expected_registry
    assert json.loads((manifests / "scoring-profiles.json").read_text(encoding="utf-8")) == expected_profiles


def test_current_catalog_is_explicitly_unscored_without_invented_severity(repo_root: Path):
    registry = load_control_registry(repo_root)
    assert registry.entries
    assert all(entry.disposition != "health" for entry in registry.entries)
    assert all(entry.control_definition["severity"] == "informational" for entry in registry.entries)
    assert all(entry.control_definition["scoring_behavior"] in {"watchlist", "opportunity"} for entry in registry.entries)
    assert all(entry.control_definition["required_inputs"] for entry in registry.entries)


def test_all_twelve_profiles_are_versioned_disabled_and_fail_closed(repo_root: Path):
    registry = load_control_registry(repo_root)
    assert {profile.platform for profile in registry.profiles} == PLATFORMS
    for platform in PLATFORMS:
        profile = registry.profile_for(platform)
        assert profile.status == "disabled"
        assert "invent" in (profile.disabled_reason or "")
        with pytest.raises(RegistryError, match="is disabled"):
            registry.scoring_inputs(platform)
        result = registry.score_platform(platform, [])
        assert result.health_score is None
        assert result.evidence_coverage == 0.0
        assert result.status == "insufficient_evidence"


def test_source_grounded_watchlists_resolve_to_verified_load_bearing_claims(repo_root: Path):
    registry = load_control_registry(repo_root)
    claims = json.loads(
        (repo_root / "control-plane/manifests/claim-ledger.json").read_text(encoding="utf-8")
    )["claims"]
    by_id = {claim["id"]: claim for claim in claims}
    grounded = [entry for entry in registry.entries if entry.source_claim_ids]
    assert grounded
    for entry in grounded:
        assert entry.control_definition["maturity"] == "source-grounded"
        assert entry.control_definition["source_ids"]
        for claim_id in entry.source_claim_ids:
            assert by_id[claim_id]["verdict"] == "verified"
            assert by_id[claim_id]["load_bearing"] is True
            assert set(entry.control_definition["source_ids"]) <= set(by_id[claim_id]["source_ids"])

def test_registry_preserves_source_refresh_due_by_source(repo_root: Path):
    registry = load_control_registry(repo_root)
    sources = json.loads(
        (repo_root / "control-plane/manifests/source-ledger.json").read_text(encoding="utf-8")
    )["sources"]
    by_id = {source["id"]: source for source in sources}
    grounded = next(entry for entry in registry.entries if entry.control_definition["source_ids"])

    assert dict(grounded.source_refresh_due) == {
        source_id: by_id[source_id]["refresh_due"]
        for source_id in grounded.control_definition["source_ids"]
    }


def test_loader_rejects_enabling_a_watchlist_profile(tmp_path: Path, repo_root: Path):
    target = tmp_path / "control-plane" / "manifests"
    target.mkdir(parents=True)
    source = repo_root / "control-plane" / "manifests"
    for name in ("control-registry.json", "claim-ledger.json", "source-ledger.json"):
        (target / name).write_bytes((source / name).read_bytes())
    profiles = json.loads((source / "scoring-profiles.json").read_text(encoding="utf-8"))
    profiles["profiles"][0].update(
        status="enabled",
        category_weights={"measurement": 100},
        health_control_ids=["AMZ-M01"],
    )
    profiles["profiles"][0].pop("disabled_reason")
    (target / "scoring-profiles.json").write_text(json.dumps(profiles), encoding="utf-8")
    with pytest.raises(RegistryError, match="references unscored control"):
        load_control_registry(tmp_path)


def test_loader_rejects_health_control_without_verified_claim_grounding(
    tmp_path: Path, repo_root: Path
):
    target = tmp_path / "control-plane" / "manifests"
    target.mkdir(parents=True)
    source = repo_root / "control-plane" / "manifests"
    for name in ("control-registry.json", "scoring-profiles.json", "claim-ledger.json", "source-ledger.json"):
        (target / name).write_bytes((source / name).read_bytes())
    registry = json.loads((target / "control-registry.json").read_text(encoding="utf-8"))
    entry = next(item for item in registry["controls"] if not item["source_claim_ids"])
    entry["disposition"] = "health"
    entry["control_definition"].update(
        severity="high",
        maturity="source-grounded",
        scoring_behavior="health",
        stability="stable",
        expires_at="2026-08-01",
    )
    (target / "control-registry.json").write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(RegistryError, match="lacks typed evidence grounding"):
        load_control_registry(tmp_path)


def _enabled_registry() -> ControlRegistry:
    health = control()
    omitted_health = control("G-2")
    watchlist = control("W-1")
    watchlist.update(severity="informational", scoring_behavior="watchlist")
    source_refresh_due = (("google-help-1", "2026-07-11"),)
    return ControlRegistry(
        entries=(
            RegistryEntry("google", "G-1", "health", "health", (), health, (), source_refresh_due),
            RegistryEntry("google", "G-2", "health", "health", (), omitted_health, (), source_refresh_due),
            RegistryEntry("google", "W-1", "watchlist", "conditional_watchlist", (), watchlist),
        ),
        profiles=(
            ScoringProfile(
                "google-test-v1",
                "google",
                "enabled",
                {"tracking": 100.0},
                ("G-1",),
                None,
            ),
        ),
    )


def _registry_with_required_input(
    required_input: str,
    *,
    claim_refresh_due: tuple[str, ...] = (),
    source_refresh_due: tuple[tuple[str, str], ...] = (("google-help-1", "2026-07-11"),),
) -> ControlRegistry:
    registry = _enabled_registry()
    health = dict(registry.entries[0].control_definition)
    health["required_inputs"] = [required_input]
    entries = (
        RegistryEntry(
            "google",
            "G-1",
            "health",
            "health",
            (),
            health,
            claim_refresh_due,
            source_refresh_due,
        ),
        *registry.entries[1:],
    )
    return ControlRegistry(entries=entries, profiles=registry.profiles)


def _bundle_for_registry(registry: ControlRegistry) -> dict:
    bundle = report_bundle()
    bundle["account_snapshot"]["measurement_context"]["source_ids"] = [_OBSERVATION_SOURCE]
    bundle["account_snapshot"]["conversions"] = [{"action": "purchase", "count": 1.0}]
    bundle["control_definitions"][0] = dict(registry.entries[0].control_definition)
    bundle["scoring"] = registry.score_platform("google", bundle["findings"]).to_dict()
    return bundle


def _disabled_registry_with_omitted_health() -> ControlRegistry:
    registry = _enabled_registry()
    return ControlRegistry(
        entries=registry.entries,
        profiles=(
            ScoringProfile(
                "google-disabled-test-v1",
                "google",
                "disabled",
                {},
                (),
                "No approved controls.",
            ),
        ),
    )


def test_disabled_google_profile_rejects_fabricated_report_score(repo_root: Path):
    registry = load_control_registry(repo_root)
    bundle = report_bundle()
    bundle["control_definitions"] = []
    bundle["findings"] = []
    bundle["run_manifest"]["completeness"] = "partial"
    with pytest.raises(RegistryError, match="google-health-v1"):
        registry.validate_report_scoring(bundle)



def test_disabled_google_profile_rejects_unapproved_health_definition():
    registry = _disabled_registry_with_omitted_health()
    bundle = report_bundle()
    bundle["control_definitions"] = [
        next(entry.control_definition for entry in registry.entries if entry.control_id == "G-2")
    ]
    bundle["findings"] = []
    bundle["scoring"] = registry.score_platform("google", []).to_dict()
    bundle["run_manifest"]["completeness"] = "partial"

    with pytest.raises(RegistryError, match="unapproved health control"):
        registry.validate_report_scoring(bundle)

def test_disabled_google_profile_rejects_unbound_evidence_source():
    enabled = _enabled_registry()
    profile = enabled.profiles[0]
    registry = ControlRegistry(
        entries=enabled.entries,
        profiles=(
            ScoringProfile(
                "google-disabled-test-v1",
                profile.platform,
                "disabled",
                profile.category_weights,
                profile.health_control_ids,
                "No approved controls.",
            ),
        ),
    )
    bundle = _bundle_for_registry(registry)
    bundle["run_manifest"]["completeness"] = "partial"
    unknown_source = f"sha256:{'b' * 64}"
    bundle["findings"][0]["evidence"][0].update(
        source_id=unknown_source,
        sha256="b" * 64,
    )

    with pytest.raises(RegistryError, match="unbound evidence source"):
        registry.validate_report_scoring(bundle)

@pytest.mark.parametrize("status", ["pass", "fail"])
def test_disabled_watchlist_finding_requires_current_account_evidence(status: str):
    enabled = _enabled_registry()
    watchlist = dict(enabled.entries[2].control_definition)
    watchlist["required_inputs"] = ["current_account_evidence"]
    registry = ControlRegistry(
        entries=(
            *enabled.entries[:2],
            RegistryEntry("google", "W-1", "watchlist", "conditional_watchlist", (), watchlist),
        ),
        profiles=(
            ScoringProfile(
                "google-disabled-test-v1",
                "google",
                "disabled",
                {},
                (),
                "No approved controls.",
            ),
        ),
    )
    bundle = report_bundle()
    bundle["control_definitions"] = [watchlist]
    bundle["findings"] = [finding("W-1")]
    bundle["findings"][0]["evidence"][0].update(
        proof_kind="source_fact",
        source_id="google-help-1",
        window=None,
    )
    bundle["run_manifest"]["sources"].append("google-help-1")
    bundle["scoring"] = registry.score_platform("google", []).to_dict()
    bundle["run_manifest"]["completeness"] = "partial"

    with pytest.raises(RegistryError, match="current_account_evidence"):
        registry.validate_report_scoring(bundle)





def test_disabled_google_profile_accepts_registry_no_score(repo_root: Path):
    registry = load_control_registry(repo_root)
    bundle = report_bundle()
    bundle["control_definitions"] = []
    bundle["findings"] = []
    bundle["scoring"] = registry.score_platform("google", []).to_dict()
    bundle["run_manifest"]["completeness"] = "partial"
    assert registry.validate_report_scoring(bundle) == registry.score_platform("google", [])



def test_enabled_registry_accepts_exact_score_and_ignores_watchlist_findings():
    registry = _enabled_registry()
    bundle = report_bundle()
    bundle["account_snapshot"]["conversions"] = [{"action": "purchase", "count": 1.0}]
    bundle["account_snapshot"]["measurement_context"]["source_ids"] = [_OBSERVATION_SOURCE]
    bundle["control_definitions"].append(control("W-1") | {"severity": "informational", "scoring_behavior": "watchlist"})
    bundle["findings"].append(finding("W-1"))
    bundle["findings"][1]["evidence"][0]["evidence_id"] = "evidence-watchlist"
    expected = registry.score_platform("google", [bundle["findings"][0]])
    bundle["scoring"] = expected.to_dict()

    bundle["findings"][1]["status"] = "fail"
    assert registry.validate_report_scoring(bundle) == expected


def test_enabled_registry_rejects_tampered_health_score():
    registry = _enabled_registry()
    bundle = report_bundle()
    bundle["scoring"] = registry.score_platform("google", bundle["findings"]).to_dict()
    bundle["scoring"]["health_score"] = 99.0

    with pytest.raises(RegistryError, match="google-test-v1"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_contract_error_as_registry_error():
    registry = _enabled_registry()
    bundle = report_bundle()
    bundle["account_snapshot"]["account"]["platform"] = "unsupported"

    with pytest.raises(RegistryError, match="platform"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_duplicate_approved_findings():
    registry = _enabled_registry()
    bundle = report_bundle()
    bundle["findings"].append(finding())
    bundle["findings"][1]["evidence"][0]["evidence_id"] = "evidence-duplicate"
    bundle["scoring"] = registry.score_platform("google", [bundle["findings"][0]]).to_dict()

    with pytest.raises(RegistryError, match="google-test-v1"):
        registry.validate_report_scoring(bundle)


@pytest.mark.parametrize(
    ("tampered_field", "updates"),
    [
        ("applicable_controls", {"applicable_controls": 2, "unknown_controls": 1}),
        ("known_controls", {"applicable_controls": 2, "known_controls": 2, "failed_controls": 1}),
        ("passed_controls", {"passed_controls": 0, "failed_controls": 1}),
        ("failed_controls", {"passed_controls": 0, "failed_controls": 1}),
        ("unknown_controls", {"applicable_controls": 2, "unknown_controls": 1}),
    ],
)
def test_enabled_registry_rejects_tampered_category_count(
    tampered_field: str, updates: dict[str, int]
):
    registry = _enabled_registry()
    bundle = report_bundle()
    bundle["scoring"] = registry.score_platform("google", bundle["findings"]).to_dict()
    tampered_value = bundle["scoring"]["categories"][0][tampered_field]
    bundle["scoring"]["categories"][0].update(updates)

    assert bundle["scoring"]["categories"][0][tampered_field] != tampered_value
    with pytest.raises(RegistryError, match="google-test-v1"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_accepts_reordered_categories():
    first = control()
    second = control("G-2")
    second["category"] = "creative"
    registry = ControlRegistry(
        entries=(
            RegistryEntry(
                "google",
                "G-1",
                "health",
                "health",
                (),
                first,
                (),
                (("google-help-1", "2026-07-11"),),
            ),
            RegistryEntry(
                "google",
                "G-2",
                "health",
                "health",
                (),
                second,
                (),
                (("google-help-1", "2026-07-11"),),
            ),
        ),
        profiles=(
            ScoringProfile(
                "google-two-category-test-v1",
                "google",
                "enabled",
                {"tracking": 50.0, "creative": 50.0},
                ("G-1", "G-2"),
                None,
            ),
        ),
    )
    bundle = report_bundle()
    bundle["account_snapshot"]["measurement_context"]["source_ids"] = [_OBSERVATION_SOURCE]
    bundle["account_snapshot"]["conversions"] = [{"action": "purchase", "count": 1.0}]
    bundle["control_definitions"].append(second)
    bundle["findings"].append(finding("G-2"))
    bundle["findings"][1]["evidence"][0]["evidence_id"] = "evidence-reordered"
    expected = registry.score_platform("google", bundle["findings"])
    bundle["scoring"] = expected.to_dict()
    bundle["scoring"]["categories"].reverse()

    assert registry.validate_report_scoring(bundle) == expected

@pytest.mark.parametrize("category_case", ["nonmapping", "missing_field", "unsortable"])
def test_enabled_registry_rejects_malformed_categories(category_case: str):
    registry = _enabled_registry()
    bundle = report_bundle()
    bundle["scoring"] = registry.score_platform("google", bundle["findings"]).to_dict()
    if category_case == "nonmapping":
        bundle["scoring"]["categories"] = [None]
    elif category_case == "missing_field":
        bundle["scoring"]["categories"] = [
            {"category": "tracking", "category_weight": 100.0, "health_score": 100.0}
        ]
    else:
        category = bundle["scoring"]["categories"][0]
        bundle["scoring"]["categories"] = [category, {**category, "category": 1}]

    with pytest.raises(RegistryError, match="google-test-v1"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_unapproved_health_definition():
    registry = _enabled_registry()
    bundle = report_bundle()
    bundle["control_definitions"].append(
        next(entry.control_definition for entry in registry.entries if entry.control_id == "G-2")
    )
    bundle["scoring"] = registry.score_platform("google", bundle["findings"]).to_dict()

    with pytest.raises(RegistryError, match="unapproved health control"):
        registry.validate_report_scoring(bundle)
@pytest.mark.parametrize("definition_change", ["mutated", "missing", "duplicate"])
def test_enabled_registry_rejects_approved_health_definition_drift(definition_change: str):
    registry = _enabled_registry()
    bundle = report_bundle()
    bundle["scoring"] = registry.score_platform("google", bundle["findings"]).to_dict()
    if definition_change == "mutated":
        bundle["control_definitions"][0]["severity"] = "high"
    elif definition_change == "missing":
        bundle["control_definitions"] = []
        bundle["run_manifest"]["completeness"] = "partial"
    else:
        bundle["control_definitions"].append(control())

    with pytest.raises(RegistryError, match="google-test-v1"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_unknown_extra_definition():
    registry = _enabled_registry()
    bundle = report_bundle()
    unknown = control("UNKNOWN")
    unknown.update(severity="informational", scoring_behavior="watchlist")
    bundle["control_definitions"].append(unknown)
    bundle["scoring"] = registry.score_platform("google", bundle["findings"]).to_dict()

    with pytest.raises(RegistryError, match="google-test-v1"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_mutated_unscored_definition():
    registry = _enabled_registry()
    bundle = report_bundle()
    watchlist = control("W-1")
    watchlist.update(severity="informational", scoring_behavior="watchlist", category="mutated")
    bundle["control_definitions"].append(watchlist)
    bundle["scoring"] = registry.score_platform("google", bundle["findings"]).to_dict()

    with pytest.raises(RegistryError, match="google-test-v1"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_duplicate_unscored_definitions():
    registry = _enabled_registry()
    bundle = report_bundle()
    watchlist = control("W-1")
    watchlist.update(severity="informational", scoring_behavior="watchlist")
    bundle["control_definitions"].extend([watchlist, dict(watchlist)])
    bundle["scoring"] = registry.score_platform("google", bundle["findings"]).to_dict()

    with pytest.raises(RegistryError, match="google-test-v1"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_unknown_finding_without_definition():
    registry = _enabled_registry()
    bundle = report_bundle()
    bundle["findings"].append(finding("UNKNOWN"))
    bundle["scoring"] = registry.score_platform("google", bundle["findings"][:1]).to_dict()

    with pytest.raises(RegistryError, match="google-test-v1"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_duplicate_unscored_findings():
    registry = _enabled_registry()
    bundle = report_bundle()
    watchlist = control("W-1")
    watchlist.update(severity="informational", scoring_behavior="watchlist")
    bundle["control_definitions"].append(watchlist)
    bundle["findings"].extend([finding("W-1"), finding("W-1")])
    bundle["scoring"] = registry.score_platform("google", bundle["findings"][:1]).to_dict()

    with pytest.raises(RegistryError, match="google-test-v1"):
        registry.validate_report_scoring(bundle)




def test_enabled_registry_rejects_false_for_zero_category_health_score():
    registry = _enabled_registry()
    bundle = report_bundle()
    bundle["findings"][0]["status"] = "fail"
    bundle["scoring"] = registry.score_platform("google", bundle["findings"]).to_dict()
    assert bundle["scoring"]["categories"][0]["health_score"] == 0.0
    bundle["scoring"]["categories"][0]["health_score"] = False

    with pytest.raises(RegistryError, match="google-test-v1"):
        registry.validate_report_scoring(bundle)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", 1),
        ("category", ""),
        ("category_weight", True),
        ("category_weight", float("nan")),
        ("category_weight", -1.0),
        ("category_weight", 101.0),
        ("evidence_coverage", True),
        ("evidence_coverage", float("inf")),
        ("evidence_coverage", -1.0),
        ("evidence_coverage", 101.0),
        ("health_score", True),
        ("health_score", float("nan")),
        ("health_score", -1.0),
        ("health_score", 101.0),
    ],
)
def test_enabled_registry_rejects_malformed_category_values(field: str, value: object):
    registry = _enabled_registry()
    bundle = report_bundle()
    bundle["scoring"] = registry.score_platform("google", bundle["findings"]).to_dict()
    bundle["scoring"]["categories"][0][field] = value

    with pytest.raises(RegistryError, match="google-test-v1"):
        registry.validate_report_scoring(bundle)
def test_enabled_registry_rejects_fabricated_evidence_source():
    registry = _enabled_registry()
    bundle = _bundle_for_registry(registry)
    unknown_source = f"sha256:{'b' * 64}"
    bundle["findings"][0]["evidence"][0].update(
        source_id=unknown_source,
        sha256="b" * 64,
    )

    with pytest.raises(RegistryError, match="unbound evidence source"):
        registry.validate_report_scoring(bundle)


@pytest.mark.parametrize(
    ("source_id", "sha256", "error"),
    [
        ("synthetic-observation", _OBSERVATION_DIGEST, "canonical"),
        (f"sha256:{'A' * 64}", _OBSERVATION_DIGEST, "canonical"),
        (_OBSERVATION_SOURCE, None, "64-hex digest"),
        (_OBSERVATION_SOURCE, "b" * 64, "does not match"),
    ],
)
def test_enabled_registry_rejects_noncanonical_observation_evidence(
    source_id: str, sha256: str | None, error: str
):
    registry = _enabled_registry()
    bundle = _bundle_for_registry(registry)
    if source_id not in bundle["run_manifest"]["sources"]:
        bundle["run_manifest"]["sources"].append(source_id)
    if source_id not in bundle["account_snapshot"]["measurement_context"]["source_ids"]:
        bundle["account_snapshot"]["measurement_context"]["source_ids"].append(source_id)
    bundle["findings"][0]["evidence"][0].update(source_id=source_id, sha256=sha256)

    with pytest.raises(RegistryError, match=error):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_accepts_observation_evidence_with_matching_digest():
    registry = _enabled_registry()
    bundle = _bundle_for_registry(registry)

    assert registry.validate_report_scoring(bundle) == registry.score_platform(
        "google", bundle["findings"]
    )


def test_enabled_registry_rejects_observation_evidence_with_null_digest():
    registry = _enabled_registry()
    bundle = _bundle_for_registry(registry)
    bundle["findings"][0]["evidence"][0]["sha256"] = None

    with pytest.raises(RegistryError, match="64-hex digest"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_observation_evidence_with_malformed_source_hash():
    registry = _enabled_registry()
    bundle = _bundle_for_registry(registry)
    malformed_source = "sha256:not-a-hash"
    bundle["run_manifest"]["sources"].append(malformed_source)
    bundle["account_snapshot"]["measurement_context"]["source_ids"].append(malformed_source)
    bundle["findings"][0]["evidence"][0].update(
        source_id=malformed_source,
        sha256=_OBSERVATION_DIGEST,
    )

    with pytest.raises(RegistryError, match="canonical"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_observation_evidence_with_mismatched_source_digest():
    registry = _enabled_registry()
    bundle = _bundle_for_registry(registry)
    other_source = f"sha256:{'b' * 64}"
    bundle["run_manifest"]["sources"].append(other_source)
    bundle["account_snapshot"]["measurement_context"]["source_ids"].append(other_source)
    bundle["findings"][0]["evidence"][0].update(
        source_id=other_source,
        sha256=_OBSERVATION_DIGEST,
    )

    with pytest.raises(RegistryError, match="does not match"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_observation_window_outside_snapshot():
    registry = _enabled_registry()
    bundle = _bundle_for_registry(registry)
    bundle["findings"][0]["evidence"][0]["window"]["start"] = "2026-05-31"

    with pytest.raises(RegistryError, match="window"):
        registry.validate_report_scoring(bundle)


@pytest.mark.parametrize("required_input", ["campaigns", "creatives", "conversions", "budgets"])
def test_enabled_registry_rejects_empty_required_collection(required_input: str):
    registry = _registry_with_required_input(required_input)
    bundle = _bundle_for_registry(registry)
    bundle["account_snapshot"][required_input] = []

    with pytest.raises(RegistryError, match=required_input):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_missing_required_spend():
    registry = _registry_with_required_input("spend")
    bundle = _bundle_for_registry(registry)
    bundle["account_snapshot"]["spend"] = None

    with pytest.raises(RegistryError, match="spend"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_required_context_field_listed_missing():
    registry = _registry_with_required_input("measurement_context.view_attribution_window")
    bundle = _bundle_for_registry(registry)
    bundle["account_snapshot"]["measurement_context"]["missing_fields"] = ["view_attribution_window"]

    with pytest.raises(RegistryError, match="view_attribution_window"):
        registry.validate_report_scoring(bundle)


@pytest.mark.parametrize(
    ("required_input", "unsupported_field", "row"),
    [
        ("creatives", "creative_id", {"creative_id": "creative-1", "campaign_id": "campaign-1"}),
        ("conversions", "conversion_action", {"action": "purchase", "count": 1.0}),
        ("budgets", "budget", {"campaign_id": "campaign-1", "date": "2026-06-30", "amount": 10.0}),
    ],
)
def test_enabled_registry_uses_collection_rows_not_unsupported_field_flags(
    required_input: str, unsupported_field: str, row: dict[str, object]
):
    registry = _registry_with_required_input(required_input)
    bundle = _bundle_for_registry(registry)
    bundle["account_snapshot"][required_input] = [row]
    bundle["account_snapshot"]["measurement_context"]["unsupported_fields"] = [unsupported_field]
    bundle["scoring"] = registry.score_platform("google", bundle["findings"]).to_dict()

    assert registry.validate_report_scoring(bundle) == registry.score_platform("google", bundle["findings"])


def test_enabled_registry_rejects_unknown_required_input():
    registry = _registry_with_required_input("custom_input")
    bundle = _bundle_for_registry(registry)

    with pytest.raises(RegistryError, match="custom_input"):
        registry.validate_report_scoring(bundle)

def test_enabled_registry_accepts_control_expiring_on_run_date():
    registry = _registry_with_required_input("conversions")
    entry = registry.entries[0]
    health = dict(entry.control_definition)
    health["expires_at"] = "2026-07-11"
    registry = ControlRegistry(
        entries=(
            RegistryEntry(
                "google",
                "G-1",
                "health",
                "health",
                (),
                health,
                entry.claim_refresh_due,
                entry.source_refresh_due,
            ),
            *registry.entries[1:],
        ),
        profiles=registry.profiles,
    )
    bundle = _bundle_for_registry(registry)

    assert registry.validate_report_scoring(bundle) == registry.score_platform(
        "google", bundle["findings"]
    )




def test_enabled_registry_allows_unknown_with_missing_required_input():
    registry = _registry_with_required_input("conversions")
    bundle = _bundle_for_registry(registry)
    bundle["account_snapshot"]["conversions"] = []
    bundle["findings"][0].update(status="unknown", evidence=[], confidence="none")
    bundle["run_manifest"]["completeness"] = "partial"
    bundle["scoring"] = registry.score_platform("google", bundle["findings"]).to_dict()

    assert registry.validate_report_scoring(bundle) == registry.score_platform("google", bundle["findings"])
def test_enabled_registry_rejects_expired_control_before_reconciliation():
    registry = _registry_with_required_input("conversions")
    health = dict(registry.entries[0].control_definition)
    health["expires_at"] = "2026-07-10"
    registry = ControlRegistry(
        entries=(RegistryEntry("google", "G-1", "health", "health", (), health), *registry.entries[1:]),
        profiles=registry.profiles,
    )
    bundle = _bundle_for_registry(registry)

    with pytest.raises(RegistryError, match="google-test-v1.*G-1.*expired"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_expired_claim_refresh_before_reconciliation():
    registry = _registry_with_required_input("conversions", claim_refresh_due=("2026-07-10",))
    bundle = _bundle_for_registry(registry)

    with pytest.raises(RegistryError, match="google-test-v1.*G-1.*claim.*expired"):
        registry.validate_report_scoring(bundle)

def test_enabled_registry_accepts_claim_refresh_due_on_run_date():
    registry = _registry_with_required_input("conversions", claim_refresh_due=("2026-07-11",))
    bundle = _bundle_for_registry(registry)

    assert registry.validate_report_scoring(bundle) == registry.score_platform(
        "google", bundle["findings"]
    )



def test_enabled_registry_rejects_observation_evidence_without_window():
    registry = _enabled_registry()
    bundle = _bundle_for_registry(registry)
    bundle["findings"][0]["evidence"][0]["window"] = None

    with pytest.raises(RegistryError, match="observation evidence window"):
        registry.validate_report_scoring(bundle)

@pytest.mark.parametrize("proof_kind", ["source_fact", "vendor_claim", "inference"])
def test_enabled_registry_allows_null_window_for_proof_specific_evidence(proof_kind: str):
    registry = _enabled_registry()
    bundle = _bundle_for_registry(registry)
    bundle["run_manifest"]["sources"].append("google-help-1")
    bundle["findings"][0]["evidence"][0].update(
        proof_kind=proof_kind,
        source_id="google-help-1",
        window=None,
    )

    assert registry.validate_report_scoring(bundle) == registry.score_platform(
        "google", bundle["findings"]
    )




def test_enabled_registry_accepts_available_prefixed_context_required_input():
    registry = _registry_with_required_input("measurement_context.view_attribution_window")
    bundle = _bundle_for_registry(registry)
    context = bundle["account_snapshot"]["measurement_context"]
    context["view_attribution_window"] = {"value": 1, "unit": "day"}
    context["missing_fields"] = [
        field for field in context["missing_fields"] if field != "view_attribution_window"
    ]

    assert registry.validate_report_scoring(bundle) == registry.score_platform("google", bundle["findings"])

def test_enabled_registry_rejects_context_unbound_observation_source():
    registry = _enabled_registry()
    bundle = _bundle_for_registry(registry)
    context = bundle["account_snapshot"]["measurement_context"]
    context["source_ids"] = []
    context["missing_fields"] = sorted(set(context["missing_fields"]) | {"source_ids"})

    with pytest.raises(RegistryError, match="unbound evidence source"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_missing_source_refresh_due_before_reconciliation():
    registry = _registry_with_required_input("conversions", source_refresh_due=())
    bundle = _bundle_for_registry(registry)

    with pytest.raises(RegistryError, match="google-test-v1.*G-1.*google-help-1.*refresh"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_expired_source_refresh_due_before_reconciliation():
    registry = _registry_with_required_input(
        "conversions", source_refresh_due=(("google-help-1", "2026-07-10"),)
    )
    bundle = _bundle_for_registry(registry)

    with pytest.raises(RegistryError, match="google-test-v1.*G-1.*google-help-1.*expired"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_accepts_same_day_source_refresh_due():
    registry = _registry_with_required_input(
        "conversions", source_refresh_due=(("google-help-1", "2026-07-11"),)
    )
    bundle = _bundle_for_registry(registry)

    assert registry.validate_report_scoring(bundle) == registry.score_platform(
        "google", bundle["findings"]
    )

def test_enabled_registry_rejects_control_unbound_source_fact():
    registry = _enabled_registry()
    bundle = _bundle_for_registry(registry)
    evidence = bundle["findings"][0]["evidence"][0]
    evidence.update(proof_kind="source_fact", source_id="google-export")

    with pytest.raises(RegistryError, match="unbound evidence source"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_duplicate_evidence_ids_across_findings():
    registry = _enabled_registry()
    profile = registry.profiles[0]
    registry = ControlRegistry(
        entries=registry.entries,
        profiles=(
            ScoringProfile(
                profile.profile_id,
                profile.platform,
                profile.status,
                profile.category_weights,
                ("G-1", "G-2"),
                profile.disabled_reason,
            ),
        ),
    )
    bundle = _bundle_for_registry(registry)
    bundle["control_definitions"].append(control("G-2"))
    bundle["findings"].append(finding("G-2"))
    bundle["findings"][1]["evidence"][0]["evidence_id"] = (
        bundle["findings"][0]["evidence"][0]["evidence_id"]
    )
    bundle["scoring"] = registry.score_platform("google", bundle["findings"]).to_dict()

    with pytest.raises(RegistryError, match="evidence_id"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_missing_spend_without_key_error():
    registry = _registry_with_required_input("spend")
    bundle = _bundle_for_registry(registry)
    bundle["account_snapshot"].pop("spend")

    with pytest.raises(RegistryError, match="spend"):
        registry.validate_report_scoring(bundle)


@pytest.mark.parametrize("required_input", ["applicability_context", "current_source_support"])
def test_enabled_registry_accepts_available_special_required_input(required_input: str):
    registry = _registry_with_required_input(required_input)
    bundle = _bundle_for_registry(registry)

    assert registry.validate_report_scoring(bundle) == registry.score_platform("google", bundle["findings"])


def test_enabled_registry_rejects_current_account_evidence_without_observation_or_inference():
    registry = _registry_with_required_input("current_account_evidence")
    bundle = _bundle_for_registry(registry)
    bundle["run_manifest"]["sources"].append("google-help-1")
    bundle["findings"][0]["evidence"][0].update(
        proof_kind="source_fact", source_id="google-help-1"
    )
    with pytest.raises(RegistryError, match="current_account_evidence"):
        registry.validate_report_scoring(bundle)


def test_enabled_registry_rejects_current_source_support_without_sources_or_claims():
    registry = _registry_with_required_input("current_source_support")
    health = dict(registry.entries[0].control_definition)
    health["source_ids"] = []
    registry = ControlRegistry(
        entries=(RegistryEntry("google", "G-1", "health", "health", (), health), *registry.entries[1:]),
        profiles=registry.profiles,
    )
    bundle = _bundle_for_registry(registry)

    with pytest.raises(RegistryError, match="current_source_support"):
        registry.validate_report_scoring(bundle)


@pytest.mark.parametrize("unsupported_field", ["account_name", "campaign_name", "campaign_status"])
def test_enabled_registry_rejects_unsupported_direct_required_input(unsupported_field: str):
    registry = _registry_with_required_input(unsupported_field)
    bundle = _bundle_for_registry(registry)
    bundle["account_snapshot"]["measurement_context"]["unsupported_fields"] = [unsupported_field]

    with pytest.raises(RegistryError, match=unsupported_field):
        registry.validate_report_scoring(bundle)

@pytest.mark.parametrize(
    ("required_input", "unsupported_field"),
    [
        ("measurement_context.account_name", "account_name"),
        ("measurement_context.campaign_name", "campaign_name"),
        ("measurement_context.campaign_status", "campaign_status"),
        ("measurement_context:account_name", "account_name"),
        ("measurement_context:campaign_name", "campaign_name"),
        ("measurement_context:campaign_status", "campaign_status"),
    ],
)
def test_enabled_registry_rejects_unsupported_prefixed_required_input(
    required_input: str, unsupported_field: str
):
    registry = _registry_with_required_input(required_input)
    bundle = _bundle_for_registry(registry)
    bundle["account_snapshot"]["measurement_context"]["unsupported_fields"] = [unsupported_field]

    with pytest.raises(RegistryError, match=required_input):
        registry.validate_report_scoring(bundle)






@pytest.mark.parametrize(
    ("required_input", "row"),
    [
        ("campaigns", {}),
        ("creatives", {}),
        ("conversions", {}),
        ("budgets", {}),
    ],
)
def test_enabled_registry_rejects_required_collection_without_usable_row(
    required_input: str, row: dict[str, object]
):
    registry = _registry_with_required_input(required_input)
    bundle = _bundle_for_registry(registry)
    bundle["account_snapshot"][required_input] = [row]

    with pytest.raises(RegistryError, match=required_input):
        registry.validate_report_scoring(bundle)


def test_loader_rejects_health_control_without_expiry(tmp_path: Path, repo_root: Path):
    target = tmp_path / "control-plane" / "manifests"
    target.mkdir(parents=True)
    source = repo_root / "control-plane" / "manifests"
    for name in ("control-registry.json", "scoring-profiles.json", "claim-ledger.json", "source-ledger.json"):
        (target / name).write_bytes((source / name).read_bytes())
    registry = json.loads((target / "control-registry.json").read_text(encoding="utf-8"))
    entry = next(item for item in registry["controls"] if item["source_claim_ids"])
    entry["disposition"] = "health"
    entry["control_definition"].update(
        severity="high",
        maturity="source-grounded",
        scoring_behavior="health",
        stability="stable",
    )
    (target / "control-registry.json").write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(RegistryError, match="expires_at"):
        load_control_registry(tmp_path)


def test_loader_rejects_referenced_claim_without_refresh_due(tmp_path: Path, repo_root: Path):
    target = tmp_path / "control-plane" / "manifests"
    target.mkdir(parents=True)
    source = repo_root / "control-plane" / "manifests"
    for name in ("control-registry.json", "scoring-profiles.json", "claim-ledger.json", "source-ledger.json"):
        (target / name).write_bytes((source / name).read_bytes())
    registry = json.loads((target / "control-registry.json").read_text(encoding="utf-8"))
    claim_ledger = json.loads((target / "claim-ledger.json").read_text(encoding="utf-8"))
    entry = next(item for item in registry["controls"] if item["source_claim_ids"])
    entry["disposition"] = "health"
    entry["control_definition"].update(
        expires_at="2026-09-01",
        severity="high",
        maturity="source-grounded",
        scoring_behavior="health",
        stability="stable",
    )
    claim_id = entry["source_claim_ids"][0]
    next(claim for claim in claim_ledger["claims"] if claim["id"] == claim_id).pop("refresh_due")
    (target / "control-registry.json").write_text(json.dumps(registry), encoding="utf-8")
    (target / "claim-ledger.json").write_text(json.dumps(claim_ledger), encoding="utf-8")

    with pytest.raises(RegistryError, match="refresh_due"):
        load_control_registry(tmp_path)

def test_registry_rejects_non_lowercase_report_platform():
    registry = _enabled_registry()
    bundle = _bundle_for_registry(registry)
    bundle["account_snapshot"]["account"]["platform"] = "Google"

    with pytest.raises(RegistryError, match="platform"):
        registry.validate_report_scoring(bundle)
