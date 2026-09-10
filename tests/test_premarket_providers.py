from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

from src.premarket.providers.artifacts import RawEvidenceStore
from src.premarket.providers.collector import (
    CollectionContext,
    build_provider_bundle,
)
from src.premarket.providers.config import ProviderConfig
from src.premarket.providers.fred import FredProvider
from src.premarket.providers.http import BoundedHttpClient, HttpPolicy
from src.premarket.providers.marketaux import MarketauxProvider, QuotaLedger
from src.premarket.providers.sec_edgar import SecEdgarProvider

AS_OF = "2026-09-09T12:00:00Z"
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
    return BoundedHttpClient(policy, transport=transport, sleep=lambda _seconds: None), raw_store


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
            collected_at=AS_OF,
            raw_store=raw_store,
        )
    assert calls == 0

    payload, receipt = client.get_json(
        "https://api.marketaux.com/v1/news/all",
        params={"api_token": "secret", "symbols": "AAPL"},
        provider="marketaux",
        resource="news",
        collected_at=AS_OF,
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
            collected_at=AS_OF,
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
            collected_at=AS_OF,
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
        MarketauxProvider(config.marketaux, client, QuotaLedger(tmp_path / "quota"), token=""),
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
    quota = QuotaLedger(tmp_path / "quota")
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
    assert blocked["status"] == "UNAVAILABLE"
    assert blocked["error_code"] == "MARKETAUX_DAILY_QUOTA_EXHAUSTED"
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
        QuotaLedger(tmp_path / "quota"),
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
    assert {item["series_id"] for item in result["items"]} == {"DGS10", "VIXCLS"}
    assert all(params["realtime_start"] == "2026-09-09" for params in seen)
    assert all(params["realtime_end"] == "2026-09-09" for params in seen)
    assert all(params["api_key"] == "fred-key" for params in seen)
    assert "fred-key" not in json.dumps(result)


def test_bundle_is_deterministic_and_raw_manifest_is_content_addressed(tmp_path):
    def handler(request):
        if request.url.host == "api.marketaux.com":
            return httpx.Response(200, json={"data": []})
        if request.url.host == "data.sec.gov":
            return httpx.Response(200, json={"filings": {"recent": {
                "accessionNumber": [], "filingDate": [], "reportDate": [],
                "acceptanceDateTime": [], "form": [], "primaryDocument": [],
            }}})
        return httpx.Response(200, json={"observations": []})

    client, raw_store = _http(handler, tmp_path)
    config = _config()
    providers = [
        MarketauxProvider(config.marketaux, client, QuotaLedger(tmp_path / "quota"), token="token"),
        SecEdgarProvider(config.sec_edgar, client, user_agent="Research contact@example.com"),
        FredProvider(config.fred, client, api_key="fred"),
    ]
    first = build_provider_bundle(_context(), providers, raw_store)
    second = build_provider_bundle(_context(), providers, raw_store)

    assert first == second
    assert first["schema"] == "premarket-provider-bundle/v1"
    assert first["artifact_sha256"] == second["artifact_sha256"]
    assert first["raw_manifest"]["content_file_count"] == 4
    assert first["raw_manifest"]["receipt_file_count"] == 4


def test_raw_manifest_contains_only_receipts_emitted_by_current_store(tmp_path):
    first = RawEvidenceStore(tmp_path / "raw")
    first.emit(
        provider="old",
        resource="old",
        collected_at=AS_OF,
        request_url="https://api.marketaux.com/old",
        status_code=200,
        content_type="application/json",
        body=b'{"old":true}',
    )
    current = RawEvidenceStore(tmp_path / "raw")
    current.emit(
        provider="new",
        resource="new",
        collected_at=AS_OF,
        request_url="https://api.marketaux.com/new",
        status_code=200,
        content_type="application/json",
        body=b'{"new":true}',
    )
    manifest = current.manifest()
    assert manifest["content_file_count"] == 1
    assert manifest["receipt_file_count"] == 1
    assert manifest["content_files"][0]["rel_path"].startswith("content/new/")


def test_collector_cli_without_credentials_makes_fail_closed_artifact(tmp_path):
    output = tmp_path / "bundle.json"
    command = [
        sys.executable,
        str(ROOT / "scripts" / "collect_premarket_evidence.py"),
        "--trading-day", "2026-09-09",
        "--as-of", AS_OF,
        "--provider-config", str(ROOT / "config" / "premarket_providers.yaml"),
        "--brief-config", str(ROOT / "config" / "premarket_daily_brief.yaml"),
        "--raw-root", str(tmp_path / "raw"),
        "--quota-root", str(tmp_path / "quota"),
        "--out-json", str(output),
        "--no-dotenv",
    ]
    env = dict(os.environ)
    for key in ("MARKETAUX_API_TOKEN", "SEC_EDGAR_USER_AGENT", "FRED_API_KEY"):
        env.pop(key, None)
    first = subprocess.run(command, cwd=ROOT, env=env, check=True, text=True, capture_output=True)
    before = output.read_bytes()
    second = subprocess.run(command, cwd=ROOT, env=env, check=True, text=True, capture_output=True)

    assert first.stdout == second.stdout
    assert output.read_bytes() == before
    bundle = json.loads(before)
    assert bundle["collection_status"] == "FAIL_CLOSED"
    assert bundle["raw_manifest"]["content_file_count"] == 0
    assert {item["status"] for item in bundle["providers"]} == {"UNAVAILABLE"}
