from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.history.exchange_calendar import is_trading_day
from src.history.raw_snapshot import RawSnapshotStore
from src.models import Candle
from src.premarket.contract import PremarketConfig, quality_defects, sha256_payload
from src.premarket.market_snapshot import (
    MarketSnapshotPolicy,
    build_market_bundle,
)
from src.premarket.normalizer import normalize_bundles
from src.premarket.planner import build_daily_brief
from src.premarket.schedule import build_premarket_schedule

ROOT = Path(__file__).resolve().parents[1]


def _config() -> PremarketConfig:
    return PremarketConfig.from_mapping({
        "schema": "premarket-config/v1",
        "required_source_kinds": ["MARKET", "NEWS"],
        "source_freshness_minutes": 180,
        "source_freshness_minutes_by_kind": {"FILING": 1440},
        "primary_offset_minutes": 90,
        "verification_offset_minutes": 30,
        "freeze_offset_minutes": 20,
        "max_candidates": 3,
        "min_liquidity_score": 0.0,
        "min_catalyst_score": 0.0,
        "approved_strategy_templates": [],
        "approved_short_strategy_templates": [],
        "allowed_symbols": ["SPY", "QQQ", "AAPL", "MSFT"],
        "excluded_universe_classes": ["inverse_etf"],
        "weights": {
            "catalyst_score": 0.30,
            "trend_score": 0.25,
            "relative_strength_score": 0.20,
            "volume_score": 0.15,
            "liquidity_score": 0.10,
        },
    })


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _provider_bundle(*, vix: float = 24.99) -> dict:
    as_of = "2026-03-09T12:00:00Z"
    return {
        "schema": "premarket-provider-bundle/v1",
        "artifact_sha256": "a" * 64,
        "collection_status": "OK",
        "collection_defects": [],
        "trading_day": "2026-03-09",
        "stage": "PRIMARY",
        "as_of": as_of,
        "symbols": ["SPY", "QQQ", "AAPL", "MSFT"],
        "providers": [
            {
                "provider": "marketaux",
                "kind": "NEWS",
                "status": "OK",
                "source": {
                    "source_id": "news-src",
                    "kind": "NEWS",
                    "status": "OK",
                    "observed_at": "2026-03-09T11:40:00Z",
                    "available_at": "2026-03-09T12:00:30Z",
                    "source_url": "https://api.marketaux.com/v1/news/all",
                    "content_sha256": "b" * 64,
                },
                "items": [
                    {
                        "item_id": "news:aapl:1",
                        "item_type": "NEWS",
                        "published_at": "2026-03-09T11:30:00Z",
                        "source_url": "https://publisher.invalid/aapl",
                        "content_sha256": "c" * 64,
                        "symbols": ["AAPL"],
                        "sentiment": 0.5,
                    },
                    {
                        "item_id": "news:aapl:2",
                        "item_type": "NEWS",
                        "published_at": "2026-03-09T11:40:00Z",
                        "source_url": "https://publisher.invalid/aapl-2",
                        "content_sha256": "d" * 64,
                        "symbols": ["AAPL"],
                        "sentiment": -0.1,
                    },
                    {
                        "item_id": "news:msft:1",
                        "item_type": "NEWS",
                        "published_at": "2026-03-09T11:35:00Z",
                        "source_url": "https://publisher.invalid/msft",
                        "content_sha256": "e" * 64,
                        "symbols": ["MSFT"],
                        "sentiment": 0.2,
                    },
                ],
            },
            {
                "provider": "sec-edgar",
                "kind": "FILING",
                "status": "OK",
                "source": {
                    "source_id": "filing-src",
                    "kind": "FILING",
                    "status": "OK",
                    "observed_at": "2026-03-09T10:00:00Z",
                    "available_at": "2026-03-09T12:00:30Z",
                    "source_url": "https://data.sec.gov/submissions",
                    "content_sha256": "f" * 64,
                },
                "items": [
                    {
                        "item_id": "sec:msft:recent",
                        "item_type": "FILING",
                        "accepted_at": "2026-03-09T10:00:00Z",
                        "form": "8-K",
                        "source_url": "https://sec.invalid/msft",
                        "content_sha256": "1" * 64,
                        "symbols": ["MSFT"],
                    },
                    {
                        "item_id": "sec:aapl:old",
                        "item_type": "FILING",
                        "accepted_at": "2026-03-07T10:00:00Z",
                        "form": "8-K",
                        "source_url": "https://sec.invalid/aapl-old",
                        "content_sha256": "2" * 64,
                        "symbols": ["AAPL"],
                    },
                ],
            },
            {
                "provider": "fred-alfred",
                "kind": "MACRO",
                "status": "OK",
                "source": {
                    "source_id": "fred-src",
                    "kind": "MACRO",
                    "status": "OK",
                    "observed_at": "2026-03-09T12:00:30Z",
                    "available_at": "2026-03-09T12:00:30Z",
                    "source_url": "https://api.stlouisfed.org/fred/series/observations",
                    "content_sha256": "3" * 64,
                },
                "items": [
                    {
                        "item_id": "fred:VIXCLS",
                        "item_type": "MACRO",
                        "series_id": "VIXCLS",
                        "observation_date": "2026-03-06",
                        "value": str(vix),
                    }
                ],
            },
        ],
    }


