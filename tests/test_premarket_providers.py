from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from scripts.collect_premarket_evidence import main as collector_main
from src.premarket.providers.artifacts import RawEvidenceStore
from src.premarket.providers.collector import (
    CollectionContext,
    build_provider_bundle,
)
from src.premarket.providers.config import ProviderConfig
from src.premarket.providers.fred import FredProvider
from src.premarket.providers.http import BoundedHttpClient, HttpPolicy
from src.premarket.providers.marketaux import MarketauxProvider, QuotaLedger
from src.premarket.providers.schedule_gate import validate_collection_window
from src.premarket.providers.sec_edgar import SecEdgarProvider

AS_OF = "2026-09-09T12:00:00Z"
FIXED_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _config() -> ProviderConfig:
    return ProviderConfig.from_mapping({
        "schema": "premarket-provider-config/v1",
        "http": {
            "allowed_hosts": [
                "api.marketaux.com",
                "data.sec.gov",
                "api.stlouisfed.org",
            ],
            "timeout_seconds": 5,
            "max_response_bytes": 50_000,
            "max_retries": 1,
            "retry_backoff_seconds": 0,
        },
        "marketaux": {
            "enabled": True,
            "base_url": "https://api.marketaux.com/v1/news/all",
            "daily_limit": 60,
            "lookback_minutes": 180,
            "max_articles": 3,
        },
        "sec_edgar": {
            "enabled": True,
            "base_url": "https://data.sec.gov/submissions",
            "requests_per_second": 2,
            "forms": ["8-K", "10-Q", "10-K"],
            "symbol_ciks": {"AAPL": "0000320193"},
        },
        "fred": {
            "enabled": True,
            "base_url": "https://api.stlouisfed.org/fred/series/observations",
            "series": [
                {"series_id": "VIXCLS", "label": "VIX close"},
                {"series_id": "DGS10", "label": "10Y Treasury"},
            ],
        },
    })


def _context() -> CollectionContext:
    return CollectionContext(
        trading_day=date(2026, 9, 9),
        as_of=AS_OF,
        symbols=("AAPL", "MSFT"),
    )


def _http(handler, tmp_path):
    transport = httpx.MockTransport(handler)
    raw_store = RawEvidenceStore(tmp_path / "raw")
    policy = HttpPolicy(
        allowed_hosts=("api.marketaux.com", "data.sec.gov", "api.stlouisfed.org"),
        timeout_seconds=5,
        max_response_bytes=50_000,
        max_retries=1,
        retry_backoff_seconds=0,
    )
    return BoundedHttpClient(
        policy,
        transport=transport,
        sleep=lambda _seconds: None,
        clock=lambda: FIXED_NOW,
    ), raw_store


def _quota(path: Path) -> QuotaLedger:
    return QuotaLedger(path, clock=lambda: FIXED_NOW)


def test_bounded_http_rejects_disallowed_hosts_and_redacts_credentials(tmp_path):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    client, raw_store = _http(handler, tmp_path)
    with pytest.raises(ValueError, match="host not allowed"):
        client.get_json(
            "https://example.com/data",
            params={"api_token": "secret"},
            provider="test",
            resource="bad-host",
            raw_store=raw_store,
        )
    assert calls == 0

    payload, receipt = client.get_json(
        "https://api.marketaux.com/v1/news/all",
        params={"api_token": "secret", "symbols": "AAPL"},
        provider="marketaux",
        resource="news",
        raw_store=raw_store,
    )
    assert payload == {"ok": True}
    assert "secret" not in json.dumps(receipt)
    assert "api_token" not in receipt["request_url"]
    assert raw_store.read_body(receipt["raw_rel_path"]) == b'{"ok":true}'


