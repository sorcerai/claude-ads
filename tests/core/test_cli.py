from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import claude_ads_core
from claude_ads_core.cli import _default_report_root, build_parser, main
from claude_ads_core.control_registry import load_control_registry
from claude_ads_core.reporting import ReportRenderError, _validate_windows_tree
from tests.core.test_contracts import account_snapshot, report_bundle


REPO_ROOT = Path(__file__).resolve().parents[2]


def write_json(tmp_path, name: str, payload) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_default_report_root_windows_is_beneath_home():
    root = Path(_default_report_root("nt"))
    assert root == Path.home() / ".claude-ads" / "runs"
    assert root.is_relative_to(Path.home())


def test_default_report_root_posix_remains_repo_local():
    assert _default_report_root("posix") == ".claude-ads/runs"


def test_build_parser_does_not_resolve_home_for_render_default(monkeypatch):
    def fail_home(cls):
        raise OSError("home unavailable")

    monkeypatch.setattr(Path, "home", classmethod(fail_home))
    args = build_parser().parse_args(["render", "bundle.json"])
    assert args.root is None
    assert args.registry_root is None


def test_status_command_uses_packaged_registry_outside_checkout(tmp_path, monkeypatch, capsys):
    bundle = report_bundle()
    bundle["control_definitions"] = []
    bundle["findings"] = []
    bundle["scoring"] = load_control_registry(REPO_ROOT).score_platform("google", []).to_dict()
    bundle["run_manifest"]["completeness"] = "partial"
    bundle_path = write_json(tmp_path, "report.json", bundle)
    monkeypatch.chdir(tmp_path)

    assert main(["status", bundle_path]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "insufficient_evidence"


def test_status_command_preserves_explicit_registry_root(tmp_path, capsys):
    bundle = report_bundle()
    bundle["control_definitions"] = []
    bundle["findings"] = []
    bundle["scoring"] = load_control_registry(REPO_ROOT).score_platform("google", []).to_dict()
    bundle["run_manifest"]["completeness"] = "partial"
    bundle_path = write_json(tmp_path, "report.json", bundle)

    assert main(["status", bundle_path, "--registry-root", str(REPO_ROOT)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "insufficient_evidence"

@pytest.mark.parametrize("error", [OSError("home unavailable"), RuntimeError("home unavailable")])
def test_windows_default_report_root_normalizes_home_errors(monkeypatch, error):
    def fail_home(cls):
        raise error

    monkeypatch.setattr(Path, "home", classmethod(fail_home))
    with pytest.raises(ReportRenderError, match="home"):
        _default_report_root("nt")


def test_render_home_normalization_failure_returns_json_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "claude_ads_core.cli.load_contract",
        lambda *args: {"run_manifest": {"run_id": "run"}},
    )
    monkeypatch.setattr("claude_ads_core.cli.load_control_registry", lambda *args: object())
    path = str(tmp_path / "report.json")

    def fail_home(cls):
        raise RuntimeError("home unavailable")

    monkeypatch.setattr(Path, "home", classmethod(fail_home))
    monkeypatch.setattr("claude_ads_core.cli.os.name", "nt")
    assert main(["render", path, "--registry-root", str(REPO_ROOT)]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["status"] == "invalid"
    assert "home unavailable" in error["error"]


def test_windows_tree_home_normalization_runtime_error_is_typed(tmp_path, monkeypatch):
    def fail_home(cls):
        raise RuntimeError("home unavailable")

    monkeypatch.setattr(Path, "home", classmethod(fail_home))
    with pytest.raises(ReportRenderError, match="home"):
        _validate_windows_tree(tmp_path / "reports", Path("report.md"))


def test_validate_command_emits_machine_readable_success(tmp_path, capsys):
    path = write_json(tmp_path, "snapshot.json", account_snapshot())
    assert main(["validate", "account-snapshot", path]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"contract": "account-snapshot", "path": path, "status": "valid"}


def test_validate_command_returns_two_and_json_error(tmp_path, capsys):
    path = write_json(tmp_path, "snapshot.json", {"schema_version": "1.0.0"})
    assert main(["validate", "account-snapshot", path]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["status"] == "invalid"
    assert "schema_version" in error["error"]



def test_status_command_rejects_forged_bundle_score(tmp_path, capsys):
    bundle = report_bundle()
    bundle["control_definitions"] = []
    bundle["findings"] = []
    bundle["run_manifest"]["completeness"] = "partial"
    path = write_json(tmp_path, "report.json", bundle)
    assert main(["status", path, "--root", str(REPO_ROOT)]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["status"] == "invalid"
    assert "report scoring does not match profile" in error["error"]

def test_status_command_accepts_exact_disabled_profile_no_score(tmp_path, capsys):
    bundle = report_bundle()
    bundle["control_definitions"] = []
    bundle["findings"] = []
    bundle["scoring"] = load_control_registry(REPO_ROOT).score_platform("google", []).to_dict()
    bundle["run_manifest"]["completeness"] = "partial"
    path = write_json(tmp_path, "report.json", bundle)
    assert main(["status", path, "--root", str(REPO_ROOT)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "run_id": "run-20260711-001",
        "completeness": "partial",
        "health_score": None,
        "evidence_coverage": 0.0,
        "status": "insufficient_evidence",
    }

@pytest.mark.parametrize(
    ("command", "arguments"),
    (
        ("score", ("--controls", "controls.json", "--findings", "findings.json", "--weights", "weights.json")),
        ("portfolio", ("accounts.json",)),
    ),
)
def test_removed_scoring_commands_are_rejected(command, arguments):
    with pytest.raises(SystemExit) as exc_info:
        main([command, *arguments])
    assert exc_info.value.code == 2

@pytest.mark.parametrize(
    "name",
    ("score_account", "score_portfolio", "ScoreResult", "PortfolioResult"),
)
def test_scoring_api_is_not_reexported(name):
    assert name not in claude_ads_core.__all__
    assert not hasattr(claude_ads_core, name)

def test_ingest_export_command_emits_normalized_snapshot(capsys):
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "exports" / "google.csv"
    assert main(["ingest-export", "--platform", "google", str(fixture)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == "2.0.0"
    assert output["account"]["platform"] == "google"
    assert output["measurement_context"] == {
        "timezone": None,
        "currency": "USD",
        "profile_id": "google-generic-csv-v1",
        "source_format": "portable-csv",
        "source_ids": [f"sha256:{hashlib.sha256(fixture.read_bytes()).hexdigest()}"],
        "report_grain": ["date", "campaign_id", "creative_id"],
        "conversion_definition": None,
        "conversion_actions": ["purchase"],
        "attribution_model": None,
        "click_attribution_window": None,
        "view_attribution_window": None,
        "counting_behavior": None,
        "as_of": "2026-06-15",
        "data_finalization": "unknown",
        "modeled_data_treatment": "unknown",
        "missing_fields": [
            "attribution_model",
            "click_attribution_window",
            "conversion_definition",
            "counting_behavior",
            "data_finalization",
            "modeled_data_treatment",
            "timezone",
            "view_attribution_window",
        ],
        "unsupported_fields": [],
    }
    assert output["spend"] == 42.5

def test_render_command_rejects_explicit_empty_output_before_writing(tmp_path, capsys):
    bundle = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "reports" / "sanitized-report-bundle.json").read_text(
            encoding="utf-8"
        )
    )
    bundle["control_definitions"] = []
    bundle["findings"] = []
    bundle["scoring"] = load_control_registry(REPO_ROOT).score_platform("google", []).to_dict()
    bundle_path = write_json(tmp_path, "report.json", bundle)
    root = tmp_path / "runs"
    assert (
        main(
            [
                "render",
                bundle_path,
                "--root",
                str(root),
                "--registry-root",
                str(REPO_ROOT),
                "--output",
                "",
            ]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().err)
    assert error["status"] == "invalid"
    assert "relative path" in error["error"]
    assert list(root.rglob("*")) == []


def test_status_command_rejects_explicit_empty_bundle_path(tmp_path, capsys):
    assert (
        main(
            [
                "status",
                "",
                "--root",
                str(tmp_path),
                "--as-of",
                "2026-08-30",
            ]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().err)
    assert error["status"] == "invalid"
    assert error["error"].startswith("cannot load :")