def _daily(symbol: str, *, close_last: float = 101.0, below_sma: bool = False) -> list[dict]:
    out = []
    start = date(2025, 5, 12)
    day = start
    idx = 0
    while len(out) < 200:
        if not is_trading_day(day):
            day += timedelta(days=1)
            continue
        close = 100.0
        if len(out) == 199:
            close = 80.0 if below_sma else close_last
        out.append({
            "symbol": symbol,
            "interval": "1d",
            "ts_utc": f"{day.isoformat()}T21:00:00Z",
            "ts_exchange": f"{day.isoformat()}T16:00:00-05:00",
            "exchange_date": day.isoformat(),
            "session": "REGULAR",
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1_000_000 + idx,
            "currency": "USD",
            "adjusted": True,
            "market": "US",
        })
        idx += 1
        day += timedelta(days=1)
    return out


def _minute(symbol: str, *, latest: float = 110.0, volume_last: float = 200.0) -> list[dict]:
    start = datetime(2026, 3, 9, 9, 30, tzinfo=timezone.utc)
    rows = []
    for idx in range(120):
        ts = start + timedelta(minutes=idx)
        close = latest - (119 - idx) * 0.1
        rows.append({
            "symbol": symbol,
            "interval": "1m",
            "ts_utc": _iso(ts),
            "ts_exchange": _iso(ts),
            "exchange_date": "2026-03-09",
            "session": "PRE",
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": volume_last if idx >= 115 else 100.0,
            "currency": "USD",
            "adjusted": True,
            "market": "US",
        })
    return rows


def _market_bundle(*, vix: float = 24.99, below_sma: bool = False) -> dict:
    symbols = []
    for idx, symbol in enumerate(["SPY", "QQQ", "AAPL", "MSFT"]):
        symbols.append({
            "symbol": symbol,
            "status": "OK",
            "defects": [],
            "candles_1m": _minute(symbol, latest=110 + idx, volume_last=200 + idx),
            "candles_1d": _daily(symbol, close_last=101 + idx, below_sma=below_sma and symbol in {"SPY", "QQQ"}),
        })
    bundle = {
        "schema": "premarket-market-bundle/v1",
        "trading_day": "2026-03-09",
        "stage": "PRIMARY",
        "as_of": "2026-03-09T12:00:00Z",
        "generated_at": "2026-03-09T12:00:30Z",
        "provider_bundle_sha256": "a" * 64,
        "collection_status": "OK",
        "collection_defects": [],
        "raw_manifest": {"raw_content_sha256": "4" * 64},
        "request_receipts": [],
        "symbols": symbols,
    }
    bundle["artifact_sha256"] = "5" * 64
    return bundle