def test_bounded_http_retries_transient_status_and_caps_response_size(tmp_path):
    attempts = 0
    sleeps = []

    def handler(_request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"retry": True})
        return httpx.Response(200, content=b"x" * 101)

    raw_store = RawEvidenceStore(tmp_path / "raw")
    client = BoundedHttpClient(
        HttpPolicy(
            allowed_hosts=("api.marketaux.com",),
            timeout_seconds=5,
            max_response_bytes=100,
            max_retries=1,
            retry_backoff_seconds=0.25,
        ),
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    with pytest.raises(RuntimeError, match="HTTP_RESPONSE_TOO_LARGE"):
        client.get_json(
            "https://api.marketaux.com/v1/news/all",
            params=None,
            provider="marketaux",
            resource="news",
            raw_store=raw_store,
        )
    assert attempts == 2
    assert sleeps == [0.25]
    assert raw_store.manifest()["content_file_count"] == 1


def test_bounded_http_rejects_redirect_json_without_following_it(tmp_path):
    def handler(_request):
        return httpx.Response(
            302,
            headers={"location": "https://example.com/redirected"},
            json={"data": []},
        )

    client, raw_store = _http(handler, tmp_path)
    with pytest.raises(RuntimeError, match="HTTP_STATUS_302"):
        client.get_json(
            "https://api.marketaux.com/v1/news/all",
            params=None,
            provider="marketaux",
            resource="news",
            raw_store=raw_store,
        )

    manifest = raw_store.manifest()
    assert manifest["content_file_count"] == 1
    assert manifest["receipt_file_count"] == 1


def test_missing_credentials_make_zero_network_calls_and_fail_closed(tmp_path):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client, raw_store = _http(handler, tmp_path)
    config = _config()
    providers = [
        MarketauxProvider(config.marketaux, client, _quota(tmp_path / "quota"), token=""),
        SecEdgarProvider(config.sec_edgar, client, user_agent=""),
        FredProvider(config.fred, client, api_key=""),
    ]
    bundle = build_provider_bundle(_context(), providers, raw_store)

    assert calls == 0
    assert {item["status"] for item in bundle["providers"]} == {"UNAVAILABLE"}
    assert bundle["collection_status"] == "FAIL_CLOSED"
    assert bundle["capabilities"]["order_authority"] is False
    assert bundle["capabilities"]["candidate_scoring"] is False


def test_malformed_provider_result_is_converted_to_fail_closed_error(tmp_path):
    class MalformedProvider:
        provider = "malformed"
        kind = "NEWS"

        def collect(self, _context, _raw_store):
            return {"status": "OK"}

    bundle = build_provider_bundle(
        _context(),
        [MalformedProvider()],
        RawEvidenceStore(tmp_path / "raw"),
    )
    assert bundle["collection_status"] == "FAIL_CLOSED"
    assert bundle["providers"][0]["status"] == "ERROR"
    assert bundle["providers"][0]["error_code"] == "INVALID_PROVIDER_RESULT"


def test_marketaux_parses_sanitized_news_and_enforces_hard_quota(tmp_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        assert request.url.params["api_token"] == "token-value"
        assert not request.url.params["published_after"].endswith("Z")
        assert not request.url.params["published_before"].endswith("Z")
        assert request.url.params["limit"] == "3"
        return httpx.Response(200, json={
            "meta": {"found": 1, "returned": 1, "limit": 3, "page": 1},
            "data": [{
                "uuid": "news-1",
                "title": "AAPL announces a product",
                "description": "must not enter sanitized artifact",
                "snippet": "must not enter sanitized artifact",
                "url": "https://publisher.example/aapl",
                "published_at": "2026-09-09T11:30:00.000000Z",
                "source": "publisher.example",
                "entities": [{
                    "symbol": "AAPL",
                    "sentiment_score": 0.75,
                }],
            }],
        })

    client, raw_store = _http(handler, tmp_path)
    config = _config().marketaux
    quota = _quota(tmp_path / "quota")
    provider = MarketauxProvider(config, client, quota, token="token-value")
    result = provider.collect(_context(), raw_store)

    assert calls == 1
    assert result["status"] == "OK"
    assert result["items"][0]["symbols"] == ["AAPL"]
    serialized = json.dumps(result)
    assert "description" not in serialized
    assert "snippet" not in serialized
    assert "token-value" not in serialized

    quota_path = tmp_path / "quota" / "marketaux-2026-09-09.json"
    quota_path.write_text(json.dumps({"schema": "marketaux-quota/v1", "day": "2026-09-09", "used": 60}), encoding="utf-8")
    blocked = provider.collect(_context(), raw_store)
    assert blocked["status"] == "ERROR"
    assert blocked["error_code"] == "MARKETAUX_INCOMPLETE_QUOTA"
    assert calls == 1


@pytest.mark.parametrize(
    ("published_at", "expected_code"),
    [
        ("not-a-time", "MARKETAUX_PUBLISHED_AT_INVALID"),
        ("2026-09-09T12:00:01Z", "MARKETAUX_FUTURE_ARTICLE"),
    ],
)
def test_marketaux_fails_closed_on_invalid_or_future_article_time(
    tmp_path, published_at, expected_code
):
    def handler(_request):
        return httpx.Response(200, json={
            "meta": {"found": 1, "returned": 1, "limit": 3, "page": 1},
            "data": [{
                "uuid": "bad-time",
                "title": "bad time",
                "url": "https://publisher.example/bad-time",
                "published_at": published_at,
                "source": "publisher.example",
                "entities": [{"symbol": "AAPL", "sentiment_score": 0.1}],
            }],
        })

    client, raw_store = _http(handler, tmp_path)
    provider = MarketauxProvider(
        _config().marketaux,
        client,
        _quota(tmp_path / "quota"),
        token="token-value",
    )
    result = provider.collect(_context(), raw_store)
    assert result["status"] == "ERROR"
    assert result["error_code"] == expected_code
    assert result["items"] == []


def test_sec_filters_forms_and_uses_identifiable_user_agent(tmp_path):
    seen_headers = []
    sleeps = []

    def handler(request):
        seen_headers.append(request.headers["User-Agent"])
        return httpx.Response(200, json={
            "cik": "0000320193",
            "filings": {"recent": {
                "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002"],
                "filingDate": ["2026-09-08", "2026-09-08"],
                "reportDate": ["2026-09-08", "2026-06-30"],
                "acceptanceDateTime": ["2026-09-08T18:00:00.000Z", "2026-09-08T19:00:00.000Z"],
                "form": ["8-K", "4"],
                "primaryDocument": ["aapl-8k.htm", "ownership.xml"],
            }},
        })

    client, raw_store = _http(handler, tmp_path)
    provider = SecEdgarProvider(
        _config().sec_edgar,
        client,
        user_agent="AI Trading Research contact@example.com",
        sleep=sleeps.append,
    )
    result = provider.collect(_context(), raw_store)

    assert seen_headers == ["AI Trading Research contact@example.com"]
    assert [item["form"] for item in result["items"]] == ["8-K"]
    assert result["items"][0]["symbols"] == ["AAPL"]
    assert result["unmapped_symbols"] == ["MSFT"]
    assert sleeps == []


def test_sec_fails_closed_when_recent_arrays_have_different_lengths(tmp_path):
    def handler(_request):
        return httpx.Response(200, json={
            "filings": {"recent": {
                "accessionNumber": ["0000320193-26-000001"],
                "filingDate": [],
                "reportDate": ["2026-09-08"],
                "acceptanceDateTime": ["2026-09-08T18:00:00.000Z"],
                "form": ["8-K"],
                "primaryDocument": ["aapl-8k.htm"],
            }},
        })

    client, raw_store = _http(handler, tmp_path)
    result = SecEdgarProvider(
        _config().sec_edgar,
        client,
        user_agent="AI Trading Research contact@example.com",
    ).collect(_context(), raw_store)
    assert result["status"] == "ERROR"
    assert result["error_code"] == "AAPL:SEC_RECENT_FIELD_LENGTH_MISMATCH"
    assert result["items"] == []


def test_fred_preserves_point_in_time_vintage_parameters(tmp_path):
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        if request.url.path.endswith("/releases/dates"):
            return httpx.Response(200, json={
                "count": 1,
                "offset": 0,
                "limit": 1000,
                "release_dates": [{
                    "release_id": 10,
                    "release_name": "Consumer Price Index",
                    "date": "2026-09-09",
                }],
            })
        series_id = request.url.params["series_id"]
        return httpx.Response(200, json={
            "realtime_start": "2026-09-09",
            "realtime_end": "2026-09-09",
            "observations": [{
                "realtime_start": "2026-09-09",
                "realtime_end": "9999-12-31",
                "date": "2026-09-08",
                "value": "15.2" if series_id == "VIXCLS" else "4.01",
            }],
        })

    client, raw_store = _http(handler, tmp_path)
    result = FredProvider(_config().fred, client, api_key="fred-key").collect(
        _context(), raw_store
    )

    assert result["status"] == "OK"
    assert {
        item["series_id"] for item in result["items"]
        if item["item_type"] == "MACRO"
    } == {"DGS10", "VIXCLS"}
    assert all(params["realtime_start"] == "2026-09-09" for params in seen)
    assert all(params["realtime_end"] == "2026-09-09" for params in seen)
    assert all(params["api_key"] == "fred-key" for params in seen)
    assert sum(item["item_type"] == "MACRO_RELEASE" for item in result["items"]) == 1
    assert "fred-key" not in json.dumps(result)


def test_bundle_is_deterministic_and_raw_manifest_is_content_addressed(tmp_path):
    def handler(request):
        if request.url.host == "api.marketaux.com":
            return httpx.Response(200, json={
                "meta": {"found": 0, "returned": 0, "limit": 3, "page": 1},
                "data": [],
            })
        if request.url.host == "data.sec.gov":
            return httpx.Response(200, json={"filings": {"recent": {
                "accessionNumber": [], "filingDate": [], "reportDate": [],
                "acceptanceDateTime": [], "form": [], "primaryDocument": [],
            }}})
        if request.url.path.endswith("/releases/dates"):
            return httpx.Response(200, json={
                "count": 0, "offset": 0, "limit": 1000, "release_dates": [],
            })
        return httpx.Response(200, json={"observations": []})

    client, raw_store = _http(handler, tmp_path)
    config = _config()
    providers = [
        MarketauxProvider(config.marketaux, client, _quota(tmp_path / "quota"), token="token"),
        SecEdgarProvider(config.sec_edgar, client, user_agent="Research contact@example.com"),
        FredProvider(config.fred, client, api_key="fred"),
    ]
    first = build_provider_bundle(_context(), providers, raw_store)
    second = build_provider_bundle(_context(), providers, raw_store)

    assert first == second
    assert first["schema"] == "premarket-provider-bundle/v1"
    assert first["stage"] == "PRIMARY"
    assert first["run_id"].startswith("premarket-evidence-2026-09-09-primary-")
    assert first["artifact_sha256"] == second["artifact_sha256"]
    assert first["raw_manifest"]["content_file_count"] == 5
    assert first["raw_manifest"]["receipt_file_count"] == 5


def test_raw_manifest_contains_only_receipts_emitted_by_current_store(tmp_path):
    first = RawEvidenceStore(tmp_path / "raw")
    first.emit(
        provider="old",
        resource="old",
        requested_at=AS_OF,
        received_at=AS_OF,
        request_url="https://api.marketaux.com/old",
        status_code=200,
        content_type="application/json",
        body=b'{"old":true}',
    )
    current = RawEvidenceStore(tmp_path / "raw")
    current.emit(
        provider="new",
        resource="new",
        requested_at=AS_OF,
        received_at=AS_OF,
        request_url="https://api.marketaux.com/new",
        status_code=200,
        content_type="application/json",
        body=b'{"new":true}',
    )
    manifest = current.manifest()
    assert manifest["content_file_count"] == 1
    assert manifest["receipt_file_count"] == 1
    assert manifest["content_files"][0]["rel_path"].startswith("content/new/")


def test_collector_cli_without_credentials_makes_fail_closed_artifact(tmp_path, monkeypatch):
    output = tmp_path / "bundle.json"
    receipt = tmp_path / "receipt.json"
    args = [
        "--trading-day", "2026-09-09",
        "--stage", "PRIMARY",
        "--as-of", AS_OF,
        "--provider-config", str(ROOT / "config" / "premarket_providers.yaml"),
        "--brief-config", str(ROOT / "config" / "premarket_daily_brief.yaml"),
        "--raw-root", str(tmp_path / "raw"),
        "--quota-root", str(tmp_path / "quota"),
        "--out-json", str(output),
        "--receipt", str(receipt),
        "--no-dotenv",
    ]
    for key in (
        "MARKETAUX_API_KEY", "MARKETAUX_API_TOKEN",
        "SEC_USER_AGENT", "SEC_EDGAR_USER_AGENT", "FRED_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    assert collector_main(args, clock=lambda: FIXED_NOW) == 0
    before = output.read_bytes()
    assert collector_main(args, clock=lambda: FIXED_NOW) == 0

    assert output.read_bytes() == before
    assert receipt.exists()
    bundle = json.loads(before)
    assert bundle["collection_status"] == "FAIL_CLOSED"
    assert bundle["raw_manifest"]["content_file_count"] == 0
    assert {item["status"] for item in bundle["providers"]} == {"UNAVAILABLE"}


def test_http_receipt_uses_actual_clock_not_research_as_of(tmp_path):
    moments = iter([
        datetime(2026, 9, 10, 1, 2, 3, tzinfo=timezone.utc),
        datetime(2026, 9, 10, 1, 2, 4, tzinfo=timezone.utc),
    ])
    client = BoundedHttpClient(
        HttpPolicy(("api.marketaux.com",), 5, 50_000, 0, 0),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"ok": True})),
        clock=lambda: next(moments),
    )
    raw_store = RawEvidenceStore(tmp_path / "raw")
    _, receipt = client.get_json(
        "https://api.marketaux.com/v1/news/all",
        params=None,
        provider="marketaux",
        resource="news",
        raw_store=raw_store,
    )
    client.close()
    assert receipt["requested_at"] == "2026-09-10T01:02:03Z"
    assert receipt["received_at"] == "2026-09-10T01:02:04Z"
    assert receipt["received_at"] != AS_OF


