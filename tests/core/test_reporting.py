from __future__ import annotations

import copy
import json
import os
import stat
import tomllib
import uuid
from pathlib import Path

import pytest
import claude_ads_core.reporting as reporting

from claude_ads_core.cli import main
from claude_ads_core.contracts import validate_contract
from claude_ads_core.control_registry import ControlRegistry, RegistryEntry, ScoringProfile, load_control_registry
from claude_ads_core.reporting import (
    PDFDependencyError,
    ReportRenderError,
    _windows_reparse,
    atomic_write_report,
    render_html,
    render_markdown,
    render_pdf,
    resolve_report_path,
    write_report_bundle,
)


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "reports"
BUNDLE_PATH = FIXTURE_ROOT / "sanitized-report-bundle.json"
REPO_ROOT = Path(__file__).resolve().parents[2]


def load_bundle() -> dict:
    return json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))


def fixture_registry() -> ControlRegistry:
    controls = load_bundle()["control_definitions"]
    entries = tuple(
        RegistryEntry(
            platform="google",
            control_id=control["control_id"],
            intent="fixture",
            disposition="health",
            source_claim_ids=(),
            control_definition=control,
            source_refresh_due=(("official-google-help", "2026-08-01"),),
        )
        for control in controls
    )
    return ControlRegistry(
        entries=entries,
        profiles=(
            ScoringProfile(
                profile_id="google-fixture-health-v1",
                platform="google",
                status="enabled",
                category_weights={"creative": 20.0, "policy": 30.0, "tracking": 50.0},
                health_control_ids=tuple(control["control_id"] for control in controls),
                disabled_reason=None,
            ),
        ),
    )


def no_score_bundle() -> dict:
    bundle = load_bundle()
    bundle["control_definitions"] = []
    bundle["findings"] = []
    bundle["scoring"] = {
        "health_score": None,
        "evidence_coverage": 0.0,
        "status": "insufficient_evidence",
        "categories": [],
    }
    bundle["contradictions"] = []
    bundle["actions"] = []
    return bundle




