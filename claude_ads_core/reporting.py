"""Deterministic, privacy-aware renderers for validated report bundles.

JSON remains the canonical artifact.  This module produces human-readable
views after validating and recomputing scores through the supplied control
registry, without loading remote assets or embedding the source bundle in the output.
"""

from __future__ import annotations

import html
import importlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping


from .contracts import ContractError, validate_contract
from .control_registry import ControlRegistry, RegistryError

_POSIX_CAPABILITY_FUNCS = {
    name: getattr(os, name, None) for name in ("open", "mkdir", "stat", "rename", "unlink")
}


class ReportRenderError(ValueError):
    """Raised when a report cannot be rendered or written safely."""


class PDFDependencyError(ReportRenderError):
    """Raised when the optional PDF renderer is unavailable."""


_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_BEARER_RE = re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]+")
_HEADER_SECRET_RE = re.compile(
    r"(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie)[ \t]*:[ \t]*)[^\r\n]*"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|api[_-]?key|client[_-]?secret|"
    r"password|passwd|authorization|cookie)\s*([:=])\s*(?!\[REDACTED\])([^\s&;,]+)"
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(^|[_-])(access[_-]?token|refresh[_-]?token|api[_-]?key|client[_-]?secret|"
    r"password|passwd|authorization|cookie|set[_-]?cookie|email|phone)([_-]|$)"
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_STATUS_LABELS = {
    "normal": "Normal",
    "provisional": "Provisional",
    "insufficient_evidence": "Insufficient evidence",
}
_COMPLETENESS_LABELS = {"complete": "Complete", "partial": "Partial", "failed": "Failed"}


def _redact_text(value: str) -> str:
    value = _CONTROL_CHARS_RE.sub("", value).replace("\r\n", "\n").replace("\r", "\n")
    value = _HEADER_SECRET_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    value = _BEARER_RE.sub("Bearer [REDACTED]", value)
    value = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)
    return _EMAIL_RE.sub("[REDACTED EMAIL]", value)


def _redact_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): _redact_value(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    return _redact_text(str(value)).replace("\n", " ").strip()


def _md(value: Any) -> str:
    """Make untrusted text inert in a single Markdown paragraph."""

    text = _text(value)
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|>-])", r"\\\1", text)