def test_retry_after_is_honored_and_capped(tmp_path):
    attempts = 0
    sleeps: list[float] = []

    def handler(_request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "20"}, json={"retry": True})
        return httpx.Response(200, json={"ok": True})

    client = BoundedHttpClient(
        HttpPolicy(("api.marketaux.com",), 5, 50_000, 1, 0.25, 5),
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        clock=lambda: FIXED_NOW,
    )
    raw_store = RawEvidenceStore(tmp_path / "raw")
    client.get_json(
        "https://api.marketaux.com/v1/news/all",
        params=None,
        provider="marketaux",
        resource="news",
        raw_store=raw_store,
    )
    client.close()
    assert sleeps == [5.0]
    receipt_paths = raw_store.manifest()["receipt_files"]
    receipts = [
        json.loads((raw_store.root / entry["rel_path"]).read_text(encoding="utf-8"))
        for entry in receipt_paths
    ]
    retry_receipt = next(item for item in receipts if item["status_code"] == 429)
    assert retry_receipt["retry_delay_seconds"] == 5.0


def test_quota_uses_actual_utc_day_across_research_trading_days(tmp_path):
    quota = _quota(tmp_path / "quota")
    assert all(quota.reserve(limit=60) for _ in range(60))
    assert quota.reserve(limit=60) is False
    assert [path.name for path in (tmp_path / "quota").glob("*.json")] == [
        "marketaux-2026-09-09.json"
    ]