def test_product_manifest_advertises_only_executable_report_formats():
    manifest = json.loads(
        (REPO_ROOT / "control-plane" / "manifests" / "product-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["human_renderers"] == ["markdown", "html", "pdf"]
    for output_format in manifest["human_renderers"]:
        assert output_format in {"markdown", "html", "pdf"}

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert any(
        requirement.startswith("weasyprint>=")
        for requirement in project["project"]["optional-dependencies"]["pdf"]
    )


def test_sanitized_report_fixture_is_a_valid_v2_bundle_with_measurement_context():
    bundle = load_bundle()
    validate_contract("report-bundle", bundle)
    assert bundle["schema_version"] == "2.0.0"
    assert bundle["account_snapshot"]["schema_version"] == "2.0.0"
    assert all(finding["schema_version"] == "2.0.0" for finding in bundle["findings"])
    context = bundle["account_snapshot"]["measurement_context"]
    assert context["profile_id"] == "google-fixture-health-v1"
    assert context["unsupported_fields"] == ["budget", "creative_id", "creative_name"]
    assert context["missing_fields"] == [
        "attribution_model",
        "click_attribution_window",
        "counting_behavior",
        "data_finalization",
        "modeled_data_treatment",
        "timezone",
        "view_attribution_window",
    ]
    assert bundle["run_manifest"]["sources"] == [
        "sanitized-google-export.csv",
        "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "official-google-help",
    ]
    assert bundle["account_snapshot"]["campaigns"] == [
        {"campaign_id": "campaign-001", "policy_status": "eligible"}
    ]
    assert bundle["account_snapshot"]["conversions"] == [
        {"action": "primary_conversion_action", "status": "inactive"}
    ]
    assert bundle["findings"][2]["evidence"][0] == {
        "evidence_id": "evidence-google-policy-001",
        "proof_kind": "observation",
        "source_id": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "locator": "input:sanitized-google-export.csv",
        "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "observed_at": "2026-07-11T16:00:00Z",
        "query_id": None,
        "report_id": None,
        "window": {"start": "2026-06-01", "end": "2026-06-30"},
        "report_grain": ["date", "campaign_id"],
        "input_field": "policy_status",
        "redacted_value": "eligible",
        "observation_ref": None,
    }
def test_sanitized_report_fixture_has_exact_category_scores_and_registry_validation():
    bundle = load_bundle()
    expected_categories = [
        {
            "category": "tracking",
            "category_weight": 50.0,
            "health_score": 0.0,
            "evidence_coverage": 100.0,
            "applicable_controls": 1,
            "known_controls": 1,
            "passed_controls": 0,
            "failed_controls": 1,
            "unknown_controls": 0,
        },
        {
            "category": "creative",
            "category_weight": 20.0,
            "health_score": None,
            "evidence_coverage": 0.0,
            "applicable_controls": 1,
            "known_controls": 0,
            "passed_controls": 0,
            "failed_controls": 0,
            "unknown_controls": 1,
        },
        {
            "category": "policy",
            "category_weight": 30.0,
            "health_score": 100.0,
            "evidence_coverage": 100.0,
            "applicable_controls": 1,
            "known_controls": 1,
            "passed_controls": 1,
            "failed_controls": 0,
            "unknown_controls": 0,
        },
    ]
    assert bundle["scoring"]["categories"] == expected_categories
    recomputed = fixture_registry().validate_report_scoring(bundle)
    assert recomputed.to_dict()["categories"] == sorted(
        expected_categories, key=lambda category: category["category"]
    )


def test_markdown_matches_golden_report():
    expected = (FIXTURE_ROOT / "sanitized-report.md").read_text(encoding="utf-8").rstrip("\n") + "\n"
    assert render_markdown(load_bundle(), registry=fixture_registry()) == expected

def test_renderer_rejects_tampered_score_before_returning_content():
    bundle = load_bundle()
    bundle["scoring"]["health_score"] = 99.0
    with pytest.raises(ReportRenderError, match="report scoring does not match"):
        render_markdown(bundle, registry=fixture_registry())


def test_html_matches_golden_report_and_is_self_contained():
    expected = (FIXTURE_ROOT / "sanitized-report.html").read_text(encoding="utf-8").rstrip("\n") + "\n"
    rendered = render_html(load_bundle(), registry=fixture_registry())
    assert rendered == expected
    lowered = rendered.lower()
    assert "<script" not in lowered
    assert "<link" not in lowered
    assert " src=" not in lowered
    assert " url(" not in lowered
def test_report_surfaces_measurement_context_and_provenance():
    bundle = load_bundle()
    markdown = render_markdown(bundle, registry=fixture_registry())
    html = render_html(bundle, registry=fixture_registry())
    for rendered in (markdown, html):
        plain = rendered.replace("\\", "")
        assert "Measurement context" in plain
        assert "google-fixture-health-v1" in plain
        assert "sanitized-report-fixture" in plain
        assert "official-google-help" in plain
        assert "date" in plain and "campaign_id" in plain
        assert "primary_conversion_action" in plain
        assert "None supplied" in plain
        assert "Unknown" in plain
        assert "attribution_model" in plain
        assert "Missing fields" in plain
        assert plain.count("Unsupported fields") == 1
        assert "budget, creative_id, creative_name" in plain
        assert "input:sanitized-google-export.csv" in plain
        assert "evidence-google-policy-001" in plain
def test_report_surfaces_partial_provisional_evidence_contradictions_and_actions():
    markdown = render_markdown(load_bundle(), registry=fixture_registry())
    assert "Run completeness: **Partial**" in markdown
    assert "Evidence status: **Provisional**" in markdown
    assert "must not be presented as a complete audit" in markdown
    assert "Campaign eligibility is present" in markdown
    assert "Verify and repair the primary conversion action" in markdown
    assert '"evidence_id":"evidence-google-tracking-001"' in markdown

def test_report_footers_state_registry_recomputed_scores():
    expected = (
        "Scores were recomputed from the supplied control registry and verified "
        "against this ReportBundle before rendering."
    )
    rendered_outputs = (
        render_markdown(load_bundle(), registry=fixture_registry()),
        render_html(load_bundle(), registry=fixture_registry()),
    )
    for rendered in rendered_outputs:
        assert rendered.count(expected) == 1
        assert "Scores were not recalculated" not in rendered


def test_insufficient_evidence_is_visible_and_health_is_not_invented():
    bundle = no_score_bundle()
    registry = load_control_registry(REPO_ROOT)
    markdown = render_markdown(bundle, registry=registry)
    html = render_html(bundle, registry=registry)
    assert "Evidence status: **Insufficient evidence**" in markdown
    assert "Health score: **Not scored**" in markdown
    assert "Evidence is insufficient for a defensible health score" in markdown
    assert "Evidence status: Insufficient evidence" in html
    assert "Health score<strong>Not scored</strong>" in html


def test_rendering_is_reproducible_and_does_not_mutate_input():
    bundle = load_bundle()
    original = copy.deepcopy(bundle)
    registry = fixture_registry()
    assert render_markdown(bundle, registry=registry) == render_markdown(bundle, registry=registry)
    assert render_html(bundle, registry=registry) == render_html(bundle, registry=registry)
    assert bundle == original


def test_untrusted_content_is_escaped_and_obvious_credentials_and_pii_are_redacted():
    bundle = load_bundle()
    finding = bundle["findings"][0]
    finding["observation"] = (
        "Contact analyst@example.test with access_token=secret-value "
        "and <script>alert('untrusted')</script>."
    )
    finding["evidence"].append(
        {
            "evidence_id": "evidence-google-tracking-sensitive-001",
            "proof_kind": "observation",
            "source_id": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "locator": "input:sanitized-google-export.csv",
            "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "observed_at": "2026-07-11T16:00:00Z",
            "query_id": None,
            "report_id": None,
            "window": {"start": "2026-06-01", "end": "2026-06-30"},
            "report_grain": ["date", "campaign_id"],
            "input_field": "authorization",
            "redacted_value": "Bearer credential-value; person@example.test",
            "observation_ref": None,
        }
    )
    markdown = render_markdown(bundle, registry=fixture_registry())
    rendered_html = render_html(bundle, registry=fixture_registry())
    combined = markdown + rendered_html
    assert "analyst@example.test" not in combined
    assert "person@example.test" not in combined
    assert "secret-value" not in combined
    assert "credential-value" not in combined
    assert "<script>" not in rendered_html
    assert "&lt;script&gt;" in rendered_html
    assert "[REDACTED]" in combined

def test_header_credentials_redact_full_values_without_crossing_lines():
    rendered = reporting._redact_text(
        "aUtHoRiZaTiOn: Basic basic-secret\r\n"
        "Next-Header: keep-basic\r\n"
        "pRoXy-AuThOrIzAtIoN: Bearer proxy-secret\n"
        "Next-Text: keep-this-text\n"
        "cOoKiE: session=one; preference=two\n"
        "Next-Header: keep-pairs\r\n"
        "sEt-Cookie: \"quoted-secret\"; Path=/; HttpOnly\r\n"
        "Next-Header: keep-quoted\n"
        "Observed Authorization: Basic inline-secret\n"
        "Inline Cookie: sid=one; csrf=two\n"
        "Final-Text: keep-final"
    )

    assert rendered == (
        "aUtHoRiZaTiOn: [REDACTED]\n"
        "Next-Header: keep-basic\n"
        "pRoXy-AuThOrIzAtIoN: [REDACTED]\n"
        "Next-Text: keep-this-text\n"
        "cOoKiE: [REDACTED]\n"
        "Next-Header: keep-pairs\n"
        "sEt-Cookie: [REDACTED]\n"
        "Next-Header: keep-quoted\n"
        "Observed Authorization: [REDACTED]\n"
        "Inline Cookie: [REDACTED]\n"
        "Final-Text: keep-final"
    )


def test_renderer_rejects_invalid_bundle_and_malformed_extensions():
    invalid = load_bundle()
    invalid["schema_version"] = "1.0.0"
    with pytest.raises(ReportRenderError, match="invalid report bundle"):
        render_markdown(invalid, registry=fixture_registry())

    invalid = load_bundle()
    invalid["actions"] = {"action": "not an array"}
    with pytest.raises(ReportRenderError, match=r"\$\.actions"):
        render_html(invalid, registry=fixture_registry())


@pytest.mark.parametrize("destination", ["../outside.md", "/tmp/outside.md", "nested/../../outside.md"])
def test_safe_report_path_rejects_absolute_and_traversal_paths(tmp_path, destination):
    with pytest.raises(ReportRenderError, match="relative path"):
        resolve_report_path(tmp_path / "reports", destination)

@pytest.mark.parametrize(
    "destination",
    [
        "",
        "/absolute/report.md",
        "drive:report.md",
        "a//report.md",
        "a/./report.md",
        "a/../report.md",
        "a\\report.md",
        "run\x00/report.md",
        "run./report.md",
        "run /report.md",
        "report.md.",
        "report.md ",
        "CON/report.md",
        "PRN.txt",
        "AUX/report.md",
        "NUL.txt",
        "COM1/report.md",
        "COM¹.txt",
        "COM².txt",
        "COM³.txt",
        "COM1.txt",
        "LPT¹/report.md",
        "LPT²/report.md",
        "LPT³/report.md",
        "LPT9.txt",
        "report.md:stream",
        "report<.md",
        'report".md',
        "report>.md",
        "report|.md",
        "report?.md",
        "report*.md",
        *[f"report{chr(code)}.md" for code in range(1, 32)],
    ],
)
def test_report_destination_grammar_rejects_unsafe_components(tmp_path, destination):
    with pytest.raises(ReportRenderError, match="relative path"):
        resolve_report_path(tmp_path / "reports", destination)


def test_report_destination_grammar_preserves_path_as_posix(tmp_path):
    assert resolve_report_path(tmp_path / "reports", Path("run/report.md")) == (
        tmp_path / "reports"
    ).absolute() / "run/report.md"
def test_resolve_report_path_is_non_mutating_for_nominal_nested_destination(tmp_path):
    root = tmp_path / "reports"
    resolved = resolve_report_path(root, "run-1/report.md")
    assert resolved == root.absolute() / "run-1/report.md"
    assert not root.exists()


def test_atomic_report_write_fails_before_root_creation_without_posix_capabilities(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX capability gate")
    root = tmp_path / "reports"
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(ReportRenderError, match="capabilit"):
        atomic_write_report(root, "report.md", b"report\n")
    assert not root.exists()


def test_atomic_report_write_requires_getuid_before_root_creation(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX capability gate")
    root = tmp_path / "reports"
    monkeypatch.delattr(os, "getuid", raising=False)
    with pytest.raises(ReportRenderError, match="capabilit"):
        atomic_write_report(root, "report.md", b"report\n")
    assert not root.exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows coverage")
def test_windows_root_boundary_rejects_outside_home_before_mutation():
    root = Path.home().parent / f"claude-ads-phase2-shared-{uuid.uuid4().hex}"
    assert not root.exists()
    with pytest.raises(ReportRenderError, match="beneath"):
        atomic_write_report(root, "report.md", b"report\n")
    assert not root.exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows coverage")
def test_windows_missing_reparse_metadata_fails_closed():
    class MissingAttributes:
        st_mode = stat.S_IFDIR

    with pytest.raises(ReportRenderError, match="metadata"):
        _windows_reparse(MissingAttributes())


def _secure_windows_acl() -> dict:
    sid = "S-1-5-21-current"
    return {
        "owner_sid": sid,
        "current_sid": sid,
        "access": [{"sid": sid, "type": "Allow", "rights": "FullControl"}],
    }


def test_windows_acl_rejects_permissive_entries_before_write(monkeypatch, tmp_path):
    acl = _secure_windows_acl()
    acl["access"].append(
        {"sid": "S-1-1-0", "type": "Allow", "rights": "FullControl"}
    )
    monkeypatch.setattr(reporting, "_windows_acl_snapshot", lambda _path: acl)
    with pytest.raises(ReportRenderError, match="permissive"):
        reporting._validate_windows_acl(tmp_path / "reports", "root")

def test_windows_atomic_write_rejects_permissive_home_before_root_creation(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = home / "reports"
    acl = _secure_windows_acl()
    acl["access"].append({"sid": "S-1-1-0", "type": "Allow", "rights": "FullControl"})
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(reporting, "_windows_acl_snapshot", lambda _path: acl)
    with pytest.raises(ReportRenderError, match="permissive"):
        reporting._atomic_write_windows(root, "report.md", b"report\n")
    assert not root.exists()




def test_windows_acl_query_passes_path_as_uninterpolated_argument(monkeypatch, tmp_path):
    path = tmp_path / "report & $HOME.md"
    calls = []

    class Result:
        returncode = 0
        stdout = json.dumps(_secure_windows_acl())
        stderr = ""

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr(reporting.subprocess, "run", fake_run)
    assert reporting._windows_acl_snapshot(path) == _secure_windows_acl()
    argv = calls[0][0][0]
    command = argv[0]
    script = argv[argv.index("-Command") + 1]
    assert calls[0][1]["shell"] is False
    assert command == "powershell.exe"
    assert str(path) not in script
    assert argv[-1] == str(path)


def test_windows_current_user_only_protection_applies_and_verifies_acl(monkeypatch, tmp_path):
    path = tmp_path / "report.md"
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr(reporting.subprocess, "run", fake_run)
    monkeypatch.setattr(reporting, "_windows_acl_snapshot", lambda _path: _secure_windows_acl())
    reporting._protect_windows_path(path)
    assert calls
    argv = calls[0][0][0]
    script = argv[argv.index("-Command") + 1]
    assert str(path) not in script
    assert argv[-1] == str(path)
    assert "Set-Acl" in script



def test_windows_atomic_write_applies_current_user_acl_on_non_windows(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = home / "reports"
    protected = []
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(reporting, "_windows_reparse", lambda _info: False)
    monkeypatch.setattr(reporting, "_windows_acl_snapshot", lambda _path: _secure_windows_acl())
    monkeypatch.setattr(reporting, "_protect_windows_path", protected.append)
    monkeypatch.setattr(reporting.os, "chmod", lambda *_args: (_ for _ in ()).throw(AssertionError("chmod")))
    output = reporting._atomic_write_windows(root, "report.md", b"report\n")
    assert output.read_bytes() == b"report\n"
    assert len(protected) == 3
    assert protected[0] == root
    assert protected[1].parent == root
    assert protected[1].name.startswith(".report.md.")
    assert protected[2] == output




@pytest.mark.skipif(os.name != "nt", reason="native Windows coverage")
@pytest.mark.parametrize("target", ["root", "parent", "leaf"])
def test_windows_reparse_proxy_rejects_root_parent_and_leaf(tmp_path, monkeypatch, target):
    home = tmp_path / "home"
    home.mkdir()
    root = home / "reports"
    root.mkdir()
    parent = root / "run"
    parent.mkdir()
    leaf = parent / "report.md"
    leaf.write_bytes(b"old")
    selected = {"root": root, "parent": parent, "leaf": leaf}[target]
    original_lstat = Path.lstat

    class ReparseInfo:
        st_mode = stat.S_IFDIR if selected != leaf else stat.S_IFREG
        st_file_attributes = 0x400

    def proxy_lstat(path):
        if path == selected:
            return ReparseInfo()
        return original_lstat(path)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(Path, "lstat", proxy_lstat)
    with pytest.raises(ReportRenderError, match="reparse"):
        atomic_write_report(root, "run/report.md", b"report\n")

@pytest.mark.skipif(os.name != "nt", reason="native Windows coverage")
def test_windows_write_error_normalizes_and_cleans_temporary_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    root = home / "reports"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(os, "write", lambda fd, data: (_ for _ in ()).throw(OSError("windows write failure")))
    with pytest.raises(ReportRenderError, match="windows write failure"):
        atomic_write_report(root, "report.md", b"report\n")
    assert not list(root.glob(".report.md.*"))


@pytest.mark.skipif(os.name != "nt", reason="native Windows coverage")
def test_windows_post_replace_corruption_fails_exact_byte_verification(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    root = home / "reports"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    original_replace = os.replace

    def corrupting_replace(source, destination):
        original_replace(source, destination)
        Path(destination).write_bytes(b"corrupt")

    monkeypatch.setattr(os, "replace", corrupting_replace)
    with pytest.raises(ReportRenderError, match="unexpected content"):
        atomic_write_report(root, "report.md", b"report\n")

@pytest.mark.skipif(os.name != "nt", reason="native Windows coverage")
def test_windows_post_replace_temp_lstat_error_reports_unknown_without_cleanup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    root = home / "reports"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    original_lstat = Path.lstat
    original_unlink = Path.unlink
    replaced = False
    unlinked = []

    def failing_temp_lstat(path):
        if replaced and path.name.startswith(".report.md."):
            raise PermissionError("post-replace temp lstat denied")
        return original_lstat(path)

    original_replace = os.replace

    def recording_replace(source, destination):
        nonlocal replaced
        result = original_replace(source, destination)
        replaced = True
        return result

    def recording_unlink(path, *args, **kwargs):
        unlinked.append(path)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", recording_replace)
    monkeypatch.setattr(Path, "lstat", failing_temp_lstat)
    monkeypatch.setattr(Path, "unlink", recording_unlink)
    with pytest.raises(ReportRenderError, match="replacement outcome is unknown"):
        atomic_write_report(root, "report.md", b"report\n")
    assert (root / "report.md").read_bytes() == b"report\n"
    assert not any(path.name.startswith(".report.md.") for path in unlinked)


@pytest.mark.skipif(os.name != "nt", reason="native Windows coverage")
def test_windows_keyboard_interrupt_rethrows_and_cleans_owned_temp(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    root = home / "reports"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(os, "write", lambda fd, data: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        atomic_write_report(root, "report.md", b"report\n")
    assert not (root / "report.md").exists()
    assert not list(root.glob(".report.md.*"))

def test_atomic_report_write_rejects_unsupported_platform_before_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "plan9")
    root = tmp_path / "reports"
    with pytest.raises(ReportRenderError, match="unsupported"):
        atomic_write_report(root, "report.md", b"report\n")
    assert not root.exists()



def test_atomic_report_write_rejects_private_root_ownership_before_temp_creation(tmp_path, monkeypatch):
    if os.name != "posix" or not hasattr(os, "getuid"):
        pytest.skip("POSIX ownership")
    root = tmp_path / "reports"
    root.mkdir(mode=0o700)
    original_fstat = os.fstat
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    os.close(root_fd)
    monkeypatch.setattr(
        os,
        "fstat",
        lambda fd: type("Stat", (), {"st_uid": os.getuid() + 1, "st_mode": stat.S_IFDIR | 0o700})()
        if fd != 0
        else original_fstat(fd),
    )
    with pytest.raises(ReportRenderError, match="owned"):
        atomic_write_report(root, "report.md", b"report\n")
    assert list(root.glob(".report.md.*")) == []


def test_atomic_report_write_rejects_group_writable_root_before_temp_creation(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX permissions")
    root = tmp_path / "reports"
    root.mkdir(mode=0o770)
    root.chmod(0o770)
    with pytest.raises(ReportRenderError, match="private"):
        atomic_write_report(root, "report.md", b"report\n")
    assert list(root.glob(".report.md.*")) == []

@pytest.mark.parametrize("destination", ["report.md", "run/report.md"])
def test_atomic_report_write_rejects_mode_755_root_or_parent_before_temp(tmp_path, destination):
    if os.name != "posix":
        pytest.skip("POSIX permissions")
    root = tmp_path / "reports"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    if destination == "report.md":
        root.chmod(0o755)
    else:
        (root / "run").mkdir(mode=0o755)
        (root / "run").chmod(0o755)
    with pytest.raises(ReportRenderError, match="private"):
        atomic_write_report(root, destination, b"report\n")
    assert not list(root.rglob(".report.md.*"))

def test_atomic_report_write_closes_child_fd_on_parent_validation_failure(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX descriptor anchoring")
    root = tmp_path / "reports"
    child_fds = []
    closed = []
    original_open = os.open
    original_fstat = os.fstat
    original_close = os.close

    def recording_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if path == "run" and kwargs.get("dir_fd") is not None:
            child_fds.append(fd)
        return fd

    def failing_fstat(fd):
        if fd in child_fds:
            raise OSError("parent validation failure")
        return original_fstat(fd)

    def recording_close(fd):
        closed.append(fd)
        if fd in child_fds:
            original_close(fd)
            raise OSError("child close failure")
        return original_close(fd)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "fstat", failing_fstat)
    monkeypatch.setattr(os, "close", recording_close)
    with pytest.raises(ReportRenderError, match="parent validation failure.*child close failure"):
        atomic_write_report(root, "run/report.md", b"report\n")
    assert child_fds
    assert child_fds[0] in closed


def test_atomic_report_write_fsyncs_new_parent_before_next_operation(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX descriptor anchoring")
    root = tmp_path / "reports"
    events = []
    original_mkdir = os.mkdir
    original_fsync = os.fsync

    def recording_mkdir(path, mode=0o777, *, dir_fd=None):
        events.append(("mkdir", path, dir_fd))
        return original_mkdir(path, mode, dir_fd=dir_fd)

    def recording_fsync(fd):
        events.append(("fsync", fd))
        return original_fsync(fd)

    monkeypatch.setattr(os, "mkdir", recording_mkdir)
    monkeypatch.setattr(os, "fsync", recording_fsync)
    atomic_write_report(root, "run/report.md", b"report\n")
    nested_mkdir = next(index for index, event in enumerate(events) if event[0] == "mkdir" and event[1] == "run")
    assert events[nested_mkdir + 1][0] == "fsync"


def test_atomic_report_write_parent_swap_at_replace_cannot_escape_held_parent(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX descriptor anchoring")
    root = tmp_path / "reports"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "report.md"
    sentinel.write_bytes(b"sentinel")
    original_replace = os.replace
    swapped = False

    def swapping_replace(source, destination, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            (root / "run").rename(root / "run-parked")
            (root / "run").symlink_to(outside, target_is_directory=True)
        return original_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", swapping_replace)
    with pytest.raises(ReportRenderError, match="replacement occurred"):
        atomic_write_report(root, "run/report.md", b"report\n")
    assert sentinel.read_bytes() == b"sentinel"
    assert not (outside / ".report.md").exists()
    assert (root / "run-parked" / "report.md").read_bytes() == b"report\n"
    assert not list((root / "run-parked").glob(".report.md.*"))


def test_atomic_report_write_late_destination_symlink_replaces_leaf_not_target(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX descriptor anchoring")
    root = tmp_path / "reports"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "report.md"
    sentinel.write_bytes(b"sentinel")
    original_replace = os.replace
    inserted = False

    def inserting_symlink(source, destination, **kwargs):
        nonlocal inserted
        if not inserted:
            inserted = True
            (root / "report.md").symlink_to(sentinel)
        return original_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", inserting_symlink)
    output = atomic_write_report(root, "report.md", b"report\n")
    assert output.read_bytes() == b"report\n"
    assert sentinel.read_bytes() == b"sentinel"
    assert output.is_file() and not output.is_symlink()


def test_atomic_report_write_final_namespace_identity_mismatch_fails_after_replacement(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX descriptor anchoring")
    root = tmp_path / "reports"
    original_replace = os.replace
    replaced = False

    def replacing_then_swap_root(source, destination, **kwargs):
        nonlocal replaced
        result = original_replace(source, destination, **kwargs)
        if not replaced:
            replaced = True
            root.rename(tmp_path / "reports-parked")
            root.mkdir(mode=0o700)
        return result

    monkeypatch.setattr(os, "replace", replacing_then_swap_root)
    with pytest.raises(ReportRenderError, match="replacement occurred"):
        atomic_write_report(root, "report.md", b"report\n")
    assert (tmp_path / "reports-parked" / "report.md").read_bytes() == b"report\n"
    assert not (root / "report.md").exists()

def test_atomic_report_write_parent_swap_to_real_directory_fails_identity(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX descriptor anchoring")
    root = tmp_path / "reports"
    outside = tmp_path / "replacement"
    outside.mkdir()
    original_replace = os.replace
    swapped = False

    def swapping_replace(source, destination, **kwargs):
        nonlocal swapped
        result = original_replace(source, destination, **kwargs)
        if not swapped:
            swapped = True
            (root / "run").rename(root / "run-parked")
            (root / "run").mkdir(mode=0o700)
        return result

    monkeypatch.setattr(os, "replace", swapping_replace)
    with pytest.raises(ReportRenderError, match="replacement occurred"):
        atomic_write_report(root, "run/report.md", b"report\n")
    assert (root / "run-parked" / "report.md").read_bytes() == b"report\n"
    assert not (root / "run" / "report.md").exists()


def test_atomic_report_write_rejects_identical_bytes_with_replaced_leaf_identity(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX descriptor anchoring")
    root = tmp_path / "reports"
    original_replace = os.replace
    swapped = False

    def replacing_leaf_with_copy(source, destination, **kwargs):
        nonlocal swapped
        result = original_replace(source, destination, **kwargs)
        if not swapped:
            swapped = True
            replacement = root / "report.copy"
            replacement.write_bytes(b"report\n")
            (root / "report.md").unlink()
            replacement.rename(root / "report.md")
        return result

    monkeypatch.setattr(os, "replace", replacing_leaf_with_copy)
    with pytest.raises(ReportRenderError, match="replacement occurred"):
        atomic_write_report(root, "report.md", b"report\n")

def test_atomic_report_write_surfaces_outer_close_failures_after_commit(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX descriptor backend")
    root = tmp_path / "reports"
    original_parent = reporting._open_posix_parent
    original_close = os.close
    held_descriptors = set()

    def recording_parent(root_fd, parts):
        parent_fd, descriptors = original_parent(root_fd, parts)
        held_descriptors.update(descriptors)
        return parent_fd, descriptors

    def failing_held_close(fd):
        if fd in held_descriptors:
            original_close(fd)
            raise OSError("outer descriptor close failure")
        return original_close(fd)

    monkeypatch.setattr(reporting, "_open_posix_parent", recording_parent)
    monkeypatch.setattr(os, "close", failing_held_close)
    with pytest.raises(ReportRenderError, match="replacement occurred.*outer descriptor close failure"):
        atomic_write_report(root, "report.md", b"report\n")
    assert (root / "report.md").read_bytes() == b"report\n"



def test_write_report_bundle_rejects_empty_destination_before_pdf_render(tmp_path, monkeypatch):
    def unexpected_import(name: str):
        raise AssertionError("PDF renderer should not be imported")

    monkeypatch.setattr("claude_ads_core.reporting.importlib.import_module", unexpected_import)
    root = tmp_path / "reports"
    with pytest.raises(ReportRenderError, match="relative path"):
        write_report_bundle(
            no_score_bundle(),
            "pdf",
            root,
            "",
            registry=load_control_registry(REPO_ROOT),
        )
    assert not root.exists()


def test_safe_report_path_rejects_parent_and_destination_symlinks(tmp_path):
    root = tmp_path / "reports"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
        (root / "report.md").symlink_to(outside / "report.md")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(ReportRenderError, match="parent must not"):
        atomic_write_report(root, "linked/report.md", b"report\n")
    with pytest.raises(ReportRenderError, match="output must not"):
        atomic_write_report(root, "report.md", b"report\n")


def test_atomic_report_write_uses_private_permissions_and_leaves_no_temp_file(tmp_path):
    output = atomic_write_report(tmp_path / "reports", "run-1/report.md", "report\n")
    assert output.read_text(encoding="utf-8") == "report\n"
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert list(output.parent.glob(".report.md.*")) == []


def test_atomic_report_write_verifies_binary_content_without_text_translation(tmp_path, monkeypatch):
    payload = b"line1\r\n\x1a\xff\x00line2"
    native_binary_flag = getattr(os, "O_BINARY", 0)
    simulated_binary_flag = 0 if native_binary_flag else 1 << 30
    binary_flag = native_binary_flag or simulated_binary_flag
    monkeypatch.setattr(os, "O_BINARY", binary_flag, raising=False)
    original_open = os.open
    open_calls = []

    def recording_open(path, flags, *args, **kwargs):
        open_calls.append((Path(path), flags))
        if not native_binary_flag:
            flags &= ~simulated_binary_flag
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    output = atomic_write_report(tmp_path / "reports", "run-1/report.bin", payload)
    assert output.read_bytes() == payload
    assert any(flags & binary_flag == binary_flag for _, flags in open_calls)
    assert list(output.parent.glob(".report.bin.*")) == []

@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor backend")
def test_atomic_report_write_normalizes_temporary_open_error_before_root_temp(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    original_open = os.open

    def failing_temp_open(path, flags, *args, **kwargs):
        if flags & os.O_EXCL:
            raise OSError("temporary open failure")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_temp_open)
    with pytest.raises(ReportRenderError, match="temporary open failure"):
        atomic_write_report(root, "report.md", b"report\n")
    assert not list(root.rglob(".report.md.*"))

def test_atomic_report_write_rejects_noop_replace_and_leaves_no_temp_file(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    root.mkdir(mode=0o700)
    destination = root / "report.md"
    payload = b"report\n"
    destination.write_bytes(payload)
    monkeypatch.setattr(os, "replace", lambda source, destination, **kwargs: None)
    with pytest.raises(ReportRenderError, match="no-op"):
        atomic_write_report(root, "report.md", payload)
    assert destination.read_bytes() == payload
    assert list(root.glob(".report.md.*")) == []



@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor backend")
def test_atomic_report_write_post_replace_temp_stat_error_reports_unknown_without_cleanup(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    original_stat = os.stat
    original_replace = os.replace
    original_unlink = os.unlink
    replaced = False
    unlinked = []

    def failing_temp_stat(path, *args, **kwargs):
        if replaced and isinstance(path, str) and path.startswith(".report.md."):
            raise PermissionError("post-replace temp stat denied")
        return original_stat(path, *args, **kwargs)

    def recording_replace(source, destination, **kwargs):
        nonlocal replaced
        result = original_replace(source, destination, **kwargs)
        replaced = True
        return result

    def recording_unlink(path, *args, **kwargs):
        unlinked.append(path)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", recording_replace)
    monkeypatch.setattr(os, "stat", failing_temp_stat)
    monkeypatch.setattr(os, "unlink", recording_unlink)
    with pytest.raises(ReportRenderError, match="replacement outcome is unknown"):
        atomic_write_report(root, "report.md", b"report\n")
    assert (root / "report.md").read_bytes() == b"report\n"
    assert not any(isinstance(path, str) and path.startswith(".report.md.") for path in unlinked)


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor backend")
def test_atomic_report_write_replace_interrupt_is_unknown_and_does_not_cleanup(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    original_replace = os.replace
    original_unlink = os.unlink
    unlinked = []

    def replacing_then_interrupt(source, destination, **kwargs):
        original_replace(source, destination, **kwargs)
        raise KeyboardInterrupt()

    def recording_unlink(path, *args, **kwargs):
        unlinked.append(path)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replacing_then_interrupt)
    monkeypatch.setattr(os, "unlink", recording_unlink)
    with pytest.raises(KeyboardInterrupt) as raised:
        atomic_write_report(root, "report.md", b"report\n")
    assert (root / "report.md").read_bytes() == b"report\n"
    assert any("replacement outcome is unknown" in note for note in raised.value.__notes__)
    assert not any(isinstance(path, str) and path.startswith(".report.md.") for path in unlinked)


@pytest.mark.skipif(os.name != "nt", reason="native Windows coverage")
def test_windows_replace_interrupt_is_unknown_and_does_not_cleanup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    root = home / "reports"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    original_replace = os.replace
    original_unlink = Path.unlink
    unlinked = []

    def replacing_then_interrupt(source, destination):
        original_replace(source, destination)
        raise KeyboardInterrupt()

    def recording_unlink(path, *args, **kwargs):
        unlinked.append(path)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replacing_then_interrupt)
    monkeypatch.setattr(Path, "unlink", recording_unlink)
    with pytest.raises(KeyboardInterrupt) as raised:
        atomic_write_report(root, "report.md", b"report\n")
    assert (root / "report.md").read_bytes() == b"report\n"
    assert any("replacement outcome is unknown" in note for note in raised.value.__notes__)
    assert not any(path.name.startswith(".report.md.") for path in unlinked)

@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor backend")
def test_atomic_report_write_keyboard_interrupt_rethrows_and_cleans_owned_temp(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    monkeypatch.setattr(os, "write", lambda fd, data: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        atomic_write_report(root, "report.md", b"report\n")
    assert not (root / "report.md").exists()
    assert not list(root.glob(".report.md.*"))


def test_resolve_report_path_wraps_root_normalization_oserror(tmp_path, monkeypatch):
    def fail_absolute(path):
        raise OSError("root normalization failure")

    monkeypatch.setattr(Path, "absolute", fail_absolute)
    with pytest.raises(ReportRenderError, match="root normalization failure"):
        resolve_report_path(tmp_path / "reports", "report.md")
def test_atomic_report_write_normalizes_replace_oserror(tmp_path, monkeypatch):
    def fail_replace(source, destination, **kwargs):
        raise OSError("replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(ReportRenderError, match="replace failure"):
        atomic_write_report(tmp_path / "reports", "report.md", b"report\n")

@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor backend")
def test_atomic_report_write_closes_owned_fd_after_write_error(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    original_open = os.open
    owned_fds = []

    def recording_open(path, flags, *args, **kwargs):
        file_descriptor = original_open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL:
            owned_fds.append(file_descriptor)
        return file_descriptor

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "write", lambda file_descriptor, data: (_ for _ in ()).throw(OSError("write-stage failure")))
    with pytest.raises(ReportRenderError, match="write-stage failure"):
        atomic_write_report(root, "report.md", b"report\n")
    assert owned_fds
    with pytest.raises(OSError):
        os.fstat(owned_fds[0])
    assert list(root.glob(".report.md.*")) == []

@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor backend")
def test_atomic_report_write_preserves_primary_error_when_cleanup_fails(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    monkeypatch.setattr(os, "write", lambda file_descriptor, data: (_ for _ in ()).throw(OSError("write-stage failure")))
    monkeypatch.setattr(os, "unlink", lambda path, *args, **kwargs: (_ for _ in ()).throw(OSError("cleanup failure")))
    with pytest.raises(ReportRenderError, match="write-stage failure.*cleanup failure"):
        atomic_write_report(root, "report.md", b"report\n")


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor backend")
def test_atomic_report_write_surfaces_verification_close_error(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    original_open = os.open
    original_close = os.close
    verification_fds = []

    def recording_open(path, flags, *args, **kwargs):
        file_descriptor = original_open(path, flags, *args, **kwargs)
        if path == "report.md" and not flags & os.O_DIRECTORY:
            verification_fds.append(file_descriptor)
        return file_descriptor

    def failing_close(file_descriptor):
        if file_descriptor in verification_fds:
            original_close(file_descriptor)
            raise OSError("verification close failure")
        return original_close(file_descriptor)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "close", failing_close)
    with pytest.raises(ReportRenderError, match="verification close failure"):
        atomic_write_report(root, "report.md", b"report\n")


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor backend")
def test_atomic_report_write_preserves_verification_error_when_close_fails(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    original_open = os.open
    original_close = os.close
    original_fstat = os.fstat
    verification_fds = []

    def recording_open(path, flags, *args, **kwargs):
        file_descriptor = original_open(path, flags, *args, **kwargs)
        if path == "report.md" and not flags & os.O_DIRECTORY:
            verification_fds.append(file_descriptor)
        return file_descriptor

    def failing_fstat(file_descriptor):
        if file_descriptor in verification_fds:
            raise OSError("verification fstat failure")
        return original_fstat(file_descriptor)

    def failing_close(file_descriptor):
        if file_descriptor in verification_fds:
            original_close(file_descriptor)
            raise OSError("verification close failure")
        return original_close(file_descriptor)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "fstat", failing_fstat)
    monkeypatch.setattr(os, "close", failing_close)
    with pytest.raises(ReportRenderError, match="verification fstat failure"):
        atomic_write_report(root, "report.md", b"report\n")


@pytest.mark.parametrize("pdf_output", [b"", b"not-pdf", b"%PDF-1.7"])
def test_pdf_bridge_rejects_empty_and_non_pdf_output(monkeypatch, pdf_output):
    class FakeHTML:
        def __init__(self, *, string, base_url):
            self.string = string
            self.base_url = base_url

        def write_pdf(self):
            return pdf_output

    class FakeWeasyPrint:
        HTML = FakeHTML

    monkeypatch.setattr(
        "claude_ads_core.reporting.importlib.import_module",
        lambda name: FakeWeasyPrint,
    )
    with pytest.raises(ReportRenderError, match="invalid PDF"):
        render_pdf(load_bundle(), registry=fixture_registry())


def test_pdf_bridge_fails_clearly_when_optional_dependency_is_unavailable(monkeypatch):
    def unavailable(name: str):
        raise ImportError(name)

    monkeypatch.setattr("claude_ads_core.reporting.importlib.import_module", unavailable)
    with pytest.raises(PDFDependencyError, match="optional 'weasyprint'"):
        render_pdf(load_bundle(), registry=fixture_registry())


def test_real_pdf_render_smoke_when_runtime_dependencies_are_installed():
    try:
        import weasyprint  # noqa: F401
    except (ImportError, OSError) as exc:
        pytest.skip(f"native PDF dependencies unavailable: {exc}")
    rendered = render_pdf(load_bundle(), registry=fixture_registry())
    assert rendered.startswith(b"%PDF-")
    assert len(rendered) > 1_000


def test_cli_render_writes_validated_report_under_safe_root(tmp_path, capsys):
    root = tmp_path / "runs"
    bundle_path = tmp_path / "report.json"
    bundle = no_score_bundle()
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    assert (
        main(
            [
                "render",
                str(bundle_path),
                "--format",
                "html",
                "--root",
                str(root),
                "--registry-root",
                str(REPO_ROOT),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    output = root / "fixture-run-20260711-001" / "report.html"
    assert result == {
        "format": "html",
        "path": str(output),
        "run_id": "fixture-run-20260711-001",
        "status": "rendered",
    }
    assert output.read_text(encoding="utf-8") == render_html(
        bundle, registry=load_control_registry(REPO_ROOT)
    )


def test_cli_render_rejects_forged_score_before_writing(tmp_path, capsys):
    root = tmp_path / "runs"
    bundle_path = tmp_path / "report.json"
    bundle = no_score_bundle()
    bundle["scoring"].update(health_score=100.0, evidence_coverage=100.0, status="normal")
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    assert (
        main(
            [
                "report",
                str(bundle_path),
                "--root",
                str(root),
                "--registry-root",
                str(REPO_ROOT),
            ]
        )
        == 2
    )
    result = json.loads(capsys.readouterr().err)
    assert result["status"] == "invalid"
    assert "report scoring does not match profile" in result["error"]
    assert not list(root.rglob("*"))


def test_cli_render_returns_machine_readable_error_for_unsafe_output(tmp_path, capsys):
    bundle_path = tmp_path / "report.json"
    bundle_path.write_text(json.dumps(no_score_bundle()), encoding="utf-8")
    assert (
        main(
            [
                "report",
                str(bundle_path),
                "--root",
                str(tmp_path / "runs"),
                "--registry-root",
                str(REPO_ROOT),
                "--output",
                "../outside.md",
            ]
        )
        == 2
    )
    result = json.loads(capsys.readouterr().err)
    assert result["status"] == "invalid"
    assert "relative path" in result["error"]
