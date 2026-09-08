"""Behavior tests for quota-gated, resumable Ad Library collection."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

import fetch_ad_library  # noqa: E402


class Response:
    def __init__(
        self, payload: dict, status_code: int = 200, headers: dict | None = None
    ):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


class RecordingBudget:
    def __init__(self, *, stop_after: int | None = None):
        self.entered = 0
        self.observed = []
        self.stop_after = stop_after

    @contextmanager
    def attempt(self):
        self.entered += 1
        if self.stop_after is not None and self.entered > self.stop_after:
            raise fetch_ad_library.QuotaDeferred("test quota stop", retry_at=1234.0)
        yield

    def observe(self, headers, *, status_code, error_code=None):
        self.observed.append((dict(headers or {}), status_code, error_code))
        return {"usage": None, "stop_reason": None, "retry_at": None}


def _page(*ads, next_url: str | None = None):
    return {"data": list(ads), "paging": ({"next": next_url} if next_url else {})}


def _ad(ad_id: str, page_id: str = "page-1"):
    return {"id": ad_id, "page_id": page_id, "page_name": page_id}


def _call_queue(monkeypatch, checkpoint: Path, responses, **kwargs):
    calls = []

    def fake_guarded_request(session, method, url, **request_kwargs):
        calls.append({"url": url, **request_kwargs})
        response = responses[len(calls) - 1]
        return response() if callable(response) else response

    monkeypatch.setattr(fetch_ad_library, "guarded_request", fake_guarded_request)
    result = fetch_ad_library.collect_advertiser_queue(
        token="secret-token",
        countries=["DE"],
        search_page_ids="page-1",
        checkpoint_path=checkpoint,
        quota_budget=kwargs.pop("quota_budget", RecordingBudget()),
        **kwargs,
    )
    return result, calls


def test_interrupted_pagination_resumes_exact_opaque_cursor_without_duplicates(
    tmp_path, monkeypatch
):
    checkpoint = tmp_path / "checkpoint.json"
    first = _page(_ad("ad-1"), next_url=f"{fetch_ad_library.ENDPOINT}?after=cursor-1")
    result1, calls1 = _call_queue(
        monkeypatch, checkpoint, [Response(first)], max_pages=1
    )
    assert result1["advertisers"][0]["status"] == "paginated"
    assert (
        json.loads(checkpoint.read_text())["advertisers"][0]["next_cursor"]
        == "cursor-1"
    )

    second = _page(_ad("ad-1"), _ad("ad-2"))
    result2, calls2 = _call_queue(
        monkeypatch, checkpoint, [Response(second)], resume=True, max_pages=2
    )
    assert result2["advertisers"][0]["status"] == "exhausted"
    assert result2["advertisers"][0]["artifact"]["collection"]["status"] == "exhausted"
    assert result2["advertisers"][0]["artifact"]["collection"]["next_cursor"] is None
    assert [
        obs["observation_id"]
        for obs in result2["advertisers"][0]["artifact"]["observations"]
    ] == [
        "meta-ad-library.ad-1",
        "meta-ad-library.ad-2",
    ]
    assert calls2[0]["url"] == fetch_ad_library.ENDPOINT
    assert calls2[0]["params"]["after"] == "cursor-1"
    assert "cursor-1" not in json.dumps(calls2[0]["url"])


def test_direct_api_resume_recovers_omitted_run_identity(tmp_path, monkeypatch):
    checkpoint = tmp_path / "checkpoint.json"
    first = _page(_ad("ad-1"), next_url=f"{fetch_ad_library.ENDPOINT}?after=cursor-1")
    _call_queue(monkeypatch, checkpoint, [Response(first)], max_pages=1)

    result, calls = _call_queue(
        monkeypatch,
        checkpoint,
        [Response(_page(_ad("ad-2")))],
        resume=True,
        max_pages=1,
    )

    assert result["advertisers"][0]["status"] == "exhausted"
    assert calls[0]["params"]["after"] == "cursor-1"


def test_resume_fingerprint_mismatch_rejects_before_network(tmp_path, monkeypatch):
    checkpoint = tmp_path / "checkpoint.json"
    _call_queue(monkeypatch, checkpoint, [Response(_page(_ad("ad-1")))])
    calls = []
    monkeypatch.setattr(
        fetch_ad_library, "guarded_request", lambda *a, **k: calls.append(k)
    )
    with pytest.raises(ValueError, match="checkpoint query mismatch"):
        fetch_ad_library.collect_advertiser_queue(
            token="secret-token",
            countries=["FR"],
            search_page_ids="page-1",
            checkpoint_path=checkpoint,
            resume=True,
            quota_budget=RecordingBudget(),
        )
    assert calls == []


def test_token_bearing_server_next_url_is_reduced_to_opaque_cursor(
    tmp_path, monkeypatch
):
    checkpoint = tmp_path / "checkpoint.json"
    next_url = f"{fetch_ad_library.ENDPOINT}?after=opaque&access_token=SECRET&foo=bar"
    result, _ = _call_queue(
        monkeypatch,
        checkpoint,
        [Response(_page(_ad("ad-1"), next_url=next_url))],
        max_pages=1,
    )
    saved = checkpoint.read_text()
    assert result["advertisers"][0]["status"] == "paginated"
    assert "SECRET" not in saved
    assert "access_token" not in saved
    assert "foo" not in saved
    assert json.loads(saved)["advertisers"][0]["next_cursor"] == "opaque"


def test_final_page_is_exhausted_and_unchanged_exhausted_resume_does_not_request(
    tmp_path, monkeypatch
):
    checkpoint = tmp_path / "checkpoint.json"
    result, calls = _call_queue(monkeypatch, checkpoint, [Response(_page(_ad("ad-1")))])
    assert result["advertisers"][0]["status"] == "exhausted"
    assert len(calls) == 1

    calls = []
    monkeypatch.setattr(
        fetch_ad_library, "guarded_request", lambda *a, **k: calls.append(k)
    )
    resumed = fetch_ad_library.collect_advertiser_queue(
        token="secret-token",
        countries=["DE"],
        search_page_ids="page-1",
        checkpoint_path=checkpoint,
        resume=True,
        quota_budget=RecordingBudget(),
    )
    assert resumed["advertisers"][0]["status"] == "exhausted"
    assert calls == []


def test_page_cap_is_paginated_not_exhausted(tmp_path, monkeypatch):
    checkpoint = tmp_path / "checkpoint.json"
    next_url = f"{fetch_ad_library.ENDPOINT}?after=cursor-2"
    result, _ = _call_queue(
        monkeypatch,
        checkpoint,
        [Response(_page(_ad("ad-1"), next_url=next_url))],
        max_pages=1,
    )
    assert result["advertisers"][0]["status"] == "paginated"


def test_quota_stop_persists_accepted_page_and_cursor(tmp_path, monkeypatch):
    checkpoint = tmp_path / "checkpoint.json"
    next_url = f"{fetch_ad_library.ENDPOINT}?after=cursor-3"
    budget = RecordingBudget(stop_after=1)
    result, calls = _call_queue(
        monkeypatch,
        checkpoint,
        [Response(_page(_ad("ad-1"), next_url=next_url))],
        quota_budget=budget,
    )
    assert result["advertisers"][0]["status"] == "quota-deferred"
    assert len(calls) == 1
    state = json.loads(checkpoint.read_text())
    assert state["advertisers"][0]["next_cursor"] == "cursor-3"
    assert state["advertisers"][0]["artifact"]["observation_count"] == 1


def test_advertisers_are_collected_sequentially(tmp_path, monkeypatch):
    checkpoint = tmp_path / "checkpoint.json"
    calls = []
    responses = {
        "page-1": [Response(_page(_ad("ad-1", "page-1")))],
        "page-2": [Response(_page(_ad("ad-2", "page-2")))],
    }

    def fake_guarded_request(session, method, url, **kwargs):
        page_id = kwargs["params"]["search_page_ids"]
        calls.append(page_id)
        return responses[page_id].pop(0)

    monkeypatch.setattr(fetch_ad_library, "guarded_request", fake_guarded_request)
    result = fetch_ad_library.collect_advertiser_queue(
        token="secret-token",
        countries=["DE"],
        search_page_ids="page-1,page-2",
        checkpoint_path=checkpoint,
        quota_budget=RecordingBudget(),
    )
    assert calls == ["page-1", "page-2"]
    assert [entry["advertiser_id"] for entry in result["advertisers"]] == [
        "page-1",
        "page-2",
    ]


def test_corrupt_checkpoint_fails_closed_before_network(tmp_path, monkeypatch):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text("not-json", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        fetch_ad_library, "guarded_request", lambda *a, **k: calls.append(k)
    )
    with pytest.raises(ValueError, match="corrupt checkpoint"):
        fetch_ad_library.collect_advertiser_queue(
            token="secret-token",
            countries=["DE"],
            search_page_ids="page-1",
            checkpoint_path=checkpoint,
            resume=True,
            quota_budget=RecordingBudget(),
        )
    assert calls == []


@pytest.mark.parametrize(
    "cursor",
    [
        "https://graph.facebook.com:443/v26.0/ads_archive?after=x",
        "https://user:pass@graph.facebook.com/v26.0/ads_archive?after=x",
        "https://graph.facebook.com/v26.0/other?after=x",
    ],
)
def test_cursor_requires_exact_graph_origin_and_path(cursor):
    with pytest.raises(ValueError):
        fetch_ad_library._validate_next_url(cursor)


def test_queue_page_cap_finishes_current_advertiser_before_next(tmp_path, monkeypatch):
    calls = []

    def transport(*args, **kwargs):
        calls.append(kwargs["params"]["search_page_ids"])
        return Response(
            _page(
                _ad("ad-1"),
                next_url=f"{fetch_ad_library.ENDPOINT}?after=continue",
            )
        )

    monkeypatch.setattr(fetch_ad_library, "guarded_request", transport)
    result = fetch_ad_library.collect_advertiser_queue(
        token="fixture",
        countries=["DE"],
        search_page_ids="page-1,page-2",
        checkpoint_path=tmp_path / "checkpoint.json",
        max_pages=1,
        quota_budget=RecordingBudget(),
    )
    assert calls == ["page-1"]
    assert [entry["status"] for entry in result["advertisers"]] == [
        "paginated",
        "queued",
    ]


def test_cursor_loop_failure_is_not_replayed_by_resume(tmp_path, monkeypatch):
    checkpoint = tmp_path / "checkpoint.json"
    looping = Response(
        _page(_ad("ad-1"), next_url=f"{fetch_ad_library.ENDPOINT}?after=loop")
    )
    _call_queue(monkeypatch, checkpoint, [looping], max_pages=1)
    failed, _ = _call_queue(monkeypatch, checkpoint, [looping], resume=True)
    assert failed["advertisers"][0]["status"] == "failed"
    resumed, calls = _call_queue(monkeypatch, checkpoint, [], resume=True)
    assert resumed["advertisers"][0]["status"] == "failed"
    assert calls == []


def test_cli_resume_reuses_default_run_identity_across_days(tmp_path, monkeypatch):
    from datetime import date

    class Day(date):
        day_number = 8

        @classmethod
        def today(cls):
            return cls(2026, 9, cls.day_number)

    checkpoint = tmp_path / "checkpoint.json"
    calls = []

    def transport(*args, **kwargs):
        calls.append(kwargs)
        return Response(_page(_ad("ad-1")))

    monkeypatch.setattr(fetch_ad_library, "date", Day)
    monkeypatch.setattr(fetch_ad_library, "guarded_request", transport)
    monkeypatch.setattr(
        fetch_ad_library, "QuotaBudget", lambda **kwargs: RecordingBudget()
    )
    monkeypatch.setenv("META_AD_LIBRARY_TOKEN", "fixture")
    monkeypatch.setenv("CLAUDE_ADS_OUTPUT_ROOT", str(tmp_path))
    argv = [
        "fetch_ad_library.py",
        "--countries",
        "DE",
        "--search-page-ids",
        "page-1",
        "--checkpoint",
        str(checkpoint),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    fetch_ad_library.main()
    original_id = json.loads(checkpoint.read_text())["filters"]["run_id"]
    Day.day_number = 9
    monkeypatch.setattr(sys, "argv", argv + ["--resume"])
    fetch_ad_library.main()
    assert json.loads(checkpoint.read_text())["filters"]["run_id"] == original_id
    assert len(calls) == 1


def test_resume_rejects_changed_effective_page_size(tmp_path, monkeypatch):
    checkpoint = tmp_path / "checkpoint.json"
    _call_queue(monkeypatch, checkpoint, [Response(_page(_ad("ad-1")))], limit=25)
    with pytest.raises(ValueError, match="mismatch"):
        _call_queue(monkeypatch, checkpoint, [], resume=True, limit=100)


def test_cli_corrupt_checkpoint_is_sanitized_error_without_dispatch(
    tmp_path, monkeypatch, capsys
):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text("invalid-json")
    monkeypatch.setenv("META_AD_LIBRARY_TOKEN", "fixture")
    monkeypatch.setenv("CLAUDE_ADS_OUTPUT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        fetch_ad_library,
        "guarded_request",
        lambda *a, **kw: pytest.fail("unexpected dispatch"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fetch_ad_library.py",
            "--countries",
            "DE",
            "--search-page-ids",
            "page-1",
            "--checkpoint",
            str(checkpoint),
            "--resume",
        ],
    )
    with pytest.raises(SystemExit) as caught:
        fetch_ad_library.main()
    assert caught.value.code == 1
    assert "Error:" in capsys.readouterr().err


def test_checkpoint_creation_rechecks_existence_after_lock(tmp_path, monkeypatch):
    checkpoint = tmp_path / "checkpoint.json"

    @contextmanager
    def competing_writer(path):
        checkpoint.write_text("completed by first writer")
        yield

    monkeypatch.setattr(fetch_ad_library, "_checkpoint_lock", competing_writer)
    with pytest.raises(ValueError, match="already exists"):
        _call_queue(monkeypatch, checkpoint, [])
    assert checkpoint.read_text() == "completed by first writer"


def test_final_page_quota_stop_defers_remaining_advertisers(tmp_path, monkeypatch):
    from ad_library_quota import QuotaBudget

    calls = []

    def transport(*args, **kwargs):
        calls.append(kwargs)
        return Response(
            _page(_ad("ad-1")), headers={"X-App-Usage": '{"call_count": 50}'}
        )

    monkeypatch.setattr(fetch_ad_library, "guarded_request", transport)
    result = fetch_ad_library.collect_advertiser_queue(
        token="fixture",
        countries=["DE"],
        search_page_ids="page-1,page-2",
        checkpoint_path=tmp_path / "checkpoint.json",
        quota_budget=QuotaBudget(tmp_path / "quota.json", clock=lambda: 1000),
    )
    assert len(calls) == 1
    assert [entry["status"] for entry in result["advertisers"]] == [
        "exhausted",
        "queued",
    ]
    assert result["status"] == "quota-deferred"
