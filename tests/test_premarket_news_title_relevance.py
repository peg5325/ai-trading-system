"""v1.3 (2026-10-02): NEWS title relevance + Marketaux ops stability."""
from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml

from src.premarket.contract import PremarketConfig, load_config, sha256_payload
from src.premarket.normalizer import normalize_bundles
from src.premarket.planner import build_daily_brief
from src.premarket.providers.artifacts import RawEvidenceStore
from src.premarket.providers.collector import CollectionContext
from src.premarket.providers.config import HttpPolicy, load_provider_config
from src.premarket.providers.http import BoundedHttpClient, HttpCollectionError
from src.premarket.providers.marketaux import (
    SYMBOL_ALIASES,
    MarketauxProvider,
    QuotaLedger,
    name_core,
    title_mentions,
)
from src.premarket.schedule import build_premarket_schedule
from tests.test_premarket_normalization import _config, _market_bundle, _provider_bundle

ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)
URL = "https://api.marketaux.com/v1/news/all"


# --- matching rule -----------------------------------------------------------

@pytest.mark.parametrize(("name", "core"), [
    ("Apple Inc.", "apple"),
    ("Amazon.com, Inc.", "amazon"),
    ("Microsoft Corporation", "microsoft"),
    ("Micron Technology, Inc.", "micron technology"),
    ("Meta Platforms Inc", "meta platforms"),
    ("Vistra Corp.", "vistra"),
    ("SPDR S&P 500 ETF Trust", "spdr s p 500"),
    ("Invesco QQQ Trust", "invesco qqq"),
    ("The Walt Disney Company", "walt disney"),
    ("Alphabet Inc. Class A", "alphabet"),
    ("Holdings Inc.", ""),
    ("Technology Corp", ""),                 # single generic word -> ticker only
    ("", ""),
])
def test_name_core_strips_legal_and_share_class_suffixes(name, core):
    assert name_core(name) == core


@pytest.mark.parametrize(("title", "symbol", "names", "expected"), [
    ("AAPL announces a product", "AAPL", [], True),
    ("Buy $NVDA before earnings", "NVDA", [], True),
    ("MU, AMD rally premarket", "MU", [], True),
    ("AMD-backed fund", "AMD", [], True),
    ("Mutual funds and community banks", "MU", [], False),   # no match inside words
    ("Ambitious AMDOCS plans", "AMD", [], False),
    ("METAVERSE stocks slide", "META", [], False),
    ("spy dips premarket", "SPY", [], False),                 # ticker is case-sensitive
    ("Microsoft's cloud grows", "MSFT", ["Microsoft Corporation"], True),
    ("MICROSOFT beats", "MSFT", ["Microsoft Corporation"], True),
    ("Microsofty memes", "MSFT", ["Microsoft Corporation"], False),
    ("Micron Technology raises guidance", "MU", ["Micron Technology, Inc."], True),
])
def test_title_mentions_ticker_or_name_core(title, symbol, names, expected):
    assert title_mentions(title, symbol, names) is expected


@pytest.mark.parametrize(("title", "symbol", "name", "expected"), [
    ("State Street Corp's Dividend Analysis", "SPY", "SPDR S&P 500 ETF Trust", False),
    ("What Invesco NASDAQ 100 ETF (QQQM) Bought: NVDA Leads on Sept. 29",
     "MSFT", "Microsoft Corporation", False),
    ("JPMorgan Nasdaq Equity Premium Income ETF (JEPQ) Adds NVDA and AAPL on Sept. 29",
     "MSFT", "Microsoft Corporation", False),
    ("iShares Russell Top 200 Value ETF (IWX) Sells Amazon and Apple on Sept. 29",
     "AMZN", "Amazon.com, Inc.", True),
])
def test_2026_10_01_freeze_titles(title, symbol, name, expected):
    assert title_mentions(title, symbol, [name]) is expected


@pytest.mark.parametrize(("title", "symbol", "expected"), [
    ("Meta shares jump after earnings", "META", True),
    ("Micron beats estimates", "MU", True),
    ("Google unveils new chip", "GOOGL", True),
    ("Metaverse stocks rally", "META", False),
    ("Apple and Microsoft lead gains", "AAPL", True),
    ("Apple and Microsoft lead gains", "MSFT", True),
    ("Facebook parent sued", "META", True),
    ("Palantir wins contract", "PLTR", True),
    ("JPMorgan Nasdaq Equity Premium Income ETF (JEPQ) Adds NVDA and AAPL on Sept. 29", "NVDA", True),
    ("JPMorgan Nasdaq Equity Premium Income ETF (JEPQ) Adds NVDA and AAPL on Sept. 29", "AAPL", True),
    ("S&P 500 slips", "SPY", False),          # ETFs have no aliases
])
def test_fixed_aliases_without_entity_names(title, symbol, expected):
    assert title_mentions(title, symbol) is expected