def test_normalizer_builds_v2_scores_and_default_no_trade() -> None:
    normalized = normalize_bundles(_provider_bundle(), _market_bundle())
    assert normalized["schema"] == "premarket-input/v2"
    assert normalized["stage"] == "PRIMARY"
    aapl = next(item for item in normalized["candidates"] if item["symbol"] == "AAPL")
    assert aapl["strategy_template_id"] is None
    assert aapl["catalyst_score"] == pytest.approx(0.2)
    assert aapl["trend_score"] in {0.0, 0.5, 1.0}
    assert aapl["volume_score"] == 0.9
    assert 0.0 <= aapl["relative_strength_score"] <= 1.0
    assert 0.0 <= aapl["liquidity_score"] <= 1.0
    assert normalized["market_regime"]["label"] == "RISK_ON"
    assert normalized["provenance"]["run_mode"] == "LIVE_WINDOW"

    schedule = build_premarket_schedule(date(2026, 3, 9))
    brief = build_daily_brief(normalized, schedule=schedule, config=_config())
    assert brief["brief"]["status"] == "NO_TRADE"
    assert brief["brief"]["orders"] == []
    assert brief["brief"]["reason_codes"] == ["NO_APPROVED_STRATEGY_TEMPLATE"]


def test_catalyst_mean_includes_neutral_news_and_excludes_filings() -> None:
    provider = _provider_bundle()
    news_provider = next(item for item in provider["providers"] if item["provider"] == "marketaux")
    news_provider["items"] = [
        {
            "item_id": "news:aapl:positive",
            "item_type": "NEWS",
            "published_at": "2026-03-09T11:30:00Z",
            "source_url": "https://publisher.invalid/aapl-positive",
            "content_sha256": "a" * 64,
            "symbols": ["AAPL"],
            "sentiment": 1.0,
        },
        {
            "item_id": "news:aapl:neutral",
            "item_type": "NEWS",
            "published_at": "2026-03-09T11:31:00Z",
            "source_url": "https://publisher.invalid/aapl-neutral",
            "content_sha256": "b" * 64,
            "symbols": ["AAPL"],
            "sentiment": 0.0,
        },
    ]
    filing_provider = next(item for item in provider["providers"] if item["provider"] == "sec-edgar")
    filing_provider["items"] = [
        {
            "item_id": "sec:aapl:recent",
            "item_type": "FILING",
            "accepted_at": "2026-03-09T10:00:00Z",
            "form": "8-K",
            "source_url": "https://sec.invalid/aapl",
            "content_sha256": "c" * 64,
            "symbols": ["AAPL"],
        }
    ]
    normalized = normalize_bundles(provider, _market_bundle())
    aapl = next(item for item in normalized["candidates"] if item["symbol"] == "AAPL")
    assert aapl["catalyst_score"] == pytest.approx(0.5)

    news_provider["items"] = []
    normalized = normalize_bundles(provider, _market_bundle())
    aapl = next(item for item in normalized["candidates"] if item["symbol"] == "AAPL")
    assert aapl["catalyst_score"] == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("vix", "below_sma", "expected"),
    [
        (25.0, False, "UNCERTAIN"),
        (30.0, False, "RISK_OFF"),
        (24.0, True, "RISK_OFF"),
    ],
)
def test_regime_boundaries(vix: float, below_sma: bool, expected: str) -> None:
    normalized = normalize_bundles(
        _provider_bundle(vix=vix),
        _market_bundle(vix=vix, below_sma=below_sma),
    )
    assert normalized["market_regime"]["label"] == expected


def test_v2_allows_ingestion_after_as_of_but_not_after_generated_at() -> None:
    normalized = normalize_bundles(_provider_bundle(), _market_bundle())
    normalized["sources"][0]["ingested_at"] = "2026-03-09T12:00:20Z"
    schedule = build_premarket_schedule(date(2026, 3, 9))
    assert "FUTURE_SOURCE_EVIDENCE" not in quality_defects(normalized, schedule, _config())
    normalized["sources"][0]["ingested_at"] = "2026-03-09T12:00:31Z"
    assert "FUTURE_SOURCE_EVIDENCE" in quality_defects(normalized, schedule, _config())


