"""Offline fixtures for the FREEZE Paper engine tests (synthetic bars, tmp_path files)."""
from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable
from datetime import date, datetime, time
from pathlib import Path

from src.freeze_paper.binding import load_binding
from src.freeze_paper.engine import Bar
from src.freeze_paper.gates import TRADE, GateCandidate, GateResult, freeze_paths
from src.models import ET
from src.premarket.contract import PremarketConfig, sha256_payload
from src.premarket.operations import OPERATION_SCHEMA
from src.premarket.planner import build_daily_brief
from src.premarket.report import json_bytes
from src.premarket.schedule import build_premarket_schedule, iso_z

BINDING = load_binding()
TEMPLATE = BINDING.strategy_template_id
BRIEF_DAY = "2026-03-09"
POLICY_SYMBOLS = ["AAPL", "AMD", "MSFT", "NVDA", "SOXL", "TQQQ", "TSLA"]


def _config() -> PremarketConfig:
    return PremarketConfig.from_mapping({
        "schema": "premarket-config/v1",
        "required_source_kinds": ["MARKET", "NEWS"],
        "source_freshness_minutes": 180,
        "source_freshness_minutes_by_kind": {},
        "primary_offset_minutes": 90,
        "verification_offset_minutes": 30,
        "freeze_offset_minutes": 20,
        "max_candidates": 3,
        "min_liquidity_score": 0.5,
        "min_catalyst_score": 0.0,
        "approved_strategy_templates": [TEMPLATE],
        "approved_short_strategy_templates": [TEMPLATE],
        "allowed_symbols": POLICY_SYMBOLS,
        "excluded_universe_classes": ["inverse_etf"],
        "weights": {"catalyst_score": 0.30, "trend_score": 0.25, "relative_strength_score": 0.20,
                    "volume_score": 0.15, "liquidity_score": 0.10},
    })


def _source(source_id: str, kind: str, minute: str, digest: str) -> dict:
    return {"source_id": source_id, "kind": kind, "status": "OK",
            "observed_at": f"2026-03-09T11:{minute}:00Z",
            "available_at": f"2026-03-09T11:{minute}:01Z",
            "ingested_at": f"2026-03-09T11:{minute}:02Z",
            "source_url": f"https://evidence.invalid/{source_id}", "content_sha256": digest * 64}


def _event(symbol: str, minute: str, digest: str) -> dict:
    return {"event_id": f"event-{symbol.lower()}", "source_id": "news-primary",
            "published_at": f"2026-03-09T11:{minute}:00Z",
            "available_at": f"2026-03-09T11:{minute}:01Z",
            "ingested_at": f"2026-03-09T11:{minute}:02Z",
            "source_url": f"https://evidence.invalid/news/{symbol.lower()}",
            "content_sha256": digest * 64, "symbols": [symbol], "sentiment": 0.6}


def _candidate(symbol: str, minute: str, scores: tuple[float, ...]) -> dict:
    fields = ("catalyst_score", "trend_score", "relative_strength_score", "volume_score",
              "liquidity_score")
    return {"symbol": symbol, "strategy_template_id": TEMPLATE,
            "available_at": f"2026-03-09T11:{minute}:00Z",
            "ingested_at": f"2026-03-09T11:{minute}:01Z",
            "evidence_event_ids": [f"event-{symbol.lower()}"],
            "source_ids": ["market-primary", "news-primary"], **dict(zip(fields, scores))}


def _input() -> dict:
    """premarket-input/v2 FREEZE input for 2026-03-09 (EDT: freeze 13:10Z, open 13:30Z)."""
    return {
        "schema": "premarket-input/v2", "market": "US", "trading_day": BRIEF_DAY,
        "stage": "FREEZE", "as_of": "2026-03-09T13:10:00Z",
        "generated_at": "2026-03-09T13:10:30Z", "provenance": {"run_mode": "LIVE_WINDOW"},
        "sources": [_source("market-primary", "MARKET", "58", "a"),
                    _source("news-primary", "NEWS", "55", "b")],
        "events": [_event("AAPL", "40", "c"), _event("MSFT", "42", "d")],
        "candidates": [_candidate("AAPL", "50", (0.9, 0.8, 0.7, 0.8, 0.9)),
                       _candidate("MSFT", "51", (0.6, 0.7, 0.8, 0.6, 0.95))],
        "market_regime": {"label": "RISK_ON", "available_at": "2026-03-09T11:59:00Z",
                          "ingested_at": "2026-03-09T11:59:01Z", "source_ids": ["market-primary"]},
    }


