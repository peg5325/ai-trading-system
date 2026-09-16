"""Read-only Toss market snapshot bundle for premarket normalization."""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.history.raw_snapshot import RawSnapshotRecord, RawSnapshotStore
from src.models import Candle
from src.premarket.contract import load_config, sha256_payload
from src.premarket.report import json_bytes, write_immutable_pair
from src.premarket.schedule import parse_iso
from src.providers.toss_market_data import TossMarketDataProvider

MARKET_BUNDLE_SCHEMA = "premarket-market-bundle/v1"
MARKET_RECEIPT_SCHEMA = "premarket-market-run-receipt/v1"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def candle_to_dict(candle: Candle) -> dict[str, Any]:
    return {
        "symbol": candle.symbol,
        "interval": candle.interval,
        "ts_utc": iso_z(candle.ts_utc),
        "ts_exchange": candle.ts_exchange.isoformat(),
        "exchange_date": candle.ts_exchange.date().isoformat(),
        "session": candle.session,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "currency": candle.currency,
        "adjusted": candle.adjusted,
        "market": candle.market,
    }


def _dedupe_candles(candles: list[Candle], defects: set[str]) -> list[Candle]:
    by_key: dict[tuple[str, str, datetime], Candle] = {}
    for candle in candles:
        key = (candle.symbol, candle.interval, candle.ts_utc)
        previous = by_key.get(key)
        if previous is not None:
            if previous != candle:
                defects.add(f"CONFLICTING_CANDLE:{candle.symbol}:{candle.interval}")
            continue
        by_key[key] = candle
    return sorted(by_key.values(), key=lambda item: item.ts_utc)


