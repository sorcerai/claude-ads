"""Command-line interface for deterministic Claude Ads core operations."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .adapters import (
    AdapterError,
    GenericCSVExportAdapter,
    NativeCSVExportAdapter,
    NativeJSONExportAdapter,
)
from .audit import AuditError, run_audit
from .competitor_fanout import SOURCES, plan_slices
from .contracts import CONTRACT_NAMES, ContractError, load_contract, validate_contract
from .control_registry import RegistryError, load_control_registry
from .doctor import DoctorError, run_doctor
from .reporting import ReportRenderError, write_report_bundle
from .product_status import ProductStatusError, evaluate_product_status
from .setup import SetupError, generate_setup_profile
from .workflow_contracts import WorkflowContractError, validate_workflow_contract



def _default_report_root(platform_name: str | None = None) -> str:
    platform_name = os.name if platform_name is None else platform_name
    if platform_name == "nt":
        try:
            return str(Path.home() / ".claude-ads" / "runs")
        except (OSError, RuntimeError) as exc:
            raise ReportRenderError(f"report root home normalization failed: {exc}") from exc
    return ".claude-ads/runs"

def _read_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load {path}: {exc}") from exc


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-ads-core")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate a versioned JSON contract")
    validate.add_argument("contract", choices=CONTRACT_NAMES)
    validate.add_argument("path")


    status = commands.add_parser("status", help="show repository status, or a report bundle when path is supplied")
    status.add_argument("path", nargs="?")
    status.add_argument("--root", default=".")
    status.add_argument("--registry-root", default=None, help="repository root containing the control registry")
    status.add_argument("--as-of")
    status.add_argument("--release-gate")

    next_command = commands.add_parser("next", help="show exactly one repository-artifact blocker")
    next_command.add_argument("--root", default=".")
    next_command.add_argument("--as-of", required=True)
    next_command.add_argument("--release-gate")

    render = commands.add_parser(
        "render",
        aliases=["report"],
        help="render a validated report bundle beneath a safe output root",
    )
    render.add_argument("path", help="ReportBundle JSON input")
    render.add_argument("--format", choices=("markdown", "html", "pdf"), default="markdown")
    render.add_argument("--root", default=None, help="safe root for report artifacts")
    render.add_argument("--registry-root", default=None, help="repository root containing the control registry")
    render.add_argument("--output", help="relative output path; defaults to <run-id>/report.<extension>")

    fanout = commands.add_parser(
        "plan-fanout",
        help="plan competitor research slices as orchestration-task packets",
    )
    fanout.add_argument("--run-id", required=True)
    fanout.add_argument("--competitors", required=True, help="comma-separated names")
    fanout.add_argument("--countries", required=True, help="comma-separated ISO codes")
    fanout.add_argument(
        "--sources",
        default=",".join(sorted(SOURCES)),
        help="comma-separated source ids; defaults to all",
    )
    fanout.add_argument("--ad-type", default="ALL")
    fanout.add_argument("--created-at", required=True, help="ISO 8601 timestamp")

    ingest = commands.add_parser("ingest-export", help="normalize a CSV export")
    ingest.add_argument("--platform", required=True)
    ingest.add_argument("--format", choices=("generic", "native"), default="generic", help="export projection format")
    ingest.add_argument("--native", action="store_true", help="use platform native CSV export profile")
    ingest.add_argument("--context", action="append", default=[], help="context key=value pairs for native adapter")
    ingest.add_argument("path")

    doctor = commands.add_parser("doctor", help="inspect environment, packaging, and registry readiness")
    doctor.add_argument("--root", default=".", help="repository root")
    doctor.add_argument("--registry-root", default=None, help="control registry root")
    doctor.add_argument("--format", choices=("json", "text"), default="json", help="output format")

    setup = commands.add_parser("setup", help="generate a validated setup profile and data lifecycle")
    setup.add_argument("--platform", default="google", help="target advertising platform")
    setup.add_argument("--client", default="Default Client", help="client business name")
    setup.add_argument("--business-model", default="ecommerce", help="business model")
    setup.add_argument("--geography", default="US", help="target geography (comma-separated for multiples)")
    setup.add_argument("--account-id", default="demo-account", help="platform account ID")
    setup.add_argument("--objective", default="conversions", help="primary campaign objective")
    setup.add_argument("--conversion-definition", default="purchase", help="conversion taxonomy definition")
    setup.add_argument("--privacy-class", default="internal", choices=("public", "internal", "confidential", "restricted"))
    setup.add_argument("--export-path", default=None, help="path to verified export data source")
    setup.add_argument("--output", default=None, help="output path for setup-profile.json")

    audit = commands.add_parser("audit", help="execute deterministic export-to-audit reference journey")
    audit.add_argument("--platform", default="google", help="advertising platform (default: google)")
    audit.add_argument("--input", required=True, help="path to account export CSV")
    audit.add_argument("--format", choices=("markdown", "html", "pdf"), default="markdown", help="report format")
    audit.add_argument("--export-format", choices=("auto", "native", "generic"), default="auto", help="export projection format")
    audit.add_argument("--context", action="append", default=[], help="context key=value pairs for native adapter")
    audit.add_argument("--run-id", default=None, help="deterministic run ID")
    audit.add_argument("--root", default=None, help="safe report output root directory")
    audit.add_argument("--registry-root", default=None, help="control registry root")
    audit.add_argument("--privacy-class", default="internal", choices=("public", "internal", "confidential", "restricted"))
    audit.add_argument("--client", default=None, help="client business name")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            load_contract(args.contract, args.path)
            _emit({"contract": args.contract, "path": args.path, "status": "valid"})
        elif args.command == "status":
            if args.path is not None:
                bundle = _read_json(args.path)
                validate_contract("report-bundle", bundle)
                registry = load_control_registry(args.registry_root)
                scoring = registry.validate_report_scoring(bundle).to_dict()
                manifest = bundle["run_manifest"]
                _emit(
                    {
                        "run_id": manifest["run_id"],
                        "completeness": manifest["completeness"],
                        "health_score": scoring["health_score"],
                        "evidence_coverage": scoring["evidence_coverage"],
                        "status": scoring["status"],
                    }
                )
            else:
                if not args.as_of:
                    raise ProductStatusError("repository status requires --as-of YYYY-MM-DD")
                try:
                    as_of = date.fromisoformat(args.as_of)
                except ValueError as exc:
                    raise ProductStatusError("--as-of must be an ISO 8601 date") from exc
                _emit(
                    evaluate_product_status(
                        args.root, as_of=as_of, release_gate_path=args.release_gate
                    )
                )
        elif args.command == "next":
            try:
                as_of = date.fromisoformat(args.as_of)
            except ValueError as exc:
                raise ProductStatusError("--as-of must be an ISO 8601 date") from exc
            status_payload = evaluate_product_status(
                args.root, as_of=as_of, release_gate_path=args.release_gate
            )
            _emit(
                {
                    "schema_version": status_payload["schema_version"],
                    "as_of": status_payload["as_of"],
                    "selection_policy": status_payload["selection_policy"],
                    "next_blocker": status_payload["next_blocker"],
                }
            )
        elif args.command in {"render", "report"}:
            bundle = load_contract("report-bundle", args.path)
            registry = load_control_registry(args.registry_root)
            extension = {"markdown": "md", "html": "html", "pdf": "pdf"}[args.format]
            destination = args.output if args.output is not None else f"{bundle['run_manifest']['run_id']}/report.{extension}"
            root = args.root if args.root is not None else _default_report_root()
            output_path = write_report_bundle(
                bundle, args.format, root, destination, registry=registry
            )
            _emit(
                {
                    "format": args.format,
                    "path": str(output_path),
                    "run_id": bundle["run_manifest"]["run_id"],
                    "status": "rendered",
                }
            )
        elif args.command == "plan-fanout":
            def _split(value: str) -> list[str]:
                return [item.strip() for item in value.split(",") if item.strip()]

            tasks = plan_slices(
                run_id=args.run_id,
                competitors=_split(args.competitors),
                countries=_split(args.countries),
                sources=_split(args.sources),
                created_at=args.created_at,
                ad_type=args.ad_type,
            )
            for task in tasks:
                validate_workflow_contract("orchestration-task", task)
            _emit({"run_id": args.run_id, "slices": len(tasks), "tasks": tasks})
        elif args.command == "ingest-export":
            use_native = args.native or args.format == "native"
            if use_native:
                context_dict = dict(item.split("=", 1) for item in args.context if "=" in item)
                if Path(args.path).suffix.lower() == ".json":
                    adapter = NativeJSONExportAdapter(args.platform, context=context_dict)
                else:
                    adapter = NativeCSVExportAdapter(args.platform, context=context_dict)
                _emit(adapter.read_snapshot(args.path))
            else:
                _emit(GenericCSVExportAdapter(args.platform).read_snapshot(args.path))
        elif args.command == "doctor":
            result = run_doctor(root=args.root, registry_root=args.registry_root)
            if args.format == "text":
                print(f"Claude Ads Core v{result['version']} Doctor")
                print(f"Status: {result['status'].upper()}")
                print(f"Python: {result['python']['version']} (supported: {result['python']['supported']})")
                print(f"Registry: {result['registry']['status']} ({result['registry']['entries_count']} entries, {result['registry']['profiles_count']} profiles)")
                print(f"Adapters: {result['adapters']['status']} ({len(result['adapters']['profiles'])} profiles)")
                print(f"Filesystem: report_root={result['filesystem']['report_root']} (writable: {result['filesystem']['writable']})")
                if result["issues"]:
                    print("Issues:")
                    for issue in result["issues"]:
                        print(f"  - {issue}")
            else:
                _emit(result)
            if result["status"] == "error":
                return 2
        elif args.command == "setup":
            geographies = [g.strip() for g in args.geography.split(",") if g.strip()]
            profile = generate_setup_profile(
                platform=args.platform,
                client_name=args.client,
                business_model=args.business_model,
                geographies=geographies,
                account_id=args.account_id,
                objective=args.objective,
                conversion_definition=args.conversion_definition,
                privacy_class=args.privacy_class,
                data_source_path=args.export_path,
                output_path=args.output,
            )
            if args.output:
                _emit({"status": "created", "path": args.output, "run_id": profile["run_id"]})
            else:
                _emit(profile)
        elif args.command == "audit":
            context_dict = dict(item.split("=", 1) for item in args.context if "=" in item)
            result = run_audit(
                platform=args.platform,
                input_path=args.input,
                report_format=args.format,
                export_format=args.export_format,
                context=context_dict if context_dict else None,
                output_dir=args.root,
                run_id=args.run_id,
                privacy_class=args.privacy_class,
                registry_root=args.registry_root,
                client_name=args.client,
            )
            _emit(result)
    except (
        AdapterError,
        AuditError,
        ContractError,
        DoctorError,
        ProductStatusError,
        RegistryError,
        ReportRenderError,
        SetupError,
        WorkflowContractError,
        ValueError,
    ) as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
