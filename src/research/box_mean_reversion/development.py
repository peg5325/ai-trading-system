"""5분봉 박스권 RSI·거래량 평균회귀 development-only 단일 실행."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.research.box_mean_reversion.contract import (
    PREREG_JSON_SHA256,
    PREREG_MD_SHA256,
    development_days,
    fold_boundaries,
    load_contract,
)
from src.research.box_mean_reversion.indicators import ema, exact_median, wilder_atr, wilder_rsi
from src.research.box_mean_reversion.loading import complete_regular_5m, load_symbol_day
from src.research.orb.identity import require_identity, verify_snapshot_identity
from src.research.orb.simulator import detect_data_defects

RESULT_SCHEMA = "p0-us30-box-rsi-volume-mean-reversion-development/v1"
TRACKED_MANIFEST = "docs/research/P0_US30_ORB_CLEAN_1M_MANIFEST.json"


@dataclass(frozen=True)
class Candidate:
    symbol: str
    day: date
    signal_index: int
    entry_index: int
    signal_ts: datetime
    entry_ts: datetime
    entry_price: float
    stop: float
    target: float
    atr: float
    rsi: float
    volume_multiple: float


@dataclass(frozen=True)
class GrossTrade:
    symbol: str
    day: date
    signal_ts: datetime
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    qty: int
    exit_reason: str
    rsi: float
    volume_multiple: float


def _minute(bar: Any) -> int:
    return bar.ts_exchange.hour * 60 + bar.ts_exchange.minute


def detect_candidate(symbol: str, day: date, bars: Sequence[Any], contract: Mapping[str, Any]
                     ) -> tuple[Candidate | None, Counter[str]]:
    signal = contract["signal"]
    eligibility = contract["eligibility"]
    closes = [float(b.close) for b in bars]
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    volumes = [float(b.volume) for b in bars]
    atrs = wilder_atr(highs, lows, closes, int(signal["atr_period"]))
    rsis = wilder_rsi(closes, int(signal["rsi_period"]))
    emas = ema(closes, int(signal["ema_period"]))
    reasons: Counter[str] = Counter()
    box_n = int(signal["box_lookback_bars"])
    vol_n = int(signal["volume_lookback_bars"])
    slope_n = int(signal["ema_slope_lookback_bars"])
    start = max(box_n, vol_n, int(signal["ema_period"]), 14, slope_n)
    for i in range(start, len(bars) - 1):
        minute = _minute(bars[i])
        if not (int(eligibility["minimum_signal_minute_et"]) <= minute
                <= int(eligibility["maximum_signal_minute_et"])):
            continue
        atr = atrs[i]
        rsi = rsis[i]
        if atr is None or atr <= 0 or rsi is None:
            reasons["indicator_not_ready"] += 1
            continue
        box = bars[i - box_n:i]
        box_high = max(float(b.high) for b in box)
        box_low = min(float(b.low) for b in box)
        width = box_high - box_low
        if width <= 0 or width > float(signal["box_width_max_atr_multiple"]) * atr:
            reasons["box_width"] += 1
            continue
        current = bars[i]
        if float(current.low) > box_low + width * float(signal["box_lower_touch_fraction"]):
            reasons["no_lower_touch"] += 1
            continue
        if float(current.close) <= box_low + width * float(signal["box_reclaim_fraction"]):
            reasons["no_reclaim"] += 1
            continue
        if rsi > float(signal["rsi_max"]):
            reasons["rsi"] += 1
            continue
        med = exact_median(volumes[i - vol_n:i])
        multiple = volumes[i] / med if med > 0 else math.inf
        if multiple < float(signal["volume_min_median_multiple"]):
            reasons["volume"] += 1
            continue
        if emas[i] - emas[i - slope_n] < float(signal["ema_slope_min_atr_multiple"]) * atr:
            reasons["ema_slope"] += 1
            continue
        if closes[i] < emas[i] + float(signal["close_min_ema_distance_atr_multiple"]) * atr:
            reasons["ema_distance"] += 1
            continue
        entry = float(bars[i + 1].open)
        stop = box_low - float(contract["exit"]["stop_atr_below_box"]) * atr
        target = (box_low + box_high) / 2.0
        if stop >= entry or target <= entry:
            reasons["invalid_geometry"] += 1
            continue
        return Candidate(
            symbol=symbol, day=day, signal_index=i, entry_index=i + 1,
            signal_ts=bars[i].ts_utc, entry_ts=bars[i + 1].ts_utc,
            entry_price=entry, stop=stop, target=target, atr=atr, rsi=float(rsi),
            volume_multiple=multiple,
        ), reasons
    reasons["no_signal"] += 1
    return None, reasons


def close_candidate(candidate: Candidate, bars: Sequence[Any], contract: Mapping[str, Any]
                    ) -> tuple[datetime, float, str]:
    max_bars = int(contract["exit"]["maximum_holding_bars"])
    held = 0
    for bar in bars[candidate.entry_index:]:
        held += 1
        open_ = float(bar.open)
        if open_ <= candidate.stop:
            return bar.ts_utc, open_, "stop_gap"
        if open_ >= candidate.target:
            return bar.ts_utc, open_, "target_gap"
        stop_hit = float(bar.low) <= candidate.stop
        target_hit = float(bar.high) >= candidate.target
        if stop_hit:
            return bar.ts_utc, candidate.stop, "stop" if not target_hit else "stop_first"
        if target_hit:
            return bar.ts_utc, candidate.target, "target"
        if held >= max_bars:
            return bar.ts_utc, float(bar.close), "time_exit"
    last = bars[-1]
    return last.ts_utc, float(last.close), "eod"


def _allocate(candidates: Sequence[tuple[Candidate, Sequence[Any]]], contract: Mapping[str, Any]
              ) -> tuple[list[GrossTrade], Counter[str]]:
    portfolio = contract["portfolio"]
    initial = float(portfolio["initial_capital"])
    risk_budget = initial * float(portfolio["risk_fraction_per_trade"])
    notional_cap = initial * float(portfolio["notional_cap_fraction_per_trade"])
    max_positions = int(portfolio["max_positions"])
    grouped: dict[datetime, list[tuple[Candidate, Sequence[Any]]]] = defaultdict(list)
    for item in candidates:
        grouped[item[0].entry_ts].append(item)
    open_until: list[datetime] = []
    trades: list[GrossTrade] = []
    skipped: Counter[str] = Counter()
    for entry_ts in sorted(grouped):
        open_until = [ts for ts in open_until if ts > entry_ts]
        ranked = sorted(grouped[entry_ts], key=lambda item: (
            -item[0].volume_multiple, item[0].rsi, item[0].symbol))
        for candidate, bars in ranked:
            if len(open_until) >= max_positions:
                skipped["slot_cap"] += 1
                continue
            per_share = candidate.entry_price - candidate.stop
            qty = math.floor(min(risk_budget / per_share, notional_cap / candidate.entry_price))
            if qty < 1:
                skipped["qty_lt_1"] += 1
                continue
            exit_ts, exit_price, reason = close_candidate(candidate, bars, contract)
            trades.append(GrossTrade(
                symbol=candidate.symbol, day=candidate.day,
                signal_ts=candidate.signal_ts, entry_ts=candidate.entry_ts, exit_ts=exit_ts,
                entry_price=candidate.entry_price, exit_price=exit_price, qty=qty,
                exit_reason=reason, rsi=candidate.rsi,
                volume_multiple=candidate.volume_multiple,
            ))
            open_until.append(exit_ts)
    return trades, skipped


def _net(trade: GrossTrade, pct_points: float) -> float:
    rate = pct_points / 100.0
    gross = (trade.exit_price - trade.entry_price) * trade.qty
    cost = (trade.entry_price * trade.qty + trade.exit_price * trade.qty) * rate / 2.0
    return gross - cost


def _stats(trades: Sequence[GrossTrade], days: Sequence[date], folds: Sequence[tuple[str, date, date]],
           pct_points: float, initial: float) -> dict[str, Any]:
    pnls = [_net(t, pct_points) for t in trades]
    by_day: dict[date, float] = defaultdict(float)
    for trade, pnl in zip(trades, pnls):
        by_day[trade.day] += pnl
    cash = initial
    peak = initial
    mdd = 0.0
    for day in days:
        cash += by_day.get(day, 0.0)
        peak = max(peak, cash)
        if peak > 0:
            mdd = max(mdd, (peak - cash) / peak)
    gains = sum(v for v in pnls if v > 0)
    losses = sum(v for v in pnls if v < 0)
    fold_pnl = {
        name: sum(pnl for trade, pnl in zip(trades, pnls) if start <= trade.day <= end)
        for name, start, end in folds
    }
    total = sum(pnls)
    return {
        "trade_count": len(trades),
        "trading_days_with_trade": len(by_day),
        "cumulative_net_pnl": total,
        "net_expectancy": total / len(trades) if trades else 0.0,
        "profit_factor": gains / abs(losses) if losses else "Infinity",
        "mdd": mdd,
        "positive_folds": sum(v > 0 for v in fold_pnl.values()),
        "fold_pnl": fold_pnl,
    }


def _gate(stats: Mapping[str, Mapping[str, Any]], defects: Mapping[str, Any],
          contract: Mapping[str, Any]) -> dict[str, Any]:
    g = contract["development_gate"]
    official = stats["official"]
    stress = stats["stress"]
    pf = official["profit_factor"]
    items = {
        "D1": official["trade_count"] >= int(g["D1_min_trades"]),
        "D2": official["trading_days_with_trade"] >= int(g["D2_min_trading_days"]),
        "D3": official["net_expectancy"] > float(g["D3_official_expectancy_gt"]),
        "D4": (
            pf == "Infinity"
            or float(pf) >= float(g["D4_official_profit_factor_gte"])
        ),
        "D5": official["mdd"] <= float(g["D5_official_mdd_lte"]),
        "D6": official["positive_folds"] >= int(g["D6_min_positive_folds"]),
        "D7": stress["cumulative_net_pnl"] >= float(g["D7_stress_cumulative_net_gte"]),
        "D9": (
            defects["incomplete_symbol_day_ratio"]
            <= float(g["D9_max_incomplete_symbol_day_ratio"])
            and defects["new_conflicts"] <= int(g["D9_max_new_conflicts"])
            and defects["major_defects"] == 0
        ),
    }
    failed = [name for name, passed in items.items() if not passed]
    if not items["D9"]:
        verdict = "INCONCLUSIVE_DATA"
    elif failed:
        verdict = "NO_SELECTION"
    else:
        verdict = "GO_CANDIDATE"
    return {"items": items, "failed": failed, "verdict": verdict}


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def run(snapshot_path: str, raw_root: str) -> dict[str, Any]:
    contract = load_contract()
    identity = verify_snapshot_identity(
        contract=contract, snapshot_path=snapshot_path, raw_root=raw_root,
        manifest_path=TRACKED_MANIFEST,
    )
    require_identity(identity)
    days = development_days(contract)
    symbols = tuple(contract["universe"])
    candidates: list[tuple[Candidate, Sequence[Any]]] = []
    exclusions: Counter[str] = Counter()
    filters: Counter[str] = Counter()
    incomplete: set[tuple[str, date]] = set()
    major: set[tuple[str, date, str]] = set()
    new_conflicts = 0
    for day in days:
        for symbol in symbols:
            candles = load_symbol_day(snapshot_path, symbol, day)
            raw_defects = detect_data_defects(candles)
            if raw_defects:
                major.update((symbol, day, reason) for reason in raw_defects)
                exclusions.update(raw_defects)
                continue
            bars, defects = complete_regular_5m(candles, day, symbol)
            if defects:
                incomplete.add((symbol, day))
                exclusions.update(defects)
                new_conflicts += int("duplicate_timestamp_value_conflict" in defects)
                continue
            candidate, reasons = detect_candidate(symbol, day, bars, contract)
            filters.update(reasons)
            if candidate is not None:
                candidates.append((candidate, bars))
    trades, skipped = _allocate(candidates, contract)
    costs = contract["costs"]["round_trip_pct_points"]
    folds = fold_boundaries(contract)
    stats = {
        level: _stats(
            trades, days, folds, float(pct), float(contract["portfolio"]["initial_capital"]))
        for level, pct in costs.items()
    }
    total_symbol_days = len(days) * len(symbols)
    defects = {
        "incomplete_symbol_days": len(incomplete),
        "total_symbol_days": total_symbol_days,
        "incomplete_symbol_day_ratio": len(incomplete) / total_symbol_days,
        "new_conflicts": new_conflicts,
        "major_defects": len(major),
    }
    gate = _gate(stats, defects, contract)
    identity_payload = [asdict(check) for check in identity.checks]
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "strategy_id": contract["strategy_id"],
        "scope": "development-only",
        "blind_oos_opened": False,
        "prereg": {"json_sha256": PREREG_JSON_SHA256, "md_sha256": PREREG_MD_SHA256},
        "identity": identity_payload,
        "window": {"start": days[0].isoformat(), "end": days[-1].isoformat(),
                   "trading_days": len(days)},
        "candidate_count_before_portfolio": len(candidates),
        "trade_count": len(trades),
        "stats": stats,
        "defects": defects,
        "gate": gate,
        "exit_reasons": dict(sorted(Counter(t.exit_reason for t in trades).items())),
        "exclusions": dict(sorted(exclusions.items())),
        "filters": dict(sorted(filters.items())),
        "portfolio_skips": dict(sorted(skipped.items())),
        "trades_sha256": hashlib.sha256(canonical_json_bytes({
            "trades": [{**asdict(t), "day": t.day.isoformat(),
                        "signal_ts": t.signal_ts.isoformat(), "entry_ts": t.entry_ts.isoformat(),
                        "exit_ts": t.exit_ts.isoformat()} for t in trades]
        })).hexdigest(),
    }
    result["payload_sha256"] = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    return result


def write_result(result: Mapping[str, Any], path: str) -> None:
    Path(path).write_bytes(json.dumps(
        result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    ).encode("utf-8") + b"\n")