def _html(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _canonical_json(value: Any) -> str:
    return json.dumps(_redact_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validated_bundle(bundle: Mapping[str, Any], registry: ControlRegistry) -> Mapping[str, Any]:
    try:
        validate_contract("report-bundle", bundle)
        registry.validate_report_scoring(bundle)
    except (ContractError, RegistryError) as exc:
        raise ReportRenderError(f"invalid report bundle: {exc}") from exc
    for field in ("contradictions", "actions"):
        if field in bundle and not isinstance(bundle[field], list):
            raise ReportRenderError(f"$.{field} must be an array when present")
    return bundle


def _score_text(score: Any) -> str:
    if score is None:
        return "Not scored"
    return f"{float(score):.2f} / 100"


def _coverage_text(coverage: Any) -> str:
    return f"{float(coverage):.2f}%"


def _controls(bundle: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(control["control_id"]): control for control in bundle["control_definitions"]}


def _findings(bundle: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return sorted(bundle["findings"], key=lambda finding: str(finding["control_id"]))


def _categories(bundle: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return sorted(bundle["scoring"]["categories"], key=lambda category: str(category.get("category", "")))


def _extension_item_text(item: Any) -> str:
    if isinstance(item, str):
        return _text(item)
    if isinstance(item, Mapping):
        preferred = ("summary", "title", "action", "claim", "description", "recommendation")
        headline = next((_text(item[key]) for key in preferred if item.get(key)), "")
        details = [
            f"{str(key).replace('_', ' ').title()}: {_text(value)}"
            for key, value in sorted(item.items(), key=lambda pair: str(pair[0]))
            if key not in preferred and value not in (None, "", [], {})
        ]
        if headline and details:
            return f"{headline} ({'; '.join(details)})"
        return headline or "; ".join(details) or _canonical_json(item)
    return _text(item)


def _actions(bundle: Mapping[str, Any]) -> list[Any]:
    explicit = list(bundle.get("actions", []))
    if explicit:
        return explicit
    return [
        {
            "action": finding["recommendation"],
            "confidence": finding["confidence"],
            "control_id": finding["control_id"],
        }
        for finding in _findings(bundle)
        if finding["status"] in {"fail", "unknown"} and _text(finding.get("recommendation"))
    ]
def _measurement_context_items(snapshot: Mapping[str, Any]) -> list[tuple[str, str]]:
    context = snapshot["measurement_context"]

    def display(value: Any) -> str:
        if value is None:
            return "None supplied"
        if value == "unknown":
            return "Unknown"
        return _text(value)

    def display_list(values: Any) -> str:
        return ", ".join(_text(value) for value in values) or "None supplied"


    def display_window(window: Any) -> str:
        if window is None:
            return "None supplied"
        return f"{_text(window['value'])} {_text(window['unit'])}"

    return [
        ("Profile ID", display(context["profile_id"])),
        ("Source format", display(context["source_format"])),
        ("Source IDs", display_list(context["source_ids"])),
        ("Report grain", display_list(context["report_grain"])),
        ("Timezone", display(context["timezone"])),
        ("Currency", display(context["currency"])),
        ("Conversion definition", display(context["conversion_definition"])),
        ("Conversion actions", display_list(context["conversion_actions"])),
        ("Attribution model", display(context["attribution_model"])),
        ("Click attribution window", display_window(context["click_attribution_window"])),
        ("View attribution window", display_window(context["view_attribution_window"])),
        ("Counting behavior", display(context["counting_behavior"])),
        ("As of", display(context["as_of"])),
        ("Data finalization", display(context["data_finalization"])),
        ("Modeled-data treatment", display(context["modeled_data_treatment"])),
        ("Missing fields", display_list(context["missing_fields"])),
        ("Unsupported fields", display_list(context["unsupported_fields"])),
    ]


def render_markdown(bundle: Mapping[str, Any], *, registry: ControlRegistry) -> str:
    """Render a validated ReportBundle as deterministic Markdown."""

    bundle = _validated_bundle(bundle, registry)
    manifest = bundle["run_manifest"]
    snapshot = bundle["account_snapshot"]
    scoring = bundle["scoring"]
    controls = _controls(bundle)
    completeness = str(manifest["completeness"])
    score_status = str(scoring["status"])
    account = snapshot["account"]

    lines = [
        "# Claude Ads Audit Report",
        "",
        f"> Run completeness: **{_COMPLETENESS_LABELS[completeness]}** · Evidence status: **{_STATUS_LABELS[score_status]}**",
        "",
        "## Run summary",
        "",
        f"- Run ID: {_md(manifest['run_id'])}",
        f"- Started: {_md(manifest['started_at'])}",
        f"- Platform: {_md(str(account['platform']).title())}",
        f"- Account: {_md(account.get('name') or account['account_id'])}",
        f"- Window: {_md(snapshot['window']['start'])} to {_md(snapshot['window']['end'])}",
        f"- Privacy class: {_md(str(manifest['privacy_class']).title())}",
    ]
    lines.extend(("", "## Measurement context", ""))
    lines.extend(f"- {label}: {_md(value)}" for label, value in _measurement_context_items(snapshot))
    lines.extend(
        (
            "",
            "## Decision status",
            "",
            f"- Run completeness: **{_COMPLETENESS_LABELS[completeness]}**",
            f"- Evidence status: **{_STATUS_LABELS[score_status]}**",
            f"- Health score: **{_score_text(scoring['health_score'])}**",
            f"- Evidence coverage: **{_coverage_text(scoring['evidence_coverage'])}**",
        )
    )
    if completeness != "complete":
        lines.extend(("", "> WARNING: Required work did not complete; this report must not be presented as a complete audit."))
    if score_status == "provisional":
        lines.extend(("", "> WARNING: The health score is provisional because evidence coverage is below the normal threshold."))
    elif score_status == "insufficient_evidence":
        lines.extend(("", "> WARNING: Evidence is insufficient for a defensible health score."))

    lines.extend(("", "## Category health", ""))
    categories = _categories(bundle)
    if categories:
        for category in categories:
            lines.append(
                f"- **{_md(str(category.get('category', 'Uncategorized')).title())}:** "
                f"{_score_text(category.get('health_score'))}; evidence {_coverage_text(category.get('evidence_coverage', 0))}"
            )
    else:
        lines.append("No category scores were supplied.")

    lines.extend(("", "## Findings", ""))
    findings = _findings(bundle)
    if not findings:
        lines.append("No findings were supplied.")
    for finding in findings:
        control = controls.get(str(finding["control_id"]), {})
        heading = (
            f"### [{_md(str(finding['status']).replace('_', ' ').upper())}] "
            f"{_md(finding['control_id'])} — {_md(str(control.get('category', 'uncategorized')).title())}"
        )
        lines.extend(
            (
                heading,
                "",
                f"- Severity: {_md(str(control.get('severity', 'not specified')).title())}",
                f"- Confidence: {_md(str(finding['confidence']).title())}",
                f"- Source classification: {_md(str(finding.get('source_classification', 'not specified')).replace('_', ' ').title())}",
                "",
                f"**Observation:** {_md(finding['observation']) or 'Not supplied.'}",
                "",
                f"**Diagnosis:** {_md(finding['diagnosis']) or 'Not supplied.'}",
                "",
                f"**Recommended action:** {_md(finding['recommendation']) or 'No action supplied.'}",
                "",
                "**Evidence:**",
                "",
            )
        )
        if finding["evidence"]:
            for index, evidence in enumerate(finding["evidence"], start=1):
                lines.extend((f"{index}.", "", f"        {_canonical_json(evidence)}", ""))
        else:
            lines.extend(("No evidence was supplied.", ""))

    lines.extend(("## Contradictions", ""))
    contradictions = list(bundle.get("contradictions", []))
    if contradictions:
        lines.extend(f"- {_md(_extension_item_text(item))}" for item in contradictions)
    else:
        lines.append("No contradictions were reported.")

    lines.extend(("", "## Prioritized actions", ""))
    actions = _actions(bundle)
    if actions:
        lines.extend(f"{index}. {_md(_extension_item_text(item))}" for index, item in enumerate(actions, start=1))
    else:
        lines.append("No follow-up actions were reported.")

    lines.extend(("", "---", "", "Generated deterministically from ReportBundle JSON. Scores were recomputed from the supplied control registry and verified against this ReportBundle before rendering.", ""))
    return "\n".join(lines)


_HTML_STYLE = """
:root{color-scheme:light;--ink:#172033;--muted:#596579;--line:#d8dee9;--paper:#fff;--soft:#f5f7fa;--ok:#18794e;--warn:#9a6700;--bad:#c62828}
*{box-sizing:border-box}body{margin:0;background:#eef1f5;color:var(--ink);font:15px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:960px;margin:32px auto;padding:40px;background:var(--paper);box-shadow:0 8px 30px #17203318}h1,h2,h3{line-height:1.2}h1{margin-top:0}h2{margin-top:2rem;border-bottom:1px solid var(--line);padding-bottom:.4rem}h3{margin-top:1.5rem}.banner{padding:12px 16px;border-left:5px solid var(--ok);background:var(--soft);font-weight:700}.banner.partial,.banner.failed,.banner.provisional{border-color:var(--warn)}.banner.insufficient_evidence{border-color:var(--bad)}.warning{padding:10px 14px;background:#fff6d6;border:1px solid #e9c46a}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.metric{padding:14px;background:var(--soft);border:1px solid var(--line)}.metric strong{display:block;font-size:1.25rem}.meta{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px}.finding{padding:18px;margin:14px 0;border:1px solid var(--line);border-radius:6px}.status-pass{border-left:5px solid var(--ok)}.status-fail{border-left:5px solid var(--bad)}.status-unknown{border-left:5px solid var(--warn)}pre{overflow-wrap:anywhere;white-space:pre-wrap;background:var(--soft);padding:10px;border:1px solid var(--line)}dt{font-weight:700}dd{margin:0 0 10px}footer{margin-top:2rem;padding-top:1rem;border-top:1px solid var(--line);color:var(--muted)}@media print{body{background:#fff}main{box-shadow:none;margin:0;max-width:none}}
""".strip()


def render_html(bundle: Mapping[str, Any], *, registry: ControlRegistry) -> str:
    """Render a validated ReportBundle as self-contained deterministic HTML."""

    bundle = _validated_bundle(bundle, registry)
    manifest = bundle["run_manifest"]
    snapshot = bundle["account_snapshot"]
    scoring = bundle["scoring"]
    controls = _controls(bundle)
    completeness = str(manifest["completeness"])
    score_status = str(scoring["status"])
    account = snapshot["account"]
    warnings: list[str] = []
    if completeness != "complete":
        warnings.append("Required work did not complete; this report must not be presented as a complete audit.")
    if score_status == "provisional":
        warnings.append("The health score is provisional because evidence coverage is below the normal threshold.")
    elif score_status == "insufficient_evidence":
        warnings.append("Evidence is insufficient for a defensible health score.")

    category_html = "".join(
        "<li><strong>{}</strong>: {}; evidence {}</li>".format(
            _html(str(category.get("category", "Uncategorized")).title()),
            _html(_score_text(category.get("health_score"))),
            _html(_coverage_text(category.get("evidence_coverage", 0))),
        )
        for category in _categories(bundle)
    ) or "<li>No category scores were supplied.</li>"

    finding_parts: list[str] = []
    for finding in _findings(bundle):
        control = controls.get(str(finding["control_id"]), {})
        evidence = "".join(f"<pre>{html.escape(_canonical_json(item))}</pre>" for item in finding["evidence"])
        if not evidence:
            evidence = "<p>No evidence was supplied.</p>"
        status = str(finding["status"])
        finding_parts.append(
            f'<article class="finding status-{html.escape(status)}">'
            f"<h3>[{_html(status.replace('_', ' ').upper())}] {_html(finding['control_id'])} — "
            f"{_html(str(control.get('category', 'uncategorized')).title())}</h3>"
            "<dl>"
            f"<dt>Severity</dt><dd>{_html(str(control.get('severity', 'not specified')).title())}</dd>"
            f"<dt>Confidence</dt><dd>{_html(str(finding['confidence']).title())}</dd>"
            f"<dt>Source classification</dt><dd>{_html(str(finding.get('source_classification', 'not specified')).replace('_', ' ').title())}</dd>"
            f"<dt>Observation</dt><dd>{_html(finding['observation']) or 'Not supplied.'}</dd>"
            f"<dt>Diagnosis</dt><dd>{_html(finding['diagnosis']) or 'Not supplied.'}</dd>"
            f"<dt>Recommended action</dt><dd>{_html(finding['recommendation']) or 'No action supplied.'}</dd>"
            f"</dl><h4>Evidence</h4>{evidence}</article>"
        )
    findings_html = "".join(finding_parts) or "<p>No findings were supplied.</p>"

    contradictions = list(bundle.get("contradictions", []))
    contradictions_html = (
        "<ul>" + "".join(f"<li>{_html(_extension_item_text(item))}</li>" for item in contradictions) + "</ul>"
        if contradictions
        else "<p>No contradictions were reported.</p>"
    )
    actions = _actions(bundle)
    actions_html = (
        "<ol>" + "".join(f"<li>{_html(_extension_item_text(item))}</li>" for item in actions) + "</ol>"
        if actions
        else "<p>No follow-up actions were reported.</p>"
    )
    warnings_html = "".join(f'<p class="warning"><strong>Warning:</strong> {_html(item)}</p>' for item in warnings)
    context_html = "".join(
        f"<dt>{_html(label)}</dt><dd>{_html(value)}</dd>"
        for label, value in _measurement_context_items(snapshot)
    )

    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Claude Ads Audit Report</title><style>" + _HTML_STYLE + "</style></head><body><main>"
        "<h1>Claude Ads Audit Report</h1>"
        f'<p class="banner {html.escape(completeness)} {html.escape(score_status)}">Run completeness: '
        f"{_html(_COMPLETENESS_LABELS[completeness])} · Evidence status: {_html(_STATUS_LABELS[score_status])}</p>"
        "<h2>Run summary</h2><dl class=\"meta\">"
        f"<dt>Run ID</dt><dd>{_html(manifest['run_id'])}</dd><dt>Started</dt><dd>{_html(manifest['started_at'])}</dd>"
        f"<dt>Platform</dt><dd>{_html(str(account['platform']).title())}</dd>"
        f"<dt>Account</dt><dd>{_html(account.get('name') or account['account_id'])}</dd>"
        f"<dt>Window</dt><dd>{_html(snapshot['window']['start'])} to {_html(snapshot['window']['end'])}</dd>"
        f"<dt>Privacy class</dt><dd>{_html(str(manifest['privacy_class']).title())}</dd></dl>"
        f"<h2>Measurement context</h2><dl class=\"meta\">{context_html}</dl>"
        "<h2>Decision status</h2><div class=\"metrics\">"
        f'<div class="metric">Run completeness<strong>{_html(_COMPLETENESS_LABELS[completeness])}</strong></div>'
        f'<div class="metric">Evidence status<strong>{_html(_STATUS_LABELS[score_status])}</strong></div>'
        f'<div class="metric">Health score<strong>{_html(_score_text(scoring["health_score"]))}</strong></div>'
        f'<div class="metric">Evidence coverage<strong>{_html(_coverage_text(scoring["evidence_coverage"]))}</strong></div></div>'
        + warnings_html
        + f"<h2>Category health</h2><ul>{category_html}</ul><h2>Findings</h2>{findings_html}"
        + f"<h2>Contradictions</h2>{contradictions_html}<h2>Prioritized actions</h2>{actions_html}"
        + "<footer>Generated deterministically from ReportBundle JSON. Scores were recomputed from the supplied control registry and verified against this ReportBundle before rendering.</footer>"
        + "</main></body></html>\n"
    )


def render_pdf(bundle: Mapping[str, Any], *, registry: ControlRegistry) -> bytes:
    """Render PDF bytes through the optional WeasyPrint bridge."""

    source = render_html(bundle, registry=registry)
    try:
        weasyprint = importlib.import_module("weasyprint")
    except (ImportError, OSError) as exc:
        raise PDFDependencyError(
            "PDF rendering requires the optional 'weasyprint' package and its system libraries; "
            "install WeasyPrint or render Markdown/HTML instead"
        ) from exc
    try:
        result = weasyprint.HTML(string=source, base_url=None).write_pdf()
    except Exception as exc:
        raise ReportRenderError(f"PDF rendering failed: {exc}") from exc
    if (
        not isinstance(result, bytes)
        or len(result) <= len(b"%PDF-")
        or not result.startswith(b"%PDF-")
        or not result.rstrip(b" \t\r\n\f\x00").endswith(b"%%EOF")
    ):
        raise ReportRenderError("PDF renderer returned invalid PDF output")
    return result


def render_report(
    bundle: Mapping[str, Any], output_format: str, *, registry: ControlRegistry
) -> str | bytes:
    """Render *bundle* to ``markdown``, ``html``, or ``pdf``."""

    normalized = output_format.lower()
    if normalized in {"md", "markdown"}:
        return render_markdown(bundle, registry=registry)
    if normalized == "html":
        return render_html(bundle, registry=registry)
    if normalized == "pdf":
        return render_pdf(bundle, registry=registry)
    raise ReportRenderError("format must be one of: markdown, html, pdf")


def _validate_report_destination(destination: str | Path) -> Path:
    """Validate report destination syntax without touching the filesystem."""

    raw = destination.as_posix() if isinstance(destination, Path) else str(destination)
    device_digits = str.maketrans({"¹": "1", "²": "2", "³": "3"})
    reserved = {"con", "prn", "aux", "nul"} | {f"com{index}" for index in range(1, 10)} | {
        f"lpt{index}" for index in range(1, 10)
    }
    parts = raw.split("/")
    if (
        not raw
        or raw.startswith("/")
        or raw.startswith("//")
        or re.match(r"^[A-Za-z]:", raw)
        or "\\" in raw
        or ":" in raw
        or re.search(r'[\x00-\x1f<>\"|?*]', raw)
        or any(
            not part
            or part in {".", ".."}
            or part[-1] in ". "
            or part.split(".", 1)[0].translate(device_digits).casefold() in reserved
            for part in parts
        )
    ):
        raise ReportRenderError("report output must be a non-empty relative path without traversal")
    return Path(*parts)


def resolve_report_path(root: str | Path, destination: str | Path) -> Path:
    """Return the nominal absolute output path without touching the filesystem."""

    try:
        root_path = Path(root).absolute()
    except (OSError, RuntimeError) as exc:
        raise ReportRenderError(f"report root normalization failed: {exc}") from exc
    return root_path / _validate_report_destination(destination)


def _require_posix_capabilities() -> None:
    required = ("open", "mkdir", "stat", "rename", "unlink")
    supports_dir_fd = getattr(os, "supports_dir_fd", ())
    if (
        os.name != "posix"
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_CLOEXEC")
        or not callable(getattr(os, "getuid", None))
    ):
        raise ReportRenderError("report output requires POSIX directory capabilities")
    if any(
        not hasattr(os, name)
        or (
            getattr(os, name) not in supports_dir_fd
            and _POSIX_CAPABILITY_FUNCS[name] not in supports_dir_fd
        )
        for name in required
    ):
        raise ReportRenderError("report output requires POSIX directory capabilities")
    supports_follow_symlinks = getattr(os, "supports_follow_symlinks", ())
    if os.stat not in supports_follow_symlinks and _POSIX_CAPABILITY_FUNCS["stat"] not in supports_follow_symlinks:
        raise ReportRenderError("report output requires POSIX no-follow stat capability")


def _directory_flags() -> int:
    return os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _check_private_directory(file_descriptor: int, label: str) -> os.stat_result:
    info = os.fstat(file_descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise ReportRenderError(f"report {label} must be a directory")
    if info.st_uid != os.getuid():
        raise ReportRenderError(f"report {label} must be owned by the current user")
    if info.st_mode & 0o077:
        raise ReportRenderError(f"report {label} must be private (mode must not grant group or other access)")
    return info


def _open_posix_parent(root_fd: int, parts: tuple[str, ...]) -> tuple[int, list[int]]:
    parent_fd = root_fd
    opened = [root_fd]
    try:
        for part in parts:
            try:
                entry = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
                entry = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(entry.st_mode):
                raise ReportRenderError("report output parent must not be a symlink")
            if not stat.S_ISDIR(entry.st_mode):
                raise ReportRenderError("report output parent must be a directory")
            child_fd = os.open(part, _directory_flags(), dir_fd=parent_fd)
            opened.append(child_fd)
            _check_private_directory(child_fd, "output parent")
            parent_fd = child_fd
    except BaseException as primary_error:
        close_errors: list[BaseException] = []
        for file_descriptor in reversed(opened[1:]):
            try:
                os.close(file_descriptor)
            except BaseException as close_error:
                close_errors.append(close_error)
        if close_errors:
            details = "; ".join(str(error) for error in close_errors)
            close_context = f"parent descriptor cleanup failed: {details}"
            if isinstance(primary_error, Exception):
                raise ReportRenderError(f"{primary_error}; {close_context}") from primary_error
            primary_error.add_note(close_context)
        raise
    return parent_fd, opened


def _read_exact(file_descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(file_descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _verify_posix_namespace(
    root_path: Path,
    relative: Path,
    held_root: os.stat_result,
    held_parent: os.stat_result,
    held_leaf: os.stat_result,
    expected: bytes,
) -> None:
    fresh_fds: list[int] = []
    verification_error: BaseException | None = None
    close_errors: list[BaseException] = []
    try:
        fresh_root_fd = os.open(root_path, _directory_flags())
        fresh_fds.append(fresh_root_fd)
        fresh_root = _check_private_directory(fresh_root_fd, "root")
        if (fresh_root.st_dev, fresh_root.st_ino) != (held_root.st_dev, held_root.st_ino):
            raise ReportRenderError("report root namespace identity changed")
        fresh_parent_fd = fresh_root_fd
        for part in relative.parts[:-1]:
            entry = os.stat(part, dir_fd=fresh_parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(entry.st_mode):
                raise ReportRenderError("report output parent must not be a symlink")
            if not stat.S_ISDIR(entry.st_mode):
                raise ReportRenderError("report output parent must be a directory")
            fresh_parent_fd = os.open(part, _directory_flags(), dir_fd=fresh_parent_fd)
            fresh_fds.append(fresh_parent_fd)
            _check_private_directory(fresh_parent_fd, "output parent")
        fresh_parent = os.fstat(fresh_parent_fd)
        if (fresh_parent.st_dev, fresh_parent.st_ino) != (held_parent.st_dev, held_parent.st_ino):
            raise ReportRenderError("report output parent namespace identity changed")
        leaf_fd = os.open(
            relative.name,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=fresh_parent_fd,
        )
        fresh_fds.append(leaf_fd)
        leaf = os.fstat(leaf_fd)
        if not stat.S_ISREG(leaf.st_mode):
            raise ReportRenderError("report output verification found a non-regular file")
        if (leaf.st_dev, leaf.st_ino) != (held_leaf.st_dev, held_leaf.st_ino):
            raise ReportRenderError("report output namespace identity changed")
        if _read_exact(leaf_fd) != expected:
            raise ReportRenderError("report output verification found unexpected content")
        os.fsync(fresh_parent_fd)
    except BaseException as exc:
        verification_error = exc
    finally:
        for file_descriptor in reversed(fresh_fds):
            try:
                os.close(file_descriptor)
            except BaseException as exc:
                close_errors.append(exc)
    if close_errors:
        details = "; ".join(str(error) for error in close_errors)
        base_close_error = next((error for error in close_errors if not isinstance(error, Exception)), None)
        if base_close_error is not None:
            if verification_error is not None:
                base_close_error.add_note(str(verification_error))
            base_close_error.add_note(f"report output verification descriptor close failed: {details}")
            raise base_close_error
        close_error = ReportRenderError(f"report output verification close failed: {details}")
        if verification_error is not None:
            if isinstance(verification_error, Exception):
                raise ReportRenderError(f"{verification_error}; {close_error}") from verification_error
            verification_error.add_note(str(close_error))
            raise verification_error
        raise close_error
    if verification_error is not None:
        raise verification_error
def _atomic_write_posix(root: str | Path, destination: str | Path, expected: bytes) -> Path:
    _require_posix_capabilities()
    relative = _validate_report_destination(destination)
    try:
        root_path = Path(root).absolute()
    except (OSError, RuntimeError) as exc:
        raise ReportRenderError(f"report root normalization failed: {exc}") from exc
    try:
        root_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise ReportRenderError(f"report root creation failed: {exc}") from exc

    root_fd = None
    parent_fd = None
    opened_parent_fds: list[int] = []
    temporary_name: str | None = None
    temporary_owned = False
    replace_called = False
    replace_returned = False
    temporary_absence_verified = False
    replacement_committed = False
    outcome_unknown = False
    primary_error: BaseException | None = None
    close_errors: list[BaseException] = []
    result = root_path / relative

    def normalize_error(error: BaseException) -> BaseException:
        if isinstance(error, ReportRenderError):
            normalized: BaseException = error
        elif isinstance(error, Exception):
            normalized = ReportRenderError(f"report output operation failed: {error}")
        else:
            normalized = error
        if replacement_committed and isinstance(normalized, Exception) and "replacement occurred" not in str(normalized):
            normalized = ReportRenderError(
                f"report output replacement occurred but verification/durability failed: {normalized}"
            )
        return normalized

    try:
        root_fd = os.open(root_path, _directory_flags())
        held_root = _check_private_directory(root_fd, "root")
        parent_fd, opened_parent_fds = _open_posix_parent(root_fd, tuple(relative.parts[:-1]))
        held_parent = os.fstat(parent_fd)
        try:
            destination_info = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            destination_info = None
        if destination_info is not None:
            if stat.S_ISLNK(destination_info.st_mode):
                raise ReportRenderError("report output must not be a symlink")
            if not stat.S_ISREG(destination_info.st_mode):
                raise ReportRenderError("report output must be a regular file")

        temporary_name = f".{relative.name}.{secrets.token_hex(16)}"
        temporary_flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
        )
        try:
            temporary_fd = os.open(temporary_name, temporary_flags, 0o600, dir_fd=parent_fd)
        except OSError as exc:
            raise ReportRenderError(f"report output temporary file open failed: {exc}") from exc
        temporary_owned = True
        stage_error: BaseException | None = None
        try:
            data = memoryview(expected)
            written = 0
            while written < len(data):
                count = os.write(temporary_fd, data[written:])
                if count <= 0:
                    raise OSError("write returned no progress")
                written += count
            os.fsync(temporary_fd)
            temporary_info = os.fstat(temporary_fd)
            if not stat.S_ISREG(temporary_info.st_mode):
                raise ReportRenderError("report temporary file must be regular")
        except BaseException as exc:
            stage_error = exc
        finally:
            try:
                os.close(temporary_fd)
            except BaseException as exc:
                if stage_error is None:
                    stage_error = ReportRenderError(f"report output close failed: {exc}") if isinstance(exc, Exception) else exc
                elif isinstance(stage_error, Exception):
                    stage_error = ReportRenderError(f"{stage_error}; report output close failed: {exc}")
                else:
                    stage_error.add_note(f"report output close failed: {exc}")
        if stage_error is not None:
            raise stage_error

        replace_called = True
        try:
            os.replace(
                temporary_name,
                relative.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            replace_returned = True
        except OSError as exc:
            raise ReportRenderError(f"report output replacement failed: {exc}") from exc
        except BaseException as exc:
            outcome_unknown = True
            exc.add_note("report output replacement outcome is unknown")
            raise

        try:
            os.stat(temporary_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            temporary_absence_verified = True
            replacement_committed = temporary_absence_verified
            temporary_owned = False
        except OSError as exc:
            outcome_unknown = True
            raise ReportRenderError(f"report output replacement outcome is unknown: {exc}") from exc
        except BaseException as exc:
            outcome_unknown = True
            exc.add_note("report output replacement outcome is unknown")
            raise
        else:
            no_op_error = ReportRenderError("report output replacement was a no-op: temporary file remains")
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except BaseException as exc:
                if isinstance(exc, Exception):
                    raise ReportRenderError(f"{no_op_error}; temporary cleanup failed: {exc}") from no_op_error
                exc.add_note(str(no_op_error))
                raise
            temporary_owned = False
            raise no_op_error

        try:
            leaf_fd = os.open(
                relative.name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0),
                dir_fd=parent_fd,
            )
            verification_error: BaseException | None = None
            try:
                held_leaf = os.fstat(leaf_fd)
                if not stat.S_ISREG(held_leaf.st_mode):
                    raise ReportRenderError("report output verification found a non-regular file")
                if (held_leaf.st_dev, held_leaf.st_ino) != (temporary_info.st_dev, temporary_info.st_ino):
                    raise ReportRenderError("report output verification found unexpected file identity")
                if _read_exact(leaf_fd) != expected:
                    raise ReportRenderError("report output verification found unexpected content")
            except BaseException as exc:
                verification_error = exc
            finally:
                try:
                    os.close(leaf_fd)
                except BaseException as exc:
                    if verification_error is None:
                        verification_error = (
                            ReportRenderError(f"report output verification close failed: {exc}")
                            if isinstance(exc, Exception)
                            else exc
                        )
                    elif isinstance(verification_error, Exception):
                        verification_error = ReportRenderError(f"{verification_error}; verification close failed: {exc}")
                    else:
                        verification_error.add_note(f"verification close failed: {exc}")
            if verification_error is not None:
                raise verification_error
            os.fsync(parent_fd)
            _verify_posix_namespace(root_path, relative, held_root, held_parent, held_leaf, expected)
        except BaseException as exc:
            if isinstance(exc, Exception):
                raise ReportRenderError(
                    f"report output replacement occurred but verification/durability failed: {exc}"
                ) from exc
            exc.add_note("report output replacement occurred before verification/durability interruption")
            raise
    except BaseException as exc:
        if temporary_owned and temporary_name is not None and parent_fd is not None and not outcome_unknown and not replacement_committed and (
            not replace_called or not replace_returned
        ):
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
                temporary_owned = False
            except BaseException as cleanup_error:
                if isinstance(exc, Exception):
                    exc = ReportRenderError(f"{exc}; temporary cleanup failed: {cleanup_error}")
                else:
                    exc.add_note(f"temporary cleanup failed: {cleanup_error}")
        primary_error = normalize_error(exc)
    finally:
        for file_descriptor in reversed(opened_parent_fds):
            try:
                os.close(file_descriptor)
            except BaseException as exc:
                close_errors.append(exc)
        if root_fd is not None and root_fd not in opened_parent_fds:
            try:
                os.close(root_fd)
            except BaseException as exc:
                close_errors.append(exc)

    if close_errors:
        details = "; ".join(str(error) for error in close_errors)
        base_close_error = next((error for error in close_errors if not isinstance(error, Exception)), None)
        if base_close_error is not None:
            if primary_error is not None:
                base_close_error.add_note(str(primary_error))
            if replacement_committed:
                base_close_error.add_note("report output replacement occurred before descriptor close failure")
            base_close_error.add_note(f"descriptor close failed: {details}")
            primary_error = base_close_error
        else:
            close_error = (
                ReportRenderError(
                    f"report output replacement occurred but descriptor close failed: {details}"
                )
                if replacement_committed
                else ReportRenderError(f"report output descriptor close failed: {details}")
            )
            if primary_error is None:
                primary_error = close_error
            elif isinstance(primary_error, Exception):
                primary_error = ReportRenderError(f"{primary_error}; {close_error}")
            else:
                primary_error.add_note(str(close_error))
    if primary_error is not None:
        raise primary_error
    return result


def _windows_reparse(info: os.stat_result) -> bool:
    try:
        attributes = info.st_file_attributes
    except AttributeError as exc:
        raise ReportRenderError("report Windows link metadata is unavailable") from exc
    return bool(attributes & 0x400)


_WINDOWS_TRUSTED_SIDS = frozenset(
    {
        "S-1-5-18",  # LocalSystem
        "S-1-5-32-544",  # Built-in Administrators
    }
)
_WINDOWS_MUTATING_RIGHTS = (
    "fullcontrol",
    "modify",
    "write",
    "writedata",
    "appenddata",
    "createfiles",
    "createdirectories",
    "delete",
    "changepermissions",
    "takeownership",
    "writeattributes",
    "writeextendedattributes",
)

_WINDOWS_ACL_QUERY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$acl = Get-Acl -LiteralPath $args[0]
$current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
$access = @($acl.Access | ForEach-Object {
    $sid = $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    [pscustomobject]@{
        sid = $sid
        type = [string]$_.AccessControlType
        rights = [string]$_.FileSystemRights
        inherited = [bool]$_.IsInherited
    }
})
[pscustomobject]@{ owner_sid = $owner; current_sid = $current; access = $access } |
    ConvertTo-Json -Compress -Depth 8
""".strip()

_WINDOWS_ACL_APPLY = r"""
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath $args[0]
$sid = New-Object System.Security.Principal.SecurityIdentifier(
    [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
)
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) {
    [void]$acl.RemoveAccessRuleSpecific($rule)
}
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $sid,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    [System.Security.AccessControl.AccessControlType]::Allow
)
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $args[0] -AclObject $acl
""".strip()


def _windows_acl_snapshot(path: Path) -> Mapping[str, Any]:
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_ACL_QUERY, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            shell=False,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise ReportRenderError(f"report Windows ACL query failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown error"
        raise ReportRenderError(f"report Windows ACL query failed: {detail}")
    try:
        snapshot = json.loads(result.stdout)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReportRenderError("report Windows ACL query returned invalid data") from exc
    if not isinstance(snapshot, Mapping):
        raise ReportRenderError("report Windows ACL query returned invalid data")
    access = snapshot.get("access")
    if isinstance(access, Mapping):
        access = [access]
    if not isinstance(access, list):
        raise ReportRenderError("report Windows ACL query returned unverifiable DACL")
    normalized = dict(snapshot)
    normalized["access"] = access
    return normalized


def _validate_windows_acl(path: Path, label: str) -> None:
    snapshot = _windows_acl_snapshot(path)
    owner_sid = snapshot.get("owner_sid")
    current_sid = snapshot.get("current_sid")
    access = snapshot.get("access")
    if not isinstance(owner_sid, str) or not owner_sid:
        raise ReportRenderError(f"report {label} owner is unverifiable")
    if not isinstance(current_sid, str) or not current_sid:
        raise ReportRenderError(f"report {label} current user is unverifiable")
    owner_folded = owner_sid.casefold()
    current_folded = current_sid.casefold()
    if owner_folded != current_folded and owner_sid.upper() not in _WINDOWS_TRUSTED_SIDS:
        raise ReportRenderError(f"report {label} must be owned by current user or a trusted Windows identity")
    if not isinstance(access, list) or not access:
        raise ReportRenderError(f"report {label} DACL is unprotected or unverifiable")

    for entry in access:
        if not isinstance(entry, Mapping):
            raise ReportRenderError(f"report {label} DACL is unverifiable")
        sid = entry.get("sid")
        access_type = entry.get("type")
        rights = entry.get("rights")
        if not isinstance(sid, str) or not sid or not isinstance(access_type, str) or not isinstance(rights, str):
            raise ReportRenderError(f"report {label} DACL is unverifiable")
        sid_folded = sid.casefold()
        rights_folded = rights.casefold()
        if access_type.casefold() == "deny":
            if sid_folded == current_folded and any(token in rights_folded for token in _WINDOWS_MUTATING_RIGHTS):
                raise ReportRenderError(f"report {label} DACL denies current-user access")
            continue
        if access_type.casefold() != "allow":
            raise ReportRenderError(f"report {label} DACL is unverifiable")
        if (
            sid_folded != current_folded
            and sid.upper() not in _WINDOWS_TRUSTED_SIDS
            and any(token in rights_folded for token in _WINDOWS_MUTATING_RIGHTS)
        ):
            raise ReportRenderError(f"report {label} DACL is permissive")


def _protect_windows_path(path: Path) -> None:
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_ACL_APPLY, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            shell=False,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise ReportRenderError(f"report Windows ACL protection failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown error"
        raise ReportRenderError(f"report Windows ACL protection failed: {detail}")
    _validate_windows_acl(path, "output")



def _validate_windows_tree(root_path: Path, destination: Path) -> Path:
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError) as exc:
        raise ReportRenderError(f"report home normalization failed: {exc}") from exc
    _validate_windows_acl(home, "home")
    try:
        root_path.resolve(strict=False).relative_to(home)
        root_path.relative_to(home)
    except (OSError, ValueError) as exc:
        raise ReportRenderError("report root must be beneath the current user's home") from exc

    current = home
    relative_root = root_path.relative_to(home)
    for part in relative_root.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or _windows_reparse(info):
                raise ReportRenderError("report root must not contain links or reparse points")
            if not stat.S_ISDIR(info.st_mode):
                raise ReportRenderError("report root must be a directory")
            _validate_windows_acl(current, "root")
        else:
            current.mkdir(mode=0o700)
            _protect_windows_path(current)

    parent = root_path
    for part in destination.parts[:-1]:
        parent = parent / part
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or _windows_reparse(info):
                raise ReportRenderError("report output parent must not contain links or reparse points")
            if not stat.S_ISDIR(info.st_mode):
                raise ReportRenderError("report output parent must be a directory")
            _validate_windows_acl(parent, "output parent")
        else:
            parent.mkdir(mode=0o700)
            _protect_windows_path(parent)

    leaf = parent / destination.name
    if leaf.exists() or leaf.is_symlink():
        info = leaf.lstat()
        if stat.S_ISLNK(info.st_mode) or _windows_reparse(info):
            raise ReportRenderError("report output must not be a link or reparse point")
        if not stat.S_ISREG(info.st_mode):
            raise ReportRenderError("report output must be a regular file")
        _validate_windows_acl(leaf, "output")
    return leaf


def _atomic_write_windows(root: str | Path, destination: str | Path, expected: bytes) -> Path:
    relative = _validate_report_destination(destination)
    try:
        root_path = Path(root).expanduser().absolute()
    except (OSError, RuntimeError) as exc:
        raise ReportRenderError(f"report root normalization failed: {exc}") from exc
    try:
        output_path = _validate_windows_tree(root_path, relative)
    except ReportRenderError:
        raise
    except OSError as exc:
        raise ReportRenderError(f"report output path validation failed: {exc}") from exc
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output_path.name}.", dir=output_path.parent)
    except OSError as exc:
        raise ReportRenderError(f"report output temporary file failed: {exc}") from exc

    temporary_path = Path(temporary_name)
    temporary_owned = True
    replace_called = False
    replace_returned = False
    temporary_absence_verified = False
    replacement_committed = False
    outcome_unknown = False
    primary_error: BaseException | None = None
    stage_error: BaseException | None = None
    try:
        try:
            data = memoryview(expected)
            written = 0
            while written < len(data):
                count = os.write(file_descriptor, data[written:])
                if count <= 0:
                    raise OSError("write returned no progress")
                written += count
            os.fsync(file_descriptor)
        except BaseException as exc:
            stage_error = exc
        finally:
            try:
                os.close(file_descriptor)
            except BaseException as exc:
                if stage_error is None:
                    stage_error = ReportRenderError(f"report output close failed: {exc}") if isinstance(exc, Exception) else exc
                elif isinstance(stage_error, Exception):
                    stage_error = ReportRenderError(f"{stage_error}; report output close failed: {exc}")
                else:
                    stage_error.add_note(f"report output close failed: {exc}")
        if stage_error is not None:
            raise stage_error

        _protect_windows_path(temporary_path)
        _validate_windows_tree(root_path, relative)
        replace_called = True
        try:
            os.replace(temporary_path, output_path)
            replace_returned = True
        except OSError as exc:
            raise ReportRenderError(f"report output replacement failed: {exc}") from exc
        except BaseException as exc:
            outcome_unknown = True
            exc.add_note("report output replacement outcome is unknown")
            raise
        _protect_windows_path(output_path)

        try:
            temporary_path.lstat()
        except FileNotFoundError:
            temporary_absence_verified = True
            replacement_committed = temporary_absence_verified
            temporary_owned = False
        except BaseException as exc:
            outcome_unknown = True
            if isinstance(exc, Exception):
                raise ReportRenderError(f"report output replacement outcome is unknown: {exc}") from exc
            exc.add_note("report output replacement outcome is unknown")
            raise
        else:
            no_op_error = ReportRenderError("report output replacement was a no-op: temporary file remains")
            try:
                temporary_path.unlink()
            except BaseException as exc:
                if isinstance(exc, Exception):
                    raise ReportRenderError(f"{no_op_error}; temporary cleanup failed: {exc}") from no_op_error
                exc.add_note(str(no_op_error))
                raise
            temporary_owned = False
            raise no_op_error

        try:
            with output_path.open("rb") as stream:
                actual = stream.read()
        except BaseException as exc:
            if isinstance(exc, Exception):
                raise ReportRenderError(f"report output replacement occurred but verification failed: {exc}") from exc
            exc.add_note("report output replacement occurred before verification interruption")
            raise
        if actual != expected:
            raise ReportRenderError(
                "report output replacement occurred but verification found unexpected content"
            )
        return output_path
    except BaseException as exc:
        if temporary_owned and not outcome_unknown and not replacement_committed and (not replace_called or not replace_returned):
            try:
                temporary_path.unlink(missing_ok=True)
                temporary_owned = False
            except BaseException as cleanup_error:
                if isinstance(exc, Exception):
                    exc = ReportRenderError(f"{exc}; temporary cleanup failed: {cleanup_error}")
                else:
                    exc.add_note(f"temporary cleanup failed: {cleanup_error}")
        if isinstance(exc, ReportRenderError):
            primary_error = exc
        elif isinstance(exc, Exception):
            primary_error = ReportRenderError(f"report output operation failed: {exc}")
        else:
            primary_error = exc
    if primary_error is not None:
        raise primary_error
    return output_path


def atomic_write_report(root: str | Path, destination: str | Path, content: str | bytes) -> Path:
    """Atomically write report content beneath a safe root."""

    _validate_report_destination(destination)
    expected_bytes = content if isinstance(content, bytes) else content.encode("utf-8")
    if os.name == "posix":
        return _atomic_write_posix(root, destination, expected_bytes)
    if os.name == "nt":
        return _atomic_write_windows(root, destination, expected_bytes)
    raise ReportRenderError("report output writing unsupported on this platform")

def write_report_bundle(
    bundle: Mapping[str, Any],
    output_format: str,
    root: str | Path,
    destination: str | Path,
    *,
    registry: ControlRegistry,
) -> Path:
    """Validate, render, and atomically write a report bundle."""

    _validate_report_destination(destination)
    return atomic_write_report(root, destination, render_report(bundle, output_format, registry=registry))