def test_news_source_observed_at_is_preserved_for_freshness() -> None:
    provider = _provider_bundle()
    news_provider = next(item for item in provider["providers"] if item["kind"] == "NEWS")
    news_provider["source"]["observed_at"] = "2026-03-09T08:00:00Z"
    normalized = normalize_bundles(provider, _market_bundle())
    news_source = next(item for item in normalized["sources"] if item["kind"] == "NEWS")
    assert news_source["observed_at"] == "2026-03-09T08:00:00Z"

    defects = quality_defects(normalized, build_premarket_schedule(date(2026, 3, 9)), _config())
    assert "STALE_NEWS_SOURCE" in defects
    assert "STALE_EVENT_SOURCE" in defects


def test_fresh_news_source_passes_without_silent_freshening() -> None:
    normalized = normalize_bundles(_provider_bundle(), _market_bundle())
    news_source = next(item for item in normalized["sources"] if item["kind"] == "NEWS")
    assert news_source["observed_at"] == "2026-03-09T11:40:00Z"
    assert news_source["available_at"] == "2026-03-09T11:40:00Z"
    defects = quality_defects(normalized, build_premarket_schedule(date(2026, 3, 9)), _config())
    assert "STALE_NEWS_SOURCE" not in defects
    assert "STALE_EVENT_SOURCE" not in defects


def test_empty_news_query_without_observed_at_uses_as_of_query_evidence() -> None:
    provider = _provider_bundle()
    news_provider = next(item for item in provider["providers"] if item["kind"] == "NEWS")
    news_provider["source"].pop("observed_at")
    news_provider["source"].pop("available_at")
    news_provider["items"] = []
    normalized = normalize_bundles(provider, _market_bundle())
    news_source = next(item for item in normalized["sources"] if item["kind"] == "NEWS")
    assert news_source["observed_at"] == "2026-03-09T12:00:00Z"
    assert news_source["available_at"] == "2026-03-09T12:00:00Z"
    defects = quality_defects(normalized, build_premarket_schedule(date(2026, 3, 9)), _config())
    assert "STALE_NEWS_SOURCE" not in defects
    assert "FUTURE_SOURCE_EVIDENCE" not in defects


def test_future_news_source_observed_at_is_not_clamped_away() -> None:
    provider = _provider_bundle()
    news_provider = next(item for item in provider["providers"] if item["kind"] == "NEWS")
    news_provider["source"]["observed_at"] = "2026-03-09T12:01:00Z"
    normalized = normalize_bundles(provider, _market_bundle())
    news_source = next(item for item in normalized["sources"] if item["kind"] == "NEWS")
    assert news_source["observed_at"] == "2026-03-09T12:01:00Z"
    assert "FUTURE_SOURCE_EVIDENCE" in quality_defects(
        normalized, build_premarket_schedule(date(2026, 3, 9)), _config()
    )


def test_filing_source_uses_kind_specific_1440_minute_freshness() -> None:
    provider = _provider_bundle()
    filing_provider = next(item for item in provider["providers"] if item["kind"] == "FILING")
    filing_provider["source"]["observed_at"] = "2026-03-08T13:00:00Z"
    filing_provider["items"] = [
        {
            "item_id": "sec:aapl:23h",
            "item_type": "FILING",
            "accepted_at": "2026-03-08T13:00:00Z",
            "form": "8-K",
            "source_url": "https://sec.invalid/aapl-23h",
            "content_sha256": "3" * 64,
            "symbols": ["AAPL"],
        }
    ]
    config = _config()
    config = PremarketConfig(
        **{**config.to_dict(), "required_source_kinds": ("FILING", "MARKET", "NEWS")}
    )
    normalized = normalize_bundles(provider, _market_bundle())
    defects = quality_defects(normalized, build_premarket_schedule(date(2026, 3, 9)), config)
    assert "STALE_FILING_SOURCE" not in defects
    assert "STALE_EVENT_SOURCE" not in defects

    filing_provider["source"]["observed_at"] = "2026-03-08T11:59:00Z"
    filing_provider["items"][0]["accepted_at"] = "2026-03-08T11:59:00Z"
    normalized = normalize_bundles(provider, _market_bundle())
    defects = quality_defects(normalized, build_premarket_schedule(date(2026, 3, 9)), config)
    assert "STALE_FILING_SOURCE" in defects