def _news_row(uuid: str, title: str) -> dict:
    return {
        "uuid": uuid,
        "title": title,
        "url": f"https://publisher.example/{uuid}",
        "published_at": "2026-09-09T11:30:00Z",
        "source": "publisher.example",
        "entities": [{"symbol": "AAPL", "sentiment_score": 0.2}],
    }


def test_marketaux_paginates_and_deduplicates_identical_article(tmp_path):
    calls: list[int] = []
    duplicate = _news_row("same", "same title")

    def handler(request):
        page = int(request.url.params["page"])
        calls.append(page)
        if page == 1:
            rows = [_news_row("a", "a"), _news_row("b", "b"), duplicate]
        else:
            rows = [duplicate, _news_row("d", "d")]
        return httpx.Response(200, json={
            "meta": {"found": 4, "returned": len(rows), "limit": 3, "page": page},
            "data": rows,
        })

    client, raw_store = _http(handler, tmp_path)
    result = MarketauxProvider(
        _config().marketaux,
        client,
        _quota(tmp_path / "quota"),
        token="token",
    ).collect(_context(), raw_store)
    client.close()
    assert result["status"] == "OK"
    assert calls == [1, 2]
    assert [item["item_id"] for item in result["items"]] == [
        "marketaux:a", "marketaux:b", "marketaux:d", "marketaux:same"
    ]