def minute(day: str, hh: int, mm: int) -> str:
    return iso_z(datetime.combine(date.fromisoformat(day), time(hh, mm), tzinfo=ET))


def flat(price: float) -> Bar:
    return Bar(price, price, price, price)


def schedule(day: str) -> dict:
    return build_premarket_schedule(date.fromisoformat(day)).to_dict()


def trade_gate(day: str, symbols: list[str], leg: str = "LONG",
               signal_symbols: list[str] | None = None) -> GateResult:
    signals = signal_symbols or symbols
    bias = "LONG_WATCH" if leg == "LONG" else "SHORT_WATCH"
    return GateResult(
        day, TRADE, (), leg=leg,
        candidates=tuple(GateCandidate(i, sig, bias, leg, sym)
                         for i, (sig, sym) in enumerate(zip(signals, symbols))),
        schedule=schedule(day), contract_sha256=BINDING.sha256)


def session_bars(day: str, prices: dict[str, float],
                 overrides: dict[tuple[int, int], dict[str, Bar | None]] | None = None,
                 end: tuple[int, int] = (16, 0)) -> dict[str, dict[str, Bar]]:
    """Flat bars for every regular minute 09:30..end (exclusive); overrides replace
    (or with None remove) a symbol's bar at an ET (hour, minute)."""
    out: dict[str, dict[str, Bar]] = {}
    hh, mm = 9, 30
    while (hh, mm) < end:
        bars = {sym: flat(p) for sym, p in prices.items()}
        for sym, bar in (overrides or {}).get((hh, mm), {}).items():
            if bar is None:
                bars.pop(sym, None)
            else:
                bars[sym] = bar
        out[minute(day, hh, mm)] = bars
        mm += 1
        if mm == 60:
            hh, mm = hh + 1, 0
    return out


def planner_artifact() -> dict:
    """A real premarket-daily-brief/v2 FREEZE artifact built by src.premarket.planner."""
    return build_daily_brief(_input(),
                             schedule=build_premarket_schedule(date.fromisoformat(BRIEF_DAY)),
                             config=_config())


def watch_item(symbol: str, bias: str = "LONG_WATCH", *, template: str | None = TEMPLATE,
               approved: bool = True) -> dict:
    return {"symbol": symbol, "bias": bias, "score": 0.5, "strategy_template_id": template,
            "strategy_template_approved": approved, "evidence": []}


def write_freeze(reports: Path, *, regime: str | None = None, watchlist: list[dict] | None = None,
                 mutate: Callable[[dict], None] | None = None,
                 mutate_receipt: Callable[[dict], None] | None = None,
                 resign: bool = True, receipt_status: str = "COMPLETE",
                 day: str = BRIEF_DAY) -> dict:
    """Write brief + COMPLETE receipt for ``day``; mutations are re-signed unless resign=False."""
    artifact = planner_artifact()
    if regime is not None:
        artifact["snapshot"]["payload"]["input"]["market_regime"]["label"] = regime
    if watchlist is not None:
        artifact["brief"]["watchlist"] = copy.deepcopy(watchlist)
    if mutate is not None:
        mutate(artifact)
    if resign:
        artifact.pop("artifact_sha256", None)
        artifact["artifact_sha256"] = sha256_payload(artifact)
    brief_path, receipt_path = freeze_paths(date.fromisoformat(day), reports)
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    content = json_bytes(artifact)
    brief_path.write_bytes(content)
    receipt = {
        "schema": OPERATION_SCHEMA, "operation": "PREMARKET_STAGE", "trading_day": day,
        "stage": "FREEZE", "status": receipt_status, "reason_codes": [],
        "brief_status": artifact["brief"].get("status"),
        "artifact_file_sha256": {"brief": hashlib.sha256(content).hexdigest()},
        "steps": [],
    }
    if mutate_receipt is not None:
        mutate_receipt(receipt)
    receipt["artifact_sha256"] = sha256_payload(receipt)
    receipt_path.write_bytes(json_bytes(receipt))
    return artifact