def test_macro_source_uses_as_of_snapshot_semantics() -> None:
    normalized = normalize_bundles(_provider_bundle(), _market_bundle())
    macro_source = next(item for item in normalized["sources"] if item["kind"] == "MACRO")
    assert macro_source["observed_at"] == "2026-03-09T12:00:00Z"
    assert macro_source["available_at"] == "2026-03-09T12:00:00Z"
    assert "FUTURE_SOURCE_EVIDENCE" not in quality_defects(
        normalized, build_premarket_schedule(date(2026, 3, 9)), _config()
    )


def test_v2_stage_windows_are_exact_and_stage_specific() -> None:
    schedule = build_premarket_schedule(date(2026, 3, 9))
    normalized = normalize_bundles(_provider_bundle(), _market_bundle())
    assert quality_defects(normalized, schedule, _config()) == []

    late_primary = dict(normalized)
    late_primary["generated_at"] = schedule.verification_utc
    assert "GENERATED_AFTER_STAGE_WINDOW" in quality_defects(
        late_primary, schedule, _config()
    )

    freeze_provider = _provider_bundle()
    freeze_provider["stage"] = "FREEZE"
    freeze_provider["as_of"] = schedule.freeze_utc
    freeze_market = _market_bundle()
    freeze_market["stage"] = "FREEZE"
    freeze_market["as_of"] = schedule.freeze_utc
    freeze_market["generated_at"] = "2026-03-09T13:20:00Z"
    freeze = normalize_bundles(freeze_provider, freeze_market)
    assert "GENERATED_AFTER_STAGE_WINDOW" not in quality_defects(
        freeze, schedule, _config()
    )
    freeze["generated_at"] = schedule.market_open_utc
    assert "GENERATED_AFTER_STAGE_WINDOW" in quality_defects(
        freeze, schedule, _config()
    )

    wrong_as_of = dict(normalized)
    wrong_as_of["as_of"] = "2026-03-09T12:01:00Z"
    assert "STAGE_AS_OF_MISMATCH" in quality_defects(wrong_as_of, schedule, _config())


def test_run_mode_marks_late_generation_as_retrospective_reconstruction() -> None:
    market = _market_bundle()
    market["generated_at"] = "2026-03-10T00:00:00Z"
    normalized = normalize_bundles(_provider_bundle(), market)
    assert normalized["provenance"]["run_mode"] == "RETROSPECTIVE_RECONSTRUCTION"
    assert "GENERATED_AFTER_STAGE_WINDOW" in quality_defects(
        normalized, build_premarket_schedule(date(2026, 3, 9)), _config()
    )


def test_failed_market_bundle_still_normalizes_to_auditable_no_trade() -> None:
    market = _market_bundle()
    market["collection_status"] = "FAIL_CLOSED"
    market["collection_defects"] = ["FUTURE_OR_INCOMPLETE_1M_CANDLE:AAPL"]
    normalized = normalize_bundles(_provider_bundle(), market)
    assert normalized["candidates"] == []
    assert normalized["market_regime"]["label"] == "UNCERTAIN"
    assert normalized["provenance"]["fail_closed_defects"] == [
        "FUTURE_OR_INCOMPLETE_1M_CANDLE:AAPL"
    ]
    schedule = build_premarket_schedule(date(2026, 3, 9))
    brief = build_daily_brief(normalized, schedule=schedule, config=_config())
    assert brief["brief"]["status"] == "NO_TRADE"
    assert "MISSING_OR_UNHEALTHY_MARKET_SOURCE" in brief["brief"]["reason_codes"]


