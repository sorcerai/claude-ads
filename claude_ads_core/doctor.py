"""System and environment diagnostics for deterministic Claude Ads operations."""

from __future__ import annotations

import importlib.util
import sys
from os import PathLike
from pathlib import Path
from typing import Any

from . import __version__
from .adapters import mappings_v1
from .contracts import PLATFORMS
from .control_registry import RegistryError, load_control_registry


class DoctorError(ValueError):
    """Raised when a required doctor verification check fails."""


def _check_python_version() -> dict[str, Any]:
    current_major = sys.version_info.major
    current_minor = sys.version_info.minor
    # Repository targets Python >= 3.11, < 3.13
    supported = (current_major == 3 and 11 <= current_minor < 13)
    return {
        "version": f"{current_major}.{current_minor}.{sys.version_info.micro}",
        "supported": supported,
        "required_range": ">=3.11,<3.13",
    }


def _check_optional_dependencies() -> dict[str, bool]:
    dependencies = {
        "weasyprint": "weasyprint",
        "playwright": "playwright",
        "jsonschema": "jsonschema",
    }
    status: dict[str, bool] = {}
    for name, module in dependencies.items():
        status[name] = importlib.util.find_spec(module) is not None
    return status


def _check_registry(registry_root: str | PathLike[str] | None = None) -> dict[str, Any]:
    try:
        registry = load_control_registry(registry_root)
    except RegistryError as exc:
        return {
            "status": "error",
            "error": str(exc),
            "entries_count": 0,
            "profiles_count": 0,
        }
    entries_count = len(registry.entries)
    profiles_count = len(registry.profiles)
    profiles_status = {p.platform: p.status for p in registry.profiles}
    return {
        "status": "ok",
        "entries_count": entries_count,
        "profiles_count": profiles_count,
        "platforms_covered": sorted(PLATFORMS),
        "profiles_status": profiles_status,
    }


def _check_adapter_profiles() -> dict[str, Any]:
    available: dict[str, str] = {}
    for platform in sorted(PLATFORMS):
        try:
            profile = mappings_v1.get_native_profile(platform)
            available[platform] = profile.status
        except ValueError:
            available[platform] = "missing"
    return {
        "status": "ok",
        "profiles": available,
    }


def _check_filesystem(report_root: Path) -> dict[str, Any]:
    writable = False
    try:
        report_root.mkdir(parents=True, exist_ok=True)
        test_file = report_root / ".doctor_write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        writable = True
    except (OSError, PermissionError):
        writable = False
    return {
        "report_root": str(report_root),
        "writable": writable,
    }


def run_doctor(
    root: str | PathLike[str] = ".",
    registry_root: str | PathLike[str] | None = None,
) -> dict[str, Any]:
    """Run all diagnostic checks and return structured status."""
    issues: list[str] = []
    py_info = _check_python_version()
    if not py_info["supported"]:
        issues.append(
            f"Python {py_info['version']} is outside supported range {py_info['required_range']}"
        )

    reg_info = _check_registry(registry_root)
    if reg_info["status"] != "ok":
        issues.append(f"Control registry error: {reg_info.get('error')}")

    adapter_info = _check_adapter_profiles()
    missing_adapters = [
        platform for platform, status in adapter_info["profiles"].items() if status == "missing"
    ]
    if missing_adapters:
        issues.append(f"Missing native adapter profiles for: {', '.join(missing_adapters)}")

    repo_path = Path(root)
    report_root = repo_path / ".claude-ads" / "runs"
    fs_info = _check_filesystem(report_root)
    if not fs_info["writable"]:
        issues.append(f"Report root {report_root} is not writable")

    opt_deps = _check_optional_dependencies()

    overall_status = "error" if issues else "ok"

    return {
        "status": overall_status,
        "version": __version__,
        "python": py_info,
        "registry": reg_info,
        "adapters": adapter_info,
        "optional_dependencies": opt_deps,
        "filesystem": fs_info,
        "issues": issues,
    }