def test_marketaux_same_id_changed_content_is_conflict(tmp_path):
    def handler(request):
        page = int(request.url.params["page"])
        rows = (
            [_news_row("same", "first"), _news_row("b", "b"), _news_row("c", "c")]
            if page == 1 else
            [_news_row("same", "changed"), _news_row("d", "d")]
        )
        return httpx.Response(200, json={
            "meta": {"found": 4, "returned": len(rows), "limit": 3, "page": page},
            "data": rows,
        })

    client, raw_store = _http(handler, tmp_path)
    result = MarketauxProvider(
        _config().marketaux,
        client,
        _quota(tmp_path / "quota"),
        token="token",
    ).collect(_context(), raw_store)
    client.close()
    assert result["status"] == "ERROR"
    assert "MARKETAUX_ARTICLE_CONFLICT" in result["error_code"]


def test_marketaux_page_cap_cannot_report_incomplete_result_ok(tmp_path):
    def handler(_request):
        rows = [_news_row("a", "a"), _news_row("b", "b"), _news_row("c", "c")]
        return httpx.Response(200, json={
            "meta": {"found": 10, "returned": 3, "limit": 3, "page": 1},
            "data": rows,
        })

    client, raw_store = _http(handler, tmp_path)
    config = replace(_config().marketaux, max_pages_by_stage={
        "PRIMARY": 1, "VERIFICATION": 1, "FREEZE": 1,
    })
    result = MarketauxProvider(
        config,
        client,
        _quota(tmp_path / "quota"),
        token="token",
    ).collect(_context(), raw_store)
    client.close()
    assert result["status"] == "ERROR"
    assert result["error_code"] == "MARKETAUX_INCOMPLETE_STAGE_PAGE_CAP"


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (None, "FRED_OBSERVATION_MISSING"),
        ({"date": "2026-08-01", "value": "4.0"}, "FRED_OBSERVATION_STALE"),
        ({"date": "2026-09-08", "value": "NaN"}, "FRED_OBSERVATION_INVALID"),
    ],
)
def test_fred_required_series_missing_stale_or_invalid_is_error(
    tmp_path, observation, expected
):
    def handler(request):
        if request.url.path.endswith("/releases/dates"):
            return httpx.Response(200, json={
                "count": 0, "offset": 0, "limit": 1000, "release_dates": [],
            })
        rows = [] if observation is None else [observation]
        return httpx.Response(200, json={"observations": rows})

    client, raw_store = _http(handler, tmp_path)
    result = FredProvider(_config().fred, client, api_key="fred").collect(
        _context(), raw_store
    )
    client.close()
    assert result["status"] == "ERROR"
    assert expected in result["error_code"]