def _fetch_pages(
    provider: TossMarketDataProvider,
    raw_store: RawSnapshotStore,
    *,
    symbol: str,
    interval: str,
    before: str,
    page_size: int,
    max_pages: int,
    request_spacing_seconds: float,
    sleep: Callable[[float], object],
    request_state: dict[str, int],
) -> tuple[list[Candle], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []

    def raw_sink(**kwargs: Any) -> object:
        rec = raw_store.emit(**kwargs)
        record = asdict(rec) if isinstance(rec, RawSnapshotRecord) else dict(rec)
        record["requested_at"] = str(kwargs["requested_at"])
        record["received_at"] = str(kwargs["received_at"])
        records.append(record)
        return rec

    cursor: str | None = before
    out: list[Candle] = []
    for _ in range(max_pages):
        if request_state["count"] > 0 and request_spacing_seconds > 0:
            sleep(request_spacing_seconds)
        previous_cursor = cursor
        candles, next_before = provider.fetch_page(
            symbol,
            interval,
            count=page_size,
            before=cursor,
            raw_sink=raw_sink,
        )
        request_state["count"] += 1
        out.extend(candles)
        if not next_before:
            break
        if previous_cursor is not None and parse_iso(str(next_before)) >= parse_iso(str(previous_cursor)):
            records.append({
                "symbol": symbol,
                "interval": interval,
                "before": previous_cursor,
                "content_sha256": "",
                "cursor_defect": "CURSOR_STALLED_OR_REVERSED",
                "next_before": str(next_before),
            })
            break
        cursor = str(next_before)
    return out, records


@dataclass(frozen=True)
class MarketSnapshotPolicy:
    page_size_1m: int = 200
    max_pages_1m: int = 2
    page_size_1d: int = 200
    max_pages_1d: int = 2
    min_1m_bars: int = 120
    min_1d_bars: int = 20
    benchmark_min_1d_bars: int = 200
    request_spacing_seconds: float = 0.4


def build_market_bundle(
    provider_bundle: Mapping[str, Any],
    *,
    raw_root: str | Path,
    config_path: str | Path = "config/premarket_daily_brief.yaml",
    clock: Callable[[], datetime] = utc_now,
    policy: MarketSnapshotPolicy | None = None,
    provider: TossMarketDataProvider | None = None,
    sleep: Callable[[float], object] = time.sleep,
) -> dict[str, Any]:
    if provider_bundle.get("schema") != "premarket-provider-bundle/v1":
        raise ValueError("provider bundle must be premarket-provider-bundle/v1")
    if policy is None:
        policy = MarketSnapshotPolicy()
    config = load_config(config_path)
    allowed = tuple(config.allowed_symbols)
    if not allowed:
        raise ValueError("configured allowed universe is empty")
    trading_day = date.fromisoformat(str(provider_bundle["trading_day"]))
    as_of = parse_iso(str(provider_bundle["as_of"]))
    before = iso_z(as_of)
    raw_store = RawSnapshotStore(str(raw_root))
    defects: set[str] = set()
    if provider_bundle.get("collection_status") != "OK":
        defects.add("PROVIDER_BUNDLE_NOT_OK")

    own_provider = provider is None
    if provider is None:
        provider = TossMarketDataProvider(load_settings())
    try:
        symbols: list[dict[str, Any]] = []
        request_receipts: list[dict[str, Any]] = []
        request_state = {"count": 0}
        for symbol in allowed:
            symbol_defects: set[str] = set()
            one_minute, one_minute_receipts = _fetch_pages(
                provider,
                raw_store,
                symbol=symbol,
                interval="1m",
                before=before,
                page_size=policy.page_size_1m,
                max_pages=policy.max_pages_1m,
                request_spacing_seconds=policy.request_spacing_seconds,
                sleep=sleep,
                request_state=request_state,
            )
            daily, daily_receipts = _fetch_pages(
                provider,
                raw_store,
                symbol=symbol,
                interval="1d",
                before=before,
                page_size=policy.page_size_1d,
                max_pages=policy.max_pages_1d,
                request_spacing_seconds=policy.request_spacing_seconds,
                sleep=sleep,
                request_state=request_state,
            )
            request_receipts.extend(
                {"symbol": symbol, "interval": "1m", **item} for item in one_minute_receipts
            )
            request_receipts.extend(
                {"symbol": symbol, "interval": "1d", **item} for item in daily_receipts
            )
            cursor_defects = [
                item["cursor_defect"] for item in [*one_minute_receipts, *daily_receipts]
                if "cursor_defect" in item
            ]
            symbol_defects.update(cursor_defects)
            excluded_boundary_1m = sum(1 for candle in one_minute if candle.ts_utc == as_of)
            true_future_1m = sum(1 for candle in one_minute if candle.ts_utc > as_of)
            clipped_1m = [
                candle for candle in one_minute
                if candle.ts_utc + timedelta(minutes=1) <= as_of
            ]
            excluded_incomplete_1m = len(one_minute) - len(clipped_1m)
            if true_future_1m:
                symbol_defects.add("FUTURE_OR_INCOMPLETE_1M_CANDLE")
            excluded_boundary_1d = sum(
                1 for candle in daily if candle.ts_exchange.date() == trading_day
            )
            true_future_1d = sum(
                1 for candle in daily if candle.ts_exchange.date() > trading_day
            )
            clipped_1d = [
                candle for candle in daily
                if candle.ts_exchange.date() < trading_day
            ]
            excluded_incomplete_1d = len(daily) - len(clipped_1d)
            if true_future_1d:
                symbol_defects.add("FUTURE_OR_INCOMPLETE_1D_CANDLE")
            clipped_1m = _dedupe_candles(clipped_1m, symbol_defects)
            clipped_1d = _dedupe_candles(clipped_1d, symbol_defects)
            if len(clipped_1m) < policy.min_1m_bars:
                symbol_defects.add("INSUFFICIENT_1M_CANDLES")
            min_daily = (
                policy.benchmark_min_1d_bars
                if symbol in {"SPY", "QQQ"}
                else policy.min_1d_bars
            )
            if len(clipped_1d) < min_daily:
                symbol_defects.add("INSUFFICIENT_1D_CANDLES")
            defects.update(f"{code}:{symbol}" for code in symbol_defects)
            symbols.append({
                "symbol": symbol,
                "status": "OK" if not symbol_defects else "FAIL_CLOSED",
                "defects": sorted(symbol_defects),
                "excluded_boundary_counts": {
                    "incomplete_1m_at_or_before_as_of": excluded_incomplete_1m,
                    "forming_1m_exact_as_of": excluded_boundary_1m,
                    "daily_on_trading_day": excluded_boundary_1d,
                    "incomplete_1d_on_or_after_trading_day": excluded_incomplete_1d,
                },
                "candles_1m": [candle_to_dict(candle) for candle in clipped_1m],
                "candles_1d": [candle_to_dict(candle) for candle in clipped_1d],
            })
    finally:
        if own_provider:
            provider.close()

    generated_at = iso_z(clock())
    bundle = {
        "schema": MARKET_BUNDLE_SCHEMA,
        "trading_day": str(provider_bundle["trading_day"]),
        "stage": str(provider_bundle["stage"]),
        "as_of": str(provider_bundle["as_of"]),
        "generated_at": generated_at,
        "provider_bundle_sha256": str(provider_bundle["artifact_sha256"]),
        "collection_status": "OK" if not defects else "FAIL_CLOSED",
        "collection_defects": sorted(defects),
        "request_policy": asdict(policy),
        "excluded_boundary_counts": {
            "incomplete_1m_at_or_before_as_of": sum(
                item["excluded_boundary_counts"]["incomplete_1m_at_or_before_as_of"]
                for item in symbols
            ),
            "forming_1m_exact_as_of": sum(
                item["excluded_boundary_counts"]["forming_1m_exact_as_of"]
                for item in symbols
            ),
            "daily_on_trading_day": sum(
                item["excluded_boundary_counts"]["daily_on_trading_day"]
                for item in symbols
            ),
            "incomplete_1d_on_or_after_trading_day": sum(
                item["excluded_boundary_counts"]["incomplete_1d_on_or_after_trading_day"]
                for item in symbols
            ),
        },
        "capabilities": {
            "order_authority": False,
            "broker_access": False,
            "canonical_market_history_access": False,
        },
        "raw_manifest": raw_store.manifest(),
        "request_receipts": sorted(
            request_receipts,
            key=lambda item: (
                item["symbol"],
                item["interval"],
                item.get("before") or "",
                item["content_sha256"],
            ),
        ),
        "symbols": sorted(symbols, key=lambda item: item["symbol"]),
    }
    bundle["artifact_sha256"] = sha256_payload(bundle)
    return bundle


def write_market_bundle(bundle: Mapping[str, Any], out_json: str | Path,
                        receipt: str | Path | None = None) -> dict[str, Any]:
    sanitized = {
        "schema": MARKET_RECEIPT_SCHEMA,
        "artifact_sha256": bundle["artifact_sha256"],
        "collection_status": bundle["collection_status"],
        "collection_defects": bundle["collection_defects"],
        "raw_content_sha256": bundle["raw_manifest"]["raw_content_sha256"],
        "request_count": len(bundle["request_receipts"]),
        "excluded_boundary_counts": bundle["excluded_boundary_counts"],
        "stage": bundle["stage"],
        "trading_day": bundle["trading_day"],
        "as_of": bundle["as_of"],
    }
    receipt_path = receipt or f"{out_json}.receipt.json"
    write_immutable_pair((out_json, json_bytes(bundle)), (receipt_path, json_bytes(sanitized)))
    return sanitized
