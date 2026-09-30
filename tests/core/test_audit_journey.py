from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import claude_ads_core.audit as audit
from claude_ads_core.reporting import ReportRenderError

from claude_ads_core.audit import AuditError, run_audit
from claude_ads_core.cli import main
from claude_ads_core.contracts import validate_contract
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


def test_setup_profile_generation_and_contract_validation():
    profile = generate_setup_profile(
        platform="google",
        client_name="Test Corp",
        account_id="google-123",
        objective="conversions",
        conversion_definition="purchase",
    )
    assert profile["business"]["name"] == "Test Corp"
    assert profile["platforms"] == ["google"]
    assert profile["schema_version"] == "2.0.0"
    lifecycle = profile["data_lifecycle"]
    assert lifecycle["schema_version"] == "2.0.0"
    assert lifecycle["encryption"] == {
        "at_rest": "unknown", "in_transit": "unknown", "evidence_refs": []
    }
    assert lifecycle["deletion"]["status"] == "pending"
    assert lifecycle["deletion"]["scheduler_receipt_locator"] is None
    assert lifecycle["retention"]["mode"] == "unassigned"
    assert lifecycle["retention"]["delete_after"] is None
    validate_workflow_contract("setup-profile", profile)


def test_setup_profile_honors_explicit_retention_days():
    profile = generate_setup_profile(retention_days=7)
    retention = profile["data_lifecycle"]["retention"]
    assert retention["mode"] == "operator-defined"
    assert retention["delete_after"] is not None
    validate_workflow_contract("setup-profile", profile)


def test_nonpublic_setup_rejects_persistence_without_touching_destination(tmp_path):
    existing = tmp_path / "setup.json"
    existing.write_bytes(b"prior-private-content")
    with pytest.raises(SetupError, match="non-public|evidence"):
        generate_setup_profile(output_path=existing)
    assert existing.read_bytes() == b"prior-private-content"

    absent = tmp_path / "new-parent" / "setup.json"
    with pytest.raises(SetupError, match="non-public|evidence"):
        generate_setup_profile(output_path=absent)
    assert not absent.parent.exists()


def test_public_setup_atomically_replaces_permissive_file(tmp_path):
    existing = tmp_path / "setup.json"
    existing.write_bytes(b"prior-private-content")
    if os.name == "posix":
        existing.chmod(0o644)
    profile = generate_setup_profile(privacy_class="public", output_path=existing)
    assert json.loads(existing.read_text(encoding="utf-8")) == profile
    validate_workflow_contract("setup-profile", profile)
    if os.name == "posix":
        assert existing.stat().st_mode & 0o077 == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory privacy boundary")
def test_public_setup_rejects_permissive_parent_without_replacing_file(tmp_path):
    tmp_path.chmod(0o755)
    existing = tmp_path / "setup.json"
    existing.write_bytes(b"prior-private-content")
    with pytest.raises(SetupError, match="persistence"):
        generate_setup_profile(privacy_class="public", output_path=existing)
    assert existing.read_bytes() == b"prior-private-content"


def test_setup_fsync_failure_preserves_previous_bytes(tmp_path, monkeypatch):
    output = tmp_path / "setup.json"
    output.write_bytes(b"prior-private-content")

    def reject_fsync(_fd):
        raise OSError("fixture blocked fsync")

    monkeypatch.setattr(os, "fsync", reject_fsync)
    with pytest.raises(SetupError, match="persistence") as failure:
        generate_setup_profile(privacy_class="public", output_path=output)
    assert "fixture blocked fsync" not in str(failure.value)
    assert output.read_bytes() == b"prior-private-content"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink boundary")
def test_setup_refuses_leaf_and_parent_symlinks(tmp_path):
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"untouched")
    leaf = tmp_path / "setup.json"
    leaf.symlink_to(sentinel)
    with pytest.raises(SetupError, match="persistence"):
        generate_setup_profile(privacy_class="public", output_path=leaf)
    assert leaf.is_symlink()
    assert sentinel.read_bytes() == b"untouched"

    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "alias"
    linked_parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SetupError, match="persistence"):
        generate_setup_profile(privacy_class="public", output_path=linked_parent / "setup.json")
    assert not (outside / "setup.json").exists()

    with pytest.raises(SetupError, match="persistence"):
        generate_setup_profile(
            privacy_class="public", output_path=linked_parent / "new" / "setup.json"
        )
    assert not (outside / "new").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX atomic replacement boundary")