def test_schedule_gate_rejects_holiday_wrong_cutoff_and_late_execution():
    with pytest.raises(ValueError, match="not an NYSE trading day"):
        validate_collection_window(
            date(2026, 9, 7),
            "PRIMARY",
            as_of=None,
            now=FIXED_NOW,
            dry_run=False,
        )
    with pytest.raises(ValueError, match="must equal"):
        validate_collection_window(
            date(2026, 9, 9),
            "PRIMARY",
            as_of="2026-09-09T12:00:01Z",
            now=FIXED_NOW,
            dry_run=False,
        )
    with pytest.raises(ValueError, match="outside"):
        validate_collection_window(
            date(2026, 9, 9),
            "PRIMARY",
            as_of=AS_OF,
            now=datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc),
            dry_run=False,
        )


def test_cli_dry_run_validates_without_network_or_artifacts(tmp_path, monkeypatch):
    def forbidden_client(*_args, **_kwargs):
        raise AssertionError("dry-run must not construct an HTTP client")

    monkeypatch.setattr(
        "scripts.collect_premarket_evidence.BoundedHttpClient",
        forbidden_client,
    )
    output = tmp_path / "never-created.json"
    assert collector_main([
        "--trading-day", "2026-09-09",
        "--stage", "PRIMARY",
        "--provider", "marketaux,fred",
        "--out-json", str(output),
        "--dry-run",
        "--no-dotenv",
    ], clock=lambda: FIXED_NOW) == 0
    assert not output.exists()


def test_bundle_contains_no_candidates_regime_or_order_payload(tmp_path):
    bundle = build_provider_bundle(
        _context(),
        [],
        RawEvidenceStore(tmp_path / "raw"),
    )
    assert "candidates" not in bundle
    assert "market_regime" not in bundle
    assert "orders" not in bundle
    assert all(value is False for value in bundle["capabilities"].values())


def test_provider_collection_layer_is_statically_isolated_from_execution_paths():
    paths = list((ROOT / "src" / "premarket" / "providers").glob("*.py")) + [
        ROOT / "scripts" / "collect_premarket_evidence.py"
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    forbidden = (
        "src.order",
        "src.live",
        "src.broker",
        "src.runtime.paper",
        "submit_order",
        "place_order",
        "PaperBroker",
    )
    assert {token for token in forbidden if token in source} == set()