def test_percentile_ties_are_average_rank_and_order_invariant() -> None:
    market = _market_bundle()
    for item in market["symbols"]:
        if item["symbol"] in {"AAPL", "MSFT"}:
            for row in item["candles_1m"]:
                row["close"] = 110.0
            for row in item["candles_1d"]:
                row["close"] = 100.0
    first = normalize_bundles(_provider_bundle(), market)
    market["symbols"].reverse()
    second = normalize_bundles(_provider_bundle(), market)
    assert first["artifact_sha256"] == second["artifact_sha256"]
    scores = {
        item["symbol"]: item["relative_strength_score"]
        for item in first["candidates"]
        if item["symbol"] in {"AAPL", "MSFT"}
    }
    assert scores["AAPL"] == scores["MSFT"]


def test_provider_order_invariant_hash_and_output() -> None:
    provider = _provider_bundle()
    reversed_provider = copy.deepcopy(provider)
    reversed_provider["providers"].reverse()
    first = normalize_bundles(provider, _market_bundle())
    second = normalize_bundles(reversed_provider, _market_bundle())
    assert first == second
    assert first["artifact_sha256"] == second["artifact_sha256"]


def test_normalizer_rejects_invalid_provider_schema() -> None:
    provider = _provider_bundle()
    provider["schema"] = "wrong/v1"
    with pytest.raises(ValueError, match="provider bundle"):
        normalize_bundles(provider, _market_bundle())


class _FakeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, str | None]] = []

    def fetch_page(self, symbol, interval, count=200, before=None, *, raw_sink=None):
        self.calls.append((symbol, interval, count, before))
        if raw_sink is not None:
            raw_sink(
                symbol=symbol,
                interval=interval,
                before=before,
                requested_at="2026-03-09T12:00:00Z",
                received_at="2026-03-09T12:00:01Z",
                source="toss",
                payload={"candles": [{"safe": True}], "nextBefore": None},
            )
        base = datetime(2026, 3, 9, 9, 0, tzinfo=timezone.utc)
        if interval == "1m":
            candles = [
                Candle.from_moment(
                    symbol=symbol,
                    interval="1m",
                    moment=base + timedelta(minutes=idx),
                    o=1,
                    h=1,
                    low=1,
                    c=1,
                    v=1,
                    currency="USD",
                    adjusted=True,
                )
                for idx in range(121)
            ]
            candles.append(Candle.from_moment(
                symbol=symbol,
                interval="1m",
                moment=datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc),
                o=1,
                h=1,
                low=1,
                c=1,
                v=1,
                currency="USD",
                adjusted=True,
            ))
        else:
            candles = [
                Candle.from_moment(
                    symbol=symbol,
                    interval="1d",
                    moment=datetime(2026, 3, 6, 21, 0, tzinfo=timezone.utc) - timedelta(days=idx),
                    o=1,
                    h=1,
                    low=1,
                    c=1,
                    v=1,
                    currency="USD",
                    adjusted=True,
                )
                for idx in range(200)
            ]
        return candles, None


class _FutureProvider(_FakeProvider):
    def fetch_page(self, symbol, interval, count=200, before=None, *, raw_sink=None):
        candles, cursor = super().fetch_page(
            symbol, interval, count=count, before=before, raw_sink=raw_sink
        )
        if interval == "1m":
            candles.append(Candle.from_moment(
                symbol=symbol,
                interval="1m",
                moment=datetime(2026, 3, 9, 12, 1, tzinfo=timezone.utc),
                o=1,
                h=1,
                low=1,
                c=1,
                v=1,
                currency="USD",
                adjusted=True,
            ))
        else:
            candles.append(Candle.from_moment(
                symbol=symbol,
                interval="1d",
                moment=datetime(2026, 3, 10, 21, 0, tzinfo=timezone.utc),
                o=1,
                h=1,
                low=1,
                c=1,
                v=1,
                currency="USD",
                adjusted=True,
            ))
        return candles, cursor


