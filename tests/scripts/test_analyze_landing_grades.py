"""A landing page that never loaded is missing evidence, not a failing page."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import analyze_landing  # noqa: E402


def _skeleton(**overrides) -> dict:
    """The shape analyze_landing builds before any page interaction."""
    base = {
        "url": "https://example.test",
        "performance": {"lcp_ms": None, "load_time_ms": None, "page_size_kb": None},
        "content": {"h1": None, "title": None, "word_count": 0},
        "conversion": {
            "cta_above_fold": False,
            "form_present": False,
            "form_fields": 0,
            "phone_number": False,
            "chat_widget": False,
        },
        "mobile": {"viewport_meta": False, "horizontal_scroll": False, "font_readable": False},
        "schema": {"product_schema": False, "faq_schema": False, "service_schema": False},
        "error": None,
    }
    base.update(overrides)
    return base


def test_unfetched_page_is_unknown_never_fail():
    """Grading the untouched skeleton previously reported FAIL on every check."""
    grades = analyze_landing.grade_landing(_skeleton(error="Connection error: DNS failure"))
    assert set(grades) == set(analyze_landing.GRADE_KEYS)
    assert set(grades.values()) == {analyze_landing.UNKNOWN}
    assert "FAIL" not in grades.values()


def test_missing_lcp_on_a_fetched_page_is_unknown_not_slow():
    grades = analyze_landing.grade_landing(_skeleton())
    assert grades["G59_mobile_speed"] == analyze_landing.UNKNOWN


def test_real_measurements_still_grade_normally():
    fetched = _skeleton()
    fetched["performance"]["lcp_ms"] = 1200
    fetched["content"]["h1"] = "Buy our thing"
    fetched["schema"]["product_schema"] = True
    grades = analyze_landing.grade_landing(fetched)
    assert grades["G59_mobile_speed"] == "PASS"
    assert grades["G60_relevance"] == "PASS"
    assert grades["G61_schema"] == "PASS"


def test_slow_page_still_fails():
    fetched = _skeleton()
    fetched["performance"]["lcp_ms"] = 5000
    assert analyze_landing.grade_landing(fetched)["G59_mobile_speed"] == "FAIL"
