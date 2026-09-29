"""Collection exhaustion and permission to dispatch again are independent."""

from contextlib import contextmanager
import json
from pathlib import Path
import sys

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import fetch_ad_library as collector  # noqa: E402


class Response:
    status_code = 200
    headers = {}

    def __init__(self, *, cursor=None, empty=False):
        self.payload = {
            "data": [] if empty else [{"id": "fixture-ad", "page_id": "page-1"}],
            "paging": {} if cursor is None else {"next": f"{collector.ENDPOINT}?after={cursor}"},
        }

    def json(self):
        return self.payload


class Budget:
    def __init__(self, stop_reason=None, defer=False):
        self.stop_reason = stop_reason
        self.defer = defer

    @contextmanager
    def attempt(self):
        if self.defer:
            raise collector.QuotaDeferred("hourly-cap", 1234.0)
        yield

    def observe(self, headers, *, status_code, error_code=None):
        return {
            "usage": None,
            "stop_reason": self.stop_reason,
            "retry_at": 1234.0 if self.stop_reason else None,
        }


def _network(monkeypatch, response):
    calls = []

    def dispatch(session, method, url, **kwargs):
        calls.append(kwargs["params"].copy())
        return response

    monkeypatch.setattr(collector, "guarded_request", dispatch)
    return calls


def _queue(path, **overrides):
    kwargs = {
        "token": "fixture-token",
        "countries": ["DE"],
        "search_page_ids": "page-1",
        "checkpoint_path": path,
        "quota_budget": Budget("usage-threshold"),
        "sleep": lambda seconds: None,
    }
    kwargs.update(overrides)
    return collector.collect_advertiser_queue(**kwargs)


@pytest.mark.parametrize("reason", ["usage-threshold", "usage-unavailable", "usage-invalid"])
@pytest.mark.parametrize("empty", [False, True])
def test_accepted_terminal_page_remains_exhausted_when_quota_stops(reason, empty, monkeypatch):
    calls = _network(monkeypatch, Response(empty=empty))
    result = collector.search_ad_library(
        token="fixture-token", countries=["DE"], search_page_ids="page-1",
        quota_budget=Budget(reason), sleep=lambda seconds: None,
    )
    assert result["status"] == "exhausted"
    assert result["pages_fetched"] == 1
    assert result["next_cursor"] is None
    assert result["quota_stop_reason"] == reason
    assert result["retry_at"] == 1234.0
    assert len(calls) == 1


def test_terminal_checkpoint_resume_never_reopens_completed_advertiser(tmp_path, monkeypatch):
    calls = _network(monkeypatch, Response())
    path = tmp_path / "checkpoint.json"
    first = _queue(path)
    assert first["status"] == "exhausted"
    entry = json.loads(path.read_text())["advertisers"][0]
    assert entry["status"] == "exhausted"
    assert entry["artifact"]["collection"]["status"] == "exhausted"
    saved = path.read_bytes()
    resumed = _queue(path, resume=True)
    assert resumed["status"] == "exhausted"
    assert len(calls) == 1
    assert path.read_bytes() == saved


def test_terminal_page_defers_next_advertiser_and_resume_starts_that_advertiser(tmp_path, monkeypatch):
    calls = _network(monkeypatch, Response())
    path = tmp_path / "checkpoint.json"
    first = _queue(path, search_page_ids="page-1,page-2")
    assert first["status"] == "quota-deferred"
    assert [entry["status"] for entry in first["advertisers"]] == ["exhausted", "queued"]
    assert [call["search_page_ids"] for call in calls] == ["page-1"]
    resumed = _queue(path, search_page_ids="page-1,page-2", resume=True, quota_budget=Budget())
    assert resumed["status"] == "exhausted"
    assert [call["search_page_ids"] for call in calls] == ["page-1", "page-2"]


def test_nonterminal_quota_stop_keeps_the_exact_resume_cursor(tmp_path, monkeypatch):
    calls = _network(monkeypatch, Response(cursor="fixture-cursor"))
    path = tmp_path / "checkpoint.json"
    first = _queue(path)
    assert first["status"] == "quota-deferred"
    assert first["advertisers"][0]["next_cursor"] == "fixture-cursor"
    assert len(calls) == 1
    calls = _network(monkeypatch, Response())
    resumed = _queue(path, resume=True, quota_budget=Budget())
    assert resumed["status"] == "exhausted"
    assert calls[0]["after"] == "fixture-cursor"


def test_deferral_before_any_response_is_not_exhaustion(tmp_path, monkeypatch):
    calls = _network(monkeypatch, Response())
    path = tmp_path / "checkpoint.json"
    first = _queue(path, quota_budget=Budget(defer=True))
    assert first["status"] == "quota-deferred"
    assert first["advertisers"][0]["artifact"] is None
    assert calls == []
    assert _queue(path, resume=True, quota_budget=Budget())["status"] == "exhausted"
    assert len(calls) == 1
    assert "after" not in calls[0]


def test_malformed_terminal_response_is_not_exhaustion(monkeypatch):
    response = Response()
    response.payload["data"] = "not-a-list"
    _network(monkeypatch, response)
    result = collector.search_ad_library(
        token="fixture-token", countries=["DE"], search_page_ids="page-1",
        quota_budget=Budget("usage-threshold"),
    )
    assert result["status"] == "failed"
    assert result["pages_fetched"] == 0


def _cli(monkeypatch, response):
    _network(monkeypatch, response)
    monkeypatch.setenv("META_AD_LIBRARY_TOKEN", "fixture-token")
    monkeypatch.setattr(collector, "QuotaBudget", lambda **kwargs: Budget("usage-threshold"))
    monkeypatch.setattr(sys, "argv", ["fetch_ad_library.py", "--countries", "DE", "--search-terms", "fixture"])
    collector._main()


def test_nonqueue_cli_signals_deferred_work_even_without_an_error_string(monkeypatch, capsys):
    with pytest.raises(SystemExit) as error:
        _cli(monkeypatch, Response(cursor="fixture-cursor"))
    assert error.value.code == 1
    artifact = json.loads(capsys.readouterr().out)
    assert artifact["collection"]["status"] == "quota-deferred"
    assert artifact["observation_count"] == 1


def test_nonqueue_cli_succeeds_for_exhausted_collection_with_quota_stop(monkeypatch, capsys):
    _cli(monkeypatch, Response())
    artifact = json.loads(capsys.readouterr().out)
    assert artifact["collection"]["status"] == "exhausted"
    assert artifact["collection"]["quota_stop_reason"] == "usage-threshold"


def test_terminal_page_without_quota_stop_remains_exhausted(monkeypatch):
    calls = _network(monkeypatch, Response())
    result = collector.search_ad_library(
        token="fixture-token", countries=["DE"], search_page_ids="page-1",
        quota_budget=Budget(),
    )
    assert result["status"] == "exhausted"
    assert result["quota_stop_reason"] is None
    assert len(calls) == 1