def test_market_snapshot_uses_before_cutoff_preserves_raw_and_clips_incomplete(tmp_path: Path) -> None:
    provider = _FakeProvider()
    sleeps: list[float] = []
    bundle = build_market_bundle(
        _provider_bundle(),
        raw_root=tmp_path / "raw",
        config_path=ROOT / "config" / "premarket_daily_brief.yaml",
        provider=provider,
        policy=MarketSnapshotPolicy(max_pages_1m=1, max_pages_1d=1),
        sleep=sleeps.append,
        clock=lambda: datetime(2026, 3, 9, 12, 0, 30, tzinfo=timezone.utc),
    )
    assert all(call[3] == "2026-03-09T12:00:00Z" for call in provider.calls)
    assert all(call[2] <= 200 for call in provider.calls)
    assert sleeps == [0.4] * (len(provider.calls) - 1)
    assert all("received_at" in item and "requested_at" in item for item in bundle["request_receipts"])
    assert bundle["raw_manifest"]["content_file_count"] >= 1
    spy = next(item for item in bundle["symbols"] if item["symbol"] == "SPY")
    assert len(spy["candles_1m"]) == 121
    assert spy["defects"] == []
    assert spy["excluded_boundary_counts"]["forming_1m_exact_as_of"] == 1
    assert spy["excluded_boundary_counts"]["incomplete_1m_at_or_before_as_of"] == 1
    assert bundle["collection_status"] == "OK"
    raw_payload = RawSnapshotStore(str(tmp_path / "raw")).read_payload(
        bundle["request_receipts"][0]["rel_path"]
    )
    assert "Authorization" not in json.dumps(raw_payload)


def test_market_snapshot_rejects_actual_future_rows_not_boundary_rows(tmp_path: Path) -> None:
    bundle = build_market_bundle(
        _provider_bundle(),
        raw_root=tmp_path / "raw",
        config_path=ROOT / "config" / "premarket_daily_brief.yaml",
        provider=_FutureProvider(),
        policy=MarketSnapshotPolicy(max_pages_1m=1, max_pages_1d=1, request_spacing_seconds=0),
        sleep=lambda _seconds: None,
        clock=lambda: datetime(2026, 3, 9, 12, 0, 30, tzinfo=timezone.utc),
    )
    spy = next(item for item in bundle["symbols"] if item["symbol"] == "SPY")
    assert bundle["collection_status"] == "FAIL_CLOSED"
    assert "FUTURE_OR_INCOMPLETE_1M_CANDLE" in spy["defects"]
    assert "FUTURE_OR_INCOMPLETE_1D_CANDLE" in spy["defects"]
    assert spy["excluded_boundary_counts"]["forming_1m_exact_as_of"] == 1
    assert spy["excluded_boundary_counts"]["daily_on_trading_day"] == 0


def test_no_db_order_broker_or_forward_imports_in_new_premarket_surface() -> None:
    paths = [
        ROOT / "src" / "premarket" / "market_snapshot.py",
        ROOT / "src" / "premarket" / "normalizer.py",
        ROOT / "scripts" / "collect_premarket_market.py",
        ROOT / "scripts" / "normalize_premarket_input.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    forbidden = (
        "market_history.sqlite",
        "src.order",
        "src.live",
        "src.broker",
        "forward_return",
        "compute_forward",
        "submit_order",
        "place_order",
    )
    assert {token for token in forbidden if token in source} == set()