def test_setup_postreplacement_interrupt_preserves_complete_result(tmp_path, monkeypatch):
    output = tmp_path / "setup.json"
    output.write_bytes(b"prior-private-content")
    original_replace = os.replace

    def replacing_then_interrupt(source, destination, **kwargs):
        original_replace(source, destination, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(os, "replace", replacing_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        generate_setup_profile(privacy_class="public", output_path=output)
    validate_workflow_contract("setup-profile", json.loads(output.read_text(encoding="utf-8")))


@pytest.mark.skipif(os.name != "posix", reason="POSIX replacement verification")
def test_setup_reports_uncertain_postreplacement_verification_without_private_details(
    tmp_path, monkeypatch
):
    output = tmp_path / "setup.json"
    output.write_bytes(b"prior-private-content")
    original_replace = os.replace
    original_fsync = os.fsync
    replaced = False

    def replacement_then_verify(source, destination, **kwargs):
        nonlocal replaced
        original_replace(source, destination, **kwargs)
        replaced = True

    def fail_after_replacement(fd):
        if replaced:
            raise OSError("fixture-private-verification-detail")
        return original_fsync(fd)

    monkeypatch.setattr(os, "replace", replacement_then_verify)
    monkeypatch.setattr(os, "fsync", fail_after_replacement)
    with pytest.raises(SetupError, match="uncertain|may have occurred") as failure:
        generate_setup_profile(privacy_class="public", output_path=output)
    assert "fixture-private-verification-detail" not in str(failure.value)
    validate_workflow_contract("setup-profile", json.loads(output.read_text(encoding="utf-8")))


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
        privacy_class="public",
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
    assert bundle["schema_version"] == "3.0.0"
    manifest = bundle["run_manifest"]
    assert manifest["schema_version"] == "2.0.0"
    assert manifest["data_lifecycle"]["schema_version"] == "2.0.0"
    assert manifest["data_lifecycle"]["encryption"]["at_rest"] == "unknown"
    assert manifest["data_lifecycle"]["deletion"]["status"] == "pending"
    assert manifest["data_lifecycle"]["retention"]["mode"] == "unassigned"
    assert manifest["data_lifecycle"]["retention"]["delete_after"] is None

    assert any(
        ev.get("source_id", "").startswith("sha256:")
        for f in bundle["findings"]
        for ev in f.get("evidence", [])
    )

    report_content = report_path.read_text(encoding="utf-8")
    assert "Claude Ads Audit Report" in report_content
    assert "Google" in report_content
    assert "Insufficient evidence" in report_content


@pytest.mark.skipif(os.name != "posix", reason="POSIX output-root symlink boundary")
def test_public_audit_does_not_chmod_or_write_through_symlink_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AuditError):
        run_audit(
            platform="google",
            input_path=GOOGLE_FIXTURE,
            output_dir=linked_root,
            run_id="audit-symlink-boundary",
            privacy_class="public",
            registry_root=REPO_ROOT,
        )
    assert outside.stat().st_mode & 0o777 == 0o755
    assert not (outside / "audit-symlink-boundary").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory privacy boundary")
def test_public_audit_rejects_permissive_root_without_creating_run(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    root.chmod(0o755)
    with pytest.raises(AuditError, match="bundle persistence"):
        run_audit(
            platform="google",
            input_path=GOOGLE_FIXTURE,
            output_dir=root,
            run_id="audit-permissive-root",
            privacy_class="public",
            registry_root=REPO_ROOT,
        )
    assert not (root / "audit-permissive-root").exists()


def test_nonpublic_audit_refuses_input_and_output_side_effects(tmp_path, monkeypatch):
    input_file = tmp_path / "not-opened.csv"
    output_dir = tmp_path / "not-created"
    original_exists = Path.exists

    def reject_input_check(path):
        if path == input_file:
            raise AssertionError("non-public audit touched its input")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", reject_input_check)
    with pytest.raises(AuditError, match="non-public|evidence"):
        run_audit(input_path=input_file, output_dir=output_dir)
    assert not original_exists(output_dir)


def test_run_audit_rejects_missing_file(tmp_path):
    with pytest.raises(AuditError, match="does not exist"):
        run_audit(
            platform="google",
            input_path=tmp_path / "missing.csv",
            privacy_class="public",
        )

@pytest.mark.parametrize("failure", ["missing", "unreadable", "output", "registry"])
def test_cli_audit_failure_does_not_echo_private_input_or_writer_detail(
    capsys, monkeypatch, tmp_path, failure
):
    private_detail = "client-secret S-1-5-21-private sk_live_private"
    input_path = (
        tmp_path / private_detail / "missing.csv"
        if failure == "missing"
        else GOOGLE_FIXTURE
    )
    if failure == "unreadable":
        original_read = Path.read_bytes

        def fail_read(path):
            if path == GOOGLE_FIXTURE:
                raise OSError(private_detail)
            return original_read(path)

        monkeypatch.setattr(Path, "read_bytes", fail_read)
    elif failure == "output":
        def fail_writer(*_args):
            raise ReportRenderError(private_detail)

        monkeypatch.setattr(audit, "atomic_write_report", fail_writer)

    result = main(
        [
            "audit", "--input", str(input_path),
            "--privacy-class", "public",
            "--root", str(tmp_path / "runs"),
            "--registry-root", str(tmp_path / private_detail if failure == "registry" else REPO_ROOT),
        ]
    )
    error = json.loads(capsys.readouterr().err)
    assert result == 2
    assert error["status"] == "invalid"
    assert private_detail not in error["error"]
    assert error["error"].startswith(
        {
            "missing": "input file",
            "unreadable": "cannot read input file",
            "output": "bundle persistence failed",
            "registry": "control registry loading failed",
        }[failure]
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
            "--privacy-class",
            "public",
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
            "--privacy-class",
            "public",
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