def test_aliases_only_for_stocks():
    etfs = {"SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLU",
            "SMH", "TQQQ", "SOXL"}
    assert etfs.isdisjoint(SYMBOL_ALIASES)


# --- provider parse ----------------------------------------------------------

def _policy(**overrides) -> HttpPolicy:
    return HttpPolicy(**{
        "allowed_hosts": ("api.marketaux.com",),
        "timeout_seconds": 10,
        "max_response_bytes": 50_000,
        "max_retries": 2,
        "retry_backoff_seconds": 0,
        **overrides,
    })


def _client(handler, **kwargs) -> BoundedHttpClient:
    return BoundedHttpClient(
        _policy(**kwargs),
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        clock=lambda: FIXED_NOW,
    )


def _marketaux_page(rows: list[dict]) -> dict:
    return {"meta": {"found": len(rows), "returned": len(rows), "limit": 3, "page": 1},
            "data": rows}


def test_provider_records_title_relevant_symbols_without_title_text(tmp_path):
    title = "JPMorgan Nasdaq Equity Premium Income ETF (JEPQ) Adds NVDA and AAPL on Sept. 29"

    def handler(_request):
        return httpx.Response(200, json=_marketaux_page([{
            "uuid": "jepq",
            "title": title,
            "url": "https://publisher.example/jepq",
            "published_at": "2026-10-01T12:00:00Z",
            "source": "publisher.example",
            "entities": [
                {"symbol": "AAPL", "name": "Apple Inc.", "sentiment_score": 0.2},
                {"symbol": "MSFT", "name": "Microsoft Corporation", "sentiment_score": 0.4},
            ],
        }]))

    config = load_provider_config(ROOT / "config" / "premarket_providers.yaml").marketaux
    provider = MarketauxProvider(config, _client(handler),
                                 QuotaLedger(tmp_path / "q", clock=lambda: FIXED_NOW),
                                 token="t")
    context = CollectionContext(trading_day=date(2026, 10, 1), as_of="2026-10-01T13:00:00Z",
                                symbols=("AAPL", "MSFT"), stage="FREEZE")
    result = provider.collect(context, RawEvidenceStore(tmp_path / "raw"))
    assert result["status"] == "OK"
    item = result["items"][0]
    assert item["symbols"] == ["AAPL", "MSFT"]
    assert item["title_relevant_symbols"] == ["AAPL"]
    assert title not in json.dumps(result)


# --- normalizer + policy flag ------------------------------------------------

def _bundle_with_relevance() -> dict:
    bundle = _provider_bundle()
    items = bundle["providers"][0]["items"]
    items[0]["title_relevant_symbols"] = ["AAPL"]   # news:aapl:1 relevant
    items[1]["title_relevant_symbols"] = []         # news:aapl:2 irrelevant
    items[2]["title_relevant_symbols"] = []         # news:msft:1 irrelevant (FILING remains)
    return bundle


def test_flag_absent_keeps_old_behaviour():
    old = normalize_bundles(_provider_bundle(), _market_bundle())
    assert normalize_bundles(_bundle_with_relevance(), _market_bundle())["events"] == old["events"]
    assert "news_title_relevance_required" not in old["provenance"]
    assert {e["event_id"] for e in old["events"]} >= {"news:aapl:2", "news:msft:1"}


def test_flag_excludes_irrelevant_news_from_catalyst_and_evidence():
    normalized = normalize_bundles(_bundle_with_relevance(), _market_bundle(),
                                   news_title_relevance_required=True)
    event_ids = {e["event_id"] for e in normalized["events"]}
    assert "news:aapl:1" in event_ids
    assert {"news:aapl:2", "news:msft:1"}.isdisjoint(event_ids)
    assert "sec:msft:recent" in event_ids                     # FILING unaffected
    by_symbol = {c["symbol"]: c for c in normalized["candidates"]}
    assert by_symbol["AAPL"]["catalyst_score"] == pytest.approx(0.5)
    assert by_symbol["AAPL"]["evidence_event_ids"] == ["news:aapl:1"]
    assert by_symbol["MSFT"]["catalyst_score"] == 0.0
    assert by_symbol["MSFT"]["evidence_event_ids"] == ["sec:msft:recent"]
    assert normalized["provenance"]["news_title_relevance_required"] is True


def test_flag_treats_items_without_relevance_field_as_irrelevant():
    normalized = normalize_bundles(_provider_bundle(), _market_bundle(),
                                   news_title_relevance_required=True)
    assert all(e["event_kind"] == "FILING" for e in normalized["events"])


