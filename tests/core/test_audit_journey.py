from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_ads_core.audit import AuditError, run_audit
from claude_ads_core.cli import main
from claude_ads_core.contracts import load_contract, validate_contract
from claude_ads_core.doctor import run_doctor
from claude_ads_core.setup import generate_setup_profile, SetupError
from claude_ads_core.workflow_contracts import validate_workflow_contract


REPO_ROOT = Path(__file__).resolve().parents[2]
GOOGLE_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "native_exports" / "google.csv"


def test_doctor_diagnostics_succeeds():
    result = run_doctor(root=REPO_ROOT)
    assert result["status"] == "ok"
    assert result["python"]["supported"] is True
    assert result["registry"]["status"] == "ok"
    assert result["registry"]["entries_count"] > 0
    assert result["filesystem"]["writable"] is True


def test_setup_profile_generation_and_contract_validation(tmp_path):
    output_file = tmp_path / "setup-profile.json"
    profile = generate_setup_profile(
        platform="google",
        client_name="Test Corp",
        account_id="google-123",
        objective="conversions",
        conversion_definition="purchase",
        output_path=output_file,
    )
    assert output_file.exists()
    assert profile["business"]["name"] == "Test Corp"
    assert profile["platforms"] == ["google"]
    validate_workflow_contract("setup-profile", profile)


def test_setup_profile_rejects_unsupported_platform():
    with pytest.raises(SetupError, match="unsupported platform"):
        generate_setup_profile(platform="myspace", client_name="Invalid")


def test_run_audit_google_native_export_end_to_end(tmp_path):
    run_dir = tmp_path / "runs"
    result = run_audit(
        platform="google",
        input_path=GOOGLE_FIXTURE,
        report_format="markdown",
        output_dir=run_dir,
        client_name="Test Client",
        registry_root=REPO_ROOT,
    )

    assert result["status"] == "completed"
    assert result["platform"] == "google"
    assert result["scoring_status"] == "insufficient_evidence"
    assert result["health_score"] is None
    assert result["completeness"] == "partial"
    assert result["findings_count"] > 0

    bundle_path = Path(result["bundle_path"])
    report_path = Path(result["report_path"])

    assert bundle_path.exists()
    assert report_path.exists()

    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    validate_contract("report-bundle", bundle)

    assert any(
        ev.get("source_id", "").startswith("sha256:")
        for f in bundle["findings"]
        for ev in f.get("evidence", [])
    )

    report_content = report_path.read_text(encoding="utf-8")
    assert "Claude Ads Audit Report" in report_content
    assert "Google" in report_content
    assert "Insufficient evidence" in report_content


def test_run_audit_rejects_missing_file(tmp_path):
    with pytest.raises(AuditError, match="does not exist"):
        run_audit(
            platform="google",
            input_path=tmp_path / "missing.csv",
        )


def test_cli_doctor_json_output(capsys):
    ret = main(["doctor", "--root", str(REPO_ROOT), "--format", "json"])
    assert ret == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert "registry" in out


def test_cli_doctor_text_output(capsys):
    ret = main(["doctor", "--root", str(REPO_ROOT), "--format", "text"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "Claude Ads Core" in out
    assert "Status: OK" in out


def test_cli_setup_command(tmp_path, capsys):
    out_path = tmp_path / "setup.json"
    ret = main(
        [
            "setup",
            "--platform",
            "google",
            "--client",
            "Acme Corp",
            "--account-id",
            "acme-456",
            "--output",
            str(out_path),
        ]
    )
    assert ret == 0
    assert out_path.exists()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["business"]["name"] == "Acme Corp"


def test_cli_audit_command(tmp_path, capsys):
    ret = main(
        [
            "audit",
            "--platform",
            "google",
            "--input",
            str(GOOGLE_FIXTURE),
            "--root",
            str(tmp_path / "runs"),
            "--registry-root",
            str(REPO_ROOT),
        ]
    )
    assert ret == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "completed"
    assert out["platform"] == "google"
    assert Path(out["bundle_path"]).exists()
    assert Path(out["report_path"]).exists()
