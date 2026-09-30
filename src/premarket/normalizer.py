"""Pure provider+market bundle normalization for premarket-input/v2."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.bars.resample import resample_1m
from src.engines.indicators import ema
from src.history.exchange_calendar import regular_close_minute
from src.models import Candle
from src.premarket.contract import INPUT_SCHEMA_V2, sha256_payload
from src.premarket.schedule import build_premarket_schedule, parse_iso

ET = ZoneInfo("America/New_York")


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


def _percentiles(values: Mapping[str, float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    n = len(ordered)
    if n == 1:
        return {ordered[0][0]: 1.0}
    by_value: defaultdict[float, list[int]] = defaultdict(list)
    for idx, (_symbol, value) in enumerate(ordered):
        by_value[value].append(idx)
    rank_by_value = {
        value: (sum(indices) / len(indices)) / (n - 1)
        for value, indices in by_value.items()
    }
    return {symbol: rank_by_value[value] for symbol, value in ordered}


def _bar_dt(row: Mapping[str, Any]) -> datetime:
    return parse_iso(str(row["ts_utc"]))


def _previous_close(daily: list[Mapping[str, Any]], trading_day: str) -> float | None:
    prior = [row for row in daily if str(row["exchange_date"]) < trading_day]
    if not prior:
        return None
    return float(max(prior, key=lambda row: str(row["exchange_date"]))["close"])


def _daily_regular_close_iso(exchange_date: str) -> str:
    day = date.fromisoformat(exchange_date)
    close_minute = regular_close_minute(day)
    if close_minute is None:
        raise ValueError(f"regular close unavailable: {exchange_date}")
    dt = datetime.combine(day, time(close_minute // 60, close_minute % 60), tzinfo=ET)
    return _iso_z(dt)


def _candle(row: Mapping[str, Any]) -> Candle:
    return Candle(
        symbol=str(row["symbol"]),
        interval=str(row["interval"]),
        ts_utc=parse_iso(str(row["ts_utc"])),
        ts_exchange=datetime.fromisoformat(str(row["ts_exchange"]).replace("Z", "+00:00")),
        session=str(row["session"]),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
        currency=str(row["currency"]),
        adjusted=bool(row["adjusted"]),
        market=str(row.get("market", "US")),
    )


def _derive_5m(one_minute: list[Mapping[str, Any]], *, as_of: str) -> list[Candle]:
    candles = [_candle(row) for row in sorted(one_minute, key=_bar_dt)]
    return resample_1m(
        candles,
        5,
        min_bars=5,
        closed_only=True,
        evaluation_time=parse_iso(as_of),
    )


def _trend_score(bars_5m: list[Candle]) -> float | None:
    closes = [float(row.close) for row in bars_5m]
    ema7 = ema(pd.Series(closes, dtype="float64"), 7)
    ema20 = ema(pd.Series(closes, dtype="float64"), 20)
    if not closes or pd.isna(ema7.iloc[-1]) or pd.isna(ema20.iloc[-1]):
        return None
    return (0.5 if closes[-1] > float(ema20.iloc[-1]) else 0.0) + (
        0.5 if float(ema7.iloc[-1]) > float(ema20.iloc[-1]) else 0.0
    )


def _volume_score(bars_5m: list[Candle]) -> float | None:
    if len(bars_5m) < 21:
        return None
    latest = float(bars_5m[-1].volume)
    baseline = sum(float(row.volume) for row in bars_5m[-21:-1]) / 20.0
    if baseline <= 0:
        return None
    ratio = latest / baseline
    if ratio < 0.7:
        return 0.3
    if ratio < 1.0:
        return 0.5
    if ratio < 1.5:
        return 0.7
    return 0.9


def _provider_source_evidence_time(
    provider: Mapping[str, Any],
    *,
    kind: str,
    as_of: str,
) -> str:
    source = provider.get("source") or {}
    if kind in {"NEWS", "FILING"} and source.get("observed_at"):
        return str(source["observed_at"])
    item_time_field = {"NEWS": "published_at", "FILING": "accepted_at"}.get(kind)
    if item_time_field is not None:
        times = [
            str(item[item_time_field])
            for item in provider.get("items") or []
            if item.get("item_type") == kind and item.get(item_time_field)
        ]
        if times:
            return max(times)
        # Empty NEWS/FILING queries are evidence that no matching provider item
        # was available at the cutoff.  Use as_of for the query evidence only;
        # actual HTTP collection time remains ingested_at=generated_at.
        return as_of
    # MACRO providers enforce series freshness before bundling and may have
    # receipt times after the cutoff, so represent the normalized source as a
    # point-in-time as_of snapshot instead of falsifying NEWS/FILING evidence.
    return as_of


def _provider_sources(provider_bundle: Mapping[str, Any], generated_at: str) -> list[dict[str, Any]]:
    out = []
    for provider in provider_bundle.get("providers", []):
        source = dict(provider.get("source") or {})
        if not source:
            continue
        kind = str(source.get("kind") or provider.get("kind")).upper()
        observed = _provider_source_evidence_time(
            provider,
            kind=kind,
            as_of=str(provider_bundle["as_of"]),
        )
        out.append({
            "source_id": str(source.get("source_id") or provider["provider"]),
            "kind": kind,
            "status": str(provider.get("status") or source.get("status") or "ERROR").upper(),
            "observed_at": observed,
            "available_at": observed,
            "ingested_at": generated_at,
            "source_url": str(source.get("source_url") or "https://evidence.invalid/provider"),
            "content_sha256": str(source.get("content_sha256") or provider_bundle["artifact_sha256"]),
        })
    return out


def _events(provider_bundle: Mapping[str, Any], allowed: set[str], as_of: str,
            generated_at: str) -> list[dict[str, Any]]:
    cutoff = parse_iso(as_of)
    recent_filing_cutoff = cutoff - timedelta(hours=24)
    out = []
    seen: dict[str, dict[str, Any]] = {}
    for provider in provider_bundle.get("providers", []):
        source = provider.get("source") or {}
        source_id = str(source.get("source_id") or provider.get("provider"))
        for item in provider.get("items") or []:
            item_type = str(item.get("item_type") or "").upper()
            if item_type == "NEWS":
                available = str(item.get("published_at"))
                if parse_iso(available) > cutoff:
                    continue
                symbols = sorted({str(sym).upper() for sym in item.get("symbols", []) if str(sym).upper() in allowed})
                if not symbols:
                    continue
                event = {
                    "event_id": str(item["item_id"]),
                    "source_id": source_id,
                    "published_at": available,
                    "available_at": available,
                    "ingested_at": generated_at,
                    "source_url": str(item["source_url"]),
                    "content_sha256": str(item["content_sha256"]),
                    "symbols": symbols,
                    "event_kind": "NEWS",
                    "sentiment": _clip(float(item.get("sentiment", 0.0)), -1.0, 1.0),
                }
            elif item_type == "FILING":
                available = str(item.get("accepted_at"))
                accepted = parse_iso(available)
                if accepted > cutoff or accepted < recent_filing_cutoff:
                    continue
                form = str(item.get("form") or "")
                if form not in {"8-K", "10-Q", "10-K"}:
                    continue
                symbols = sorted({str(sym).upper() for sym in item.get("symbols", []) if str(sym).upper() in allowed})
                if not symbols:
                    continue
                event = {
                    "event_id": str(item["item_id"]),
                    "source_id": source_id,
                    "published_at": available,
                    "available_at": available,
                    "ingested_at": generated_at,
                    "source_url": str(item["source_url"]),
                    "content_sha256": str(item["content_sha256"]),
                    "symbols": symbols,
                    "event_kind": "FILING",
                    "sentiment": 0.0,
                }
            else:
                continue
            prior = seen.get(event["event_id"])
            if prior is not None and prior != event:
                raise ValueError(f"conflicting event revision: {event['event_id']}")
            seen[event["event_id"]] = event
            out.append(event)
    return sorted({event["event_id"]: event for event in out}.values(), key=lambda item: item["event_id"])


def _vix_value(provider_bundle: Mapping[str, Any]) -> float | None:
    values = []
    for provider in provider_bundle.get("providers", []):
        for item in provider.get("items") or []:
            if item.get("item_type") == "MACRO" and item.get("series_id") == "VIXCLS":
                try:
                    values.append((str(item.get("observation_date")), float(item.get("value"))))
                except (TypeError, ValueError):
                    continue
    if not values:
        return None
    return max(values)[1]


def normalize_bundles(provider_bundle: Mapping[str, Any], market_bundle: Mapping[str, Any], *,
                      strategy_template_id: str | None = None) -> dict[str, Any]:
    """``strategy_template_id`` is attached to every candidate; callers pass
    ``attachable_template(config)`` so it is None unless the config approves it."""
    if provider_bundle.get("schema") != "premarket-provider-bundle/v1":
        raise ValueError("provider bundle must be premarket-provider-bundle/v1")
    if market_bundle.get("schema") != "premarket-market-bundle/v1":
        raise ValueError("market bundle must be premarket-market-bundle/v1")
    for field in ("trading_day", "stage", "as_of"):
        if str(provider_bundle.get(field)) != str(market_bundle.get(field)):
            raise ValueError(f"bundle {field} mismatch")
    trading_day = str(provider_bundle["trading_day"])
    as_of = str(provider_bundle["as_of"])
    generated_at = str(market_bundle["generated_at"])
    run_mode = _run_mode(
        trading_day=trading_day,
        stage=str(provider_bundle["stage"]),
        as_of=as_of,
        generated_at=generated_at,
    )
    allowed = {str(symbol) for symbol in provider_bundle.get("symbols", [])}
    market_by_symbol = {item["symbol"]: item for item in market_bundle.get("symbols", [])}
    sources = _provider_sources(provider_bundle, generated_at)
    market_source_id = f"market-toss-{market_bundle['stage'].lower()}"
    latest_market_available: str | None = None
    for item in market_by_symbol.values():
        candles = item.get("candles_1m") or []
        if candles:
            latest = max(
                _iso_z(parse_iso(str(row["ts_utc"])) + timedelta(minutes=1))
                for row in candles
            )
            latest_market_available = (
                latest if latest_market_available is None else max(latest_market_available, latest)
            )
        daily = item.get("candles_1d") or []
        if daily:
            latest_daily = max(_daily_regular_close_iso(str(row["exchange_date"])) for row in daily)
            latest_market_available = (
                latest_daily
                if latest_market_available is None
                else max(latest_market_available, latest_daily)
            )
    market_available = min(latest_market_available or as_of, as_of)
    sources.append({
        "source_id": market_source_id,
        "kind": "MARKET",
        "status": "OK" if market_bundle.get("collection_status") == "OK" else "ERROR",
        "observed_at": market_available,
        "available_at": market_available,
        "ingested_at": generated_at,
        "source_url": "https://openapi.tossinvest.com/api/v1/candles",
        "content_sha256": str(market_bundle["artifact_sha256"]),
    })
    if provider_bundle.get("collection_status") != "OK" or market_bundle.get("collection_status") != "OK":
        result = {
            "schema": INPUT_SCHEMA_V2,
            "market": "US",
            "trading_day": trading_day,
            "stage": str(provider_bundle["stage"]),
            "as_of": as_of,
            "generated_at": generated_at,
            "sources": sorted(sources, key=lambda item: (item["source_id"], item["kind"])),
            "events": [],
            "candidates": [],
            "market_regime": {
                "label": "UNCERTAIN",
                "available_at": as_of,
                "ingested_at": generated_at,
                "source_ids": [market_source_id],
            },
            "provenance": {
                "provider_bundle_sha256": provider_bundle["artifact_sha256"],
                "market_bundle_sha256": market_bundle["artifact_sha256"],
                "fail_closed_defects": list(provider_bundle.get("collection_defects", []))
                + list(market_bundle.get("collection_defects", [])),
                "run_mode": run_mode,
            },
        }
        result["artifact_sha256"] = sha256_payload(result)
        return result
    events = _events(provider_bundle, allowed, as_of, generated_at)
    events_by_symbol: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        for symbol in event["symbols"]:
            events_by_symbol[symbol].append(event)

    returns: dict[str, float] = {}
    liquidity_values: dict[str, float] = {}
    features: dict[str, dict[str, Any]] = {}
    for symbol, item in market_by_symbol.items():
        one_minute = sorted(item.get("candles_1m") or [], key=lambda row: row["ts_utc"])
        daily = sorted(item.get("candles_1d") or [], key=lambda row: row["exchange_date"])
        if not one_minute or len(daily) < 20:
            continue
        previous_close = _previous_close(daily, trading_day)
        if previous_close is None or previous_close <= 0:
            continue
        latest_close = float(one_minute[-1]["close"])
        returns[symbol] = latest_close / previous_close - 1.0
        dollar_values = [float(row["close"]) * float(row["volume"]) for row in daily[-20:]]
        liquidity_values[symbol] = float(median(dollar_values))
        bars_5m = _derive_5m(one_minute, as_of=as_of)
        latest_1m_available_at = _iso_z(_bar_dt(one_minute[-1]) + timedelta(minutes=1))
        features[symbol] = {
            "bars_5m": bars_5m,
            "previous_close": previous_close,
            "latest_1m_available_at": latest_1m_available_at,
            "daily_available_at": _daily_regular_close_iso(str(daily[-1]["exchange_date"])),
        }
    rs_percentile = _percentiles(returns)
    liquidity_percentile = _percentiles(liquidity_values)

    candidates = []
    for symbol in sorted(allowed):
        symbol_events = events_by_symbol.get(symbol, [])
        if not symbol_events or symbol not in features:
            continue
        trend = _trend_score(features[symbol]["bars_5m"])
        volume = _volume_score(features[symbol]["bars_5m"])
        if trend is None or volume is None or symbol not in rs_percentile or symbol not in liquidity_percentile:
            continue
        news_sentiments = [
            float(event["sentiment"])
            for event in symbol_events
            if event.get("event_kind") == "NEWS"
        ]
        catalyst = _clip(sum(news_sentiments) / len(news_sentiments), -1.0, 1.0) if news_sentiments else 0.0
        available_at = max(
            [features[symbol]["latest_1m_available_at"], features[symbol]["daily_available_at"]]
            + [event["available_at"] for event in symbol_events]
        )
        candidates.append({
            "symbol": symbol,
            "strategy_template_id": strategy_template_id,
            "available_at": min(available_at, as_of),
            "ingested_at": generated_at,
            "evidence_event_ids": sorted(event["event_id"] for event in symbol_events),
            "source_ids": sorted({market_source_id, *(event["source_id"] for event in symbol_events)}),
            "catalyst_score": catalyst,
            "trend_score": trend,
            "relative_strength_score": rs_percentile[symbol],
            "volume_score": volume,
            "liquidity_score": liquidity_percentile[symbol],
        })

    regime = _regime(market_by_symbol, provider_bundle, market_source_id, generated_at, as_of)
    result = {
        "schema": INPUT_SCHEMA_V2,
        "market": "US",
        "trading_day": trading_day,
        "stage": str(provider_bundle["stage"]),
        "as_of": as_of,
        "generated_at": generated_at,
        "sources": sorted(sources, key=lambda item: (item["source_id"], item["kind"])),
        "events": events,
        "candidates": candidates,
        "market_regime": regime,
        "provenance": {
            "provider_bundle_sha256": provider_bundle["artifact_sha256"],
            "market_bundle_sha256": market_bundle["artifact_sha256"],
            "market_excluded_boundary_counts": market_bundle.get("excluded_boundary_counts", {}),
            "run_mode": run_mode,
        },
    }
    result["artifact_sha256"] = sha256_payload(result)
    return result


def _regime(
    market_by_symbol: Mapping[str, Mapping[str, Any]],
    provider_bundle: Mapping[str, Any],
    market_source_id: str,
    generated_at: str,
    as_of: str,
) -> dict[str, Any]:
    source_ids = [market_source_id]
    vix = _vix_value(provider_bundle)
    label = "UNCERTAIN"
    available_at = as_of
    try:
        closes = {}
        sma = {}
        dates = []
        for symbol in ("SPY", "QQQ"):
            daily = sorted(market_by_symbol[symbol]["candles_1d"], key=lambda row: row["exchange_date"])
            closes[symbol] = float(daily[-1]["close"])
            sma[symbol] = sum(float(row["close"]) for row in daily[-200:]) / 200.0
            dates.append(_daily_regular_close_iso(str(daily[-1]["exchange_date"])))
        available_at = min(max(dates), as_of)
        if vix is not None:
            source_ids.extend(
                str((provider.get("source") or {}).get("source_id"))
                for provider in provider_bundle.get("providers", [])
                if provider.get("provider") == "fred-alfred"
            )
            if closes["SPY"] > sma["SPY"] and closes["QQQ"] > sma["QQQ"] and vix < 25.0:
                label = "RISK_ON"
            elif (closes["SPY"] < sma["SPY"] and closes["QQQ"] < sma["QQQ"]) or vix >= 30.0:
                label = "RISK_OFF"
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
        label = "UNCERTAIN"
    return {
        "label": label,
        "available_at": available_at,
        "ingested_at": generated_at,
        "source_ids": sorted({source_id for source_id in source_ids if source_id}),
    }


def _run_mode(*, trading_day: str, stage: str, as_of: str, generated_at: str) -> str:
    schedule = build_premarket_schedule(date.fromisoformat(trading_day))
    expected_as_of = {
        "PRIMARY": schedule.primary_run_utc,
        "VERIFICATION": schedule.verification_utc,
        "FREEZE": schedule.freeze_utc,
    }.get(stage)
    stage_end = {
        "PRIMARY": schedule.verification_utc,
        "VERIFICATION": schedule.freeze_utc,
        "FREEZE": schedule.market_open_utc,
    }.get(stage)
    generated = parse_iso(generated_at)
    if expected_as_of == as_of and stage_end is not None and parse_iso(as_of) <= generated < parse_iso(stage_end):
        return "LIVE_WINDOW"
    return "RETROSPECTIVE_RECONSTRUCTION"