def test_20260915_replay_receipt_is_sanitized_and_matches_markdown() -> None:
    receipt_path = ROOT / "docs" / "research" / "PREMARKET_NORMALIZATION_REPLAY_20260915_RECEIPT.json"
    markdown_path = ROOT / "docs" / "research" / "PREMARKET_NORMALIZATION_REPLAY_20260915.md"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")

    assert receipt["schema"] == "premarket-normalization-replay-receipt/v1"
    assert receipt["classification"] == "RETROSPECTIVE_RECONSTRUCTION"
    assert receipt["trading_day"] == "2026-09-15"
    assert sha256_payload(receipt) == "4dc9a542d493575fec46db78dcadde85d4a3a2b1cc516a112b24163dec52d055"
    assert receipt_path.name in markdown
    assert sha256_payload(receipt) in markdown

    safety = receipt["safety_assertions"]
    assert safety == {
        "all_operational_briefs_no_trade": True,
        "no_api_recollection": True,
        "no_credentials": True,
        "no_news_text": True,
        "no_oos": True,
        "no_orders": True,
        "no_pnl": True,
        "no_returns": True,
    }

    stages = receipt["stages"]
    assert [stage["stage"] for stage in stages] == ["PRIMARY", "VERIFICATION", "FREEZE"]
    expected_candidates = {"PRIMARY": 11, "VERIFICATION": 11, "FREEZE": 8}
    expected_forming = {"PRIMARY": 19, "VERIFICATION": 22, "FREEZE": 24}
    expected_artifact_stage = {
        "PRIMARY": "primary",
        "VERIFICATION": "verification",
        "FREEZE": "freeze",
    }
    for stage in stages:
        stage_name = stage["stage"]
        artifact_stage = expected_artifact_stage[stage_name]
        assert all("/" not in name and "\\" not in name for name in stage["artifacts"].values())
        assert stage["market"]["status"] == "OK"
        assert stage["market"]["defects"] == []
        assert stage["market"]["boundary_clip_counts"]["forming_1m_exact_as_of"] == expected_forming[stage_name]
        assert stage["market"]["boundary_clip_counts"]["daily_on_trading_day"] == 28
        assert stage["input"] == {
            "candidate_count": expected_candidates[stage_name],
            "regime": "RISK_ON",
            "run_mode": "RETROSPECTIVE_RECONSTRUCTION",
        }
        assert stage["brief"] == {
            "status": "NO_TRADE",
            "watchlist_count": 0,
            "reason_codes": ["GENERATED_AFTER_STAGE_WINDOW"],
            "orders": [],
            "executable": False,
            "promotion_allowed": False,
        }
        assert f"| {stage_name} | `OK` | 0 | {expected_forming[stage_name]} forming 1m, 28 trading-day daily |" in markdown
        assert f"| {stage_name} | `{stage['sha256']['market']}` | `{stage['sha256']['input']}` | `{stage['sha256']['brief']}` |" in markdown

        for kind, basename in (
            ("market", stage["artifacts"]["market_basename"]),
            ("input", stage["artifacts"]["input_basename"]),
            ("brief", stage["artifacts"]["brief_basename"]),
        ):
            artifact_path = ROOT / "reports" / basename
            if not artifact_path.exists():
                continue
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            assert artifact["artifact_sha256"] == stage["sha256"][kind]
            assert artifact_stage in basename


def test_20260915_replay_receipt_has_no_secret_text_news_or_absolute_paths() -> None:
    receipt_path = ROOT / "docs" / "research" / "PREMARKET_NORMALIZATION_REPLAY_20260915_RECEIPT.json"
    markdown_path = ROOT / "docs" / "research" / "PREMARKET_NORMALIZATION_REPLAY_20260915.md"
    payload = receipt_path.read_text(encoding="utf-8")
    receipt = json.loads(payload)
    markdown = markdown_path.read_text(encoding="utf-8")
    combined = payload + "\n" + markdown

    assert "/Users/" not in combined
    assert str(ROOT) not in combined
    assert "source_url" not in payload
    assert "headline" not in payload.lower()
    string_values: list[str] = []

    def collect_strings(value: object) -> None:
        if isinstance(value, str):
            string_values.append(value)
        elif isinstance(value, dict):
            for child in value.values():
                collect_strings(child)
        elif isinstance(value, list):
            for child in value:
                collect_strings(child)

    collect_strings(receipt)
    assert not any("news_text" in value.lower() for value in string_values)
    assert not re.search(
        r"(TOSS_CLIENT|TOSS_API|SECRET|Authorization|Bearer|api_key|client_secret|client_id|access_token)",
        combined,
        flags=re.IGNORECASE,
    )