def test_policy_config_records_flag_only_when_on():
    off = _config()
    assert "news_title_relevance_required" not in off.to_dict()
    on = dataclasses.replace(off, news_title_relevance_required=True)
    assert on.to_dict()["news_title_relevance_required"] is True

    repo = load_config(ROOT / "config" / "premarket_daily_brief.yaml")
    assert repo.news_title_relevance_required is True
    assert repo.to_dict()["news_title_relevance_required"] is True

    normalized = normalize_bundles(_bundle_with_relevance(), _market_bundle(),
                                   news_title_relevance_required=True)
    brief = build_daily_brief(normalized, schedule=build_premarket_schedule(date(2026, 3, 9)),
                              config=on)
    assert brief["snapshot"]["payload"]["policy_config"]["news_title_relevance_required"] is True
    assert brief["snapshot"]["snapshot_sha256"] == sha256_payload(brief["snapshot"]["payload"])


def test_flag_must_be_boolean():
    raw = yaml.safe_load((ROOT / "config" / "premarket_daily_brief.yaml").read_text())
    raw["news_title_relevance_required"] = "yes"
    with pytest.raises(TypeError, match="news_title_relevance_required"):
        PremarketConfig.from_mapping(raw)


# --- Marketaux ops stability -------------------------------------------------

def test_marketaux_timeout_is_30s_and_others_unchanged():
    config = load_provider_config(ROOT / "config" / "premarket_providers.yaml")
    assert config.marketaux.timeout_seconds == 30
    assert config.http.timeout_seconds == 10


def test_marketaux_request_uses_per_provider_timeout(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    client.get_json(URL, params=None, provider="marketaux", resource="r",
                    raw_store=RawEvidenceStore(tmp_path), timeout_seconds=30)
    client.get_json(URL, params=None, provider="other", resource="r",
                    raw_store=RawEvidenceStore(tmp_path))
    assert seen[0]["read"] == 30 and seen[0]["connect"] == 30
    assert seen[1]["read"] == 10


def test_timeout_retry_does_not_consume_quota_but_responses_do(tmp_path):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("slow", request=request)
        if attempts == 2:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"ok": True})

    quota = QuotaLedger(tmp_path / "q", clock=lambda: FIXED_NOW)
    client = _client(handler)
    payload, _ = client.get_json(URL, params=None, provider="marketaux", resource="r",
                                 raw_store=RawEvidenceStore(tmp_path / "raw"),
                                 before_attempt=lambda: quota.reserve(limit=60))
    assert payload == {"ok": True}
    assert attempts == 3
    ledger = json.loads((tmp_path / "q" / "marketaux-2026-10-01.json").read_text())
    assert ledger["used"] == 2          # timeout carried; 503 and 200 each consumed


def test_all_timeouts_consume_one_unit(tmp_path):
    def handler(request):
        raise httpx.ConnectTimeout("down", request=request)

    quota = QuotaLedger(tmp_path / "q", clock=lambda: FIXED_NOW)
    with pytest.raises(HttpCollectionError, match="HTTP_TIMEOUT"):
        _client(handler).get_json(URL, params=None, provider="marketaux", resource="r",
                                  raw_store=RawEvidenceStore(tmp_path / "raw"),
                                  before_attempt=lambda: quota.reserve(limit=60))
    ledger = json.loads((tmp_path / "q" / "marketaux-2026-10-01.json").read_text())
    assert ledger["used"] == 1


def test_each_attempt_logs_one_json_line_to_stderr(tmp_path, capsys):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("slow", request=request)
        if attempts == 2:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"ok": True})

    ticks = iter([0.0, 1.5, 2.0, 2.25, 3.0, 3.1])
    client = BoundedHttpClient(_policy(), transport=httpx.MockTransport(handler),
                               sleep=lambda _s: None, clock=lambda: FIXED_NOW,
                               monotonic=lambda: next(ticks))
    client.get_json(URL, params={"api_token": "secret"}, provider="marketaux",
                    resource="news-all-page-1", raw_store=RawEvidenceStore(tmp_path))
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = [json.loads(line) for line in captured.err.splitlines()]
    assert lines == [
        {"event": "http_attempt", "provider": "marketaux", "resource": "news-all-page-1",
         "attempt": 1, "elapsed_ms": 1500, "outcome": "ReadTimeout"},
        {"event": "http_attempt", "provider": "marketaux", "resource": "news-all-page-1",
         "attempt": 2, "elapsed_ms": 250, "outcome": 503},
        {"event": "http_attempt", "provider": "marketaux", "resource": "news-all-page-1",
         "attempt": 3, "elapsed_ms": 100, "outcome": 200},
    ]
    assert "secret" not in captured.err
