"""오전 강세·점심 눌림·오후 재상승 development 단일 실행."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.research.afternoon_continuation.contract import (
    PREREG_JSON_SHA256,
    PREREG_MD_SHA256,
    data_days,
    evaluation_days,
    fold_boundaries,
    load_contract,
)
from src.research.afternoon_continuation.indicators import (
    cumulative_vwap,
    ema,
    exact_median,
    wilder_atr,
)
from src.research.afternoon_continuation.loading import (
    complete_regular_5m,
    load_symbol_day,
)
from src.research.orb.identity import require_identity, verify_snapshot_identity
from src.research.orb.simulator import detect_data_defects

RESULT_SCHEMA = "p0-us30-afternoon-continuation-development/v1"
TRACKED_MANIFEST = "docs/research/P0_US30_ORB_CLEAN_1M_MANIFEST.json"


@dataclass(frozen=True)
class Candidate:
    symbol: str
    day: date
    entry_index: int
    signal_ts: datetime
    entry_ts: datetime
    entry_price: float
    stop: float
    target: float
    morning_return: float
    morning_volume_multiple: float


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
    morning_return: float
    morning_volume_multiple: float


def _minute(bar: Any) -> int:
    return bar.ts_exchange.hour * 60 + bar.ts_exchange.minute


def _completion_minute(bar: Any) -> int:
    """Return completion minute for canonical start-labelled 5m bars."""
    return _minute(bar) + 5


def morning_volume(bars: Sequence[Any], contract: Mapping[str, Any]) -> float:
    eligibility = contract["eligibility"]
    start = int(eligibility["morning_start_minute_et"])
    end = int(eligibility["morning_end_exclusive_minute_et"])
    selected = [bar for bar in bars if start <= _minute(bar) < end]
    if len(selected) != (end - start) // 5:
        raise ValueError("오전 60분 완전 5분봉이 아니다")
    return sum(float(bar.volume) for bar in selected)


def detect_candidate(symbol: str, day: date, bars: Sequence[Any], baseline_volume: float,
                     contract: Mapping[str, Any]) -> tuple[Candidate | None, Counter[str]]:
    signal = contract["signal"]
    eligibility = contract["eligibility"]
    closes = [float(bar.close) for bar in bars]
    highs = [float(bar.high) for bar in bars]
    lows = [float(bar.low) for bar in bars]
    volumes = [float(bar.volume) for bar in bars]
    emas = ema(closes, int(signal["ema_period"]))
    atrs = wilder_atr(highs, lows, closes, int(signal["atr_period"]))
    vwaps = cumulative_vwap(highs, lows, closes, volumes)
    reasons: Counter[str] = Counter()

    morning_start = int(eligibility["morning_start_minute_et"])
    morning_end = int(eligibility["morning_end_exclusive_minute_et"])
    morning_indices = [i for i, bar in enumerate(bars) if morning_start <= _minute(bar) < morning_end]
    if len(morning_indices) != 12:
        reasons["incomplete_morning_window"] += 1
        return None, reasons
    first, last = morning_indices[0], morning_indices[-1]
    morning_open = float(bars[first].open)
    morning_high = max(highs[i] for i in morning_indices)
    move = morning_high - morning_open
    morning_return = closes[last] / morning_open - 1.0
    current_morning_volume = sum(volumes[i] for i in morning_indices)
    volume_multiple = current_morning_volume / baseline_volume if baseline_volume > 0 else math.inf
    if morning_return < float(signal["morning_return_min"]):
        reasons["morning_return"] += 1
        return None, reasons
    if volume_multiple < float(signal["morning_volume_min_median_multiple"]):
        reasons["morning_volume"] += 1
        return None, reasons
    if vwaps[last] is None or closes[last] <= float(vwaps[last]):
        reasons["morning_vwap"] += 1
        return None, reasons
    if closes[last] <= emas[last] or move <= 0:
        reasons["morning_ema_or_move"] += 1
        return None, reasons

    signal_start = int(eligibility["signal_start_minute_et"])
    signal_end = int(eligibility["signal_end_minute_et"])
    slope_bars = int(signal["ema_slope_lookback_bars"])
    volume_lookback = int(signal["signal_volume_lookback_bars"])
    pullback_start = last + 1
    for i in range(max(pullback_start, volume_lookback, int(signal["atr_period"])), len(bars) - 1):
        minute = _completion_minute(bars[i])
        if not signal_start <= minute <= signal_end:
            continue
        atr = atrs[i]
        vwap = vwaps[i]
        if atr is None or atr <= 0 or vwap is None or vwaps[i - 1] is None:
            reasons["indicator_not_ready"] += 1
            continue
        pullback_low = min(lows[pullback_start:i + 1])
        retracement = (morning_high - pullback_low) / move
        if not (float(signal["pullback_retracement_min_fraction"]) <= retracement
                <= float(signal["pullback_retracement_max_fraction"])):
            reasons["retracement"] += 1
            continue
        if lows[i] > float(vwap) + float(signal["pullback_low_max_vwap_atr"]) * atr:
            reasons["vwap_not_touched"] += 1
            continue
        touched = lows[i] <= float(vwap) or closes[i - 1] <= float(vwaps[i - 1])
        if not touched or closes[i] <= float(vwap):
            reasons["vwap_not_reclaimed"] += 1
            continue
        if closes[i] <= emas[i]:
            reasons["ema_not_reclaimed"] += 1
            continue
        if emas[i] - emas[i - slope_bars] < float(signal["ema_slope_min_atr_multiple"]) * atr:
            reasons["ema_slope"] += 1
            continue
        intraday_median = exact_median(volumes[i - volume_lookback:i])
        if intraday_median <= 0 or volumes[i] / intraday_median < float(
                signal["signal_volume_min_median_multiple"]):
            reasons["signal_volume"] += 1
            continue
        entry = float(bars[i + 1].open)
        stop = pullback_low - float(contract["exit"]["stop_atr_below_pullback_low"]) * atr
        one_r = entry - stop
        if one_r <= 0:
            reasons["invalid_geometry"] += 1
            continue
        return Candidate(
            symbol=symbol, day=day, entry_index=i + 1, signal_ts=bars[i].ts_utc,
            entry_ts=bars[i + 1].ts_utc, entry_price=entry, stop=stop,
            target=entry + float(contract["exit"]["target_r_multiple"]) * one_r,
            morning_return=morning_return, morning_volume_multiple=volume_multiple,
        ), reasons
    reasons["no_signal"] += 1
    return None, reasons


def close_candidate(candidate: Candidate, bars: Sequence[Any]) -> tuple[datetime, float, str]:
    for bar in bars[candidate.entry_index:]:
        open_ = float(bar.open)
        if open_ <= candidate.stop:
            return bar.ts_utc, open_, "stop_gap"
        if open_ >= candidate.target:
            return bar.ts_utc, open_, "target_gap"
        stop_hit = float(bar.low) <= candidate.stop
        target_hit = float(bar.high) >= candidate.target
        if stop_hit:
            return bar.ts_utc, candidate.stop, "stop_first" if target_hit else "stop"
        if target_hit:
            return bar.ts_utc, candidate.target, "target"
    last = bars[-1]
    return last.ts_utc, float(last.close), "eod"


def _family(symbol: str, contract: Mapping[str, Any]) -> str | None:
    for name, members in contract["exposure_families"].items():
        if symbol in members:
            return str(name)
    return None


def _allocate(candidates: Sequence[tuple[Candidate, Sequence[Any]]], contract: Mapping[str, Any]
              ) -> tuple[list[GrossTrade], Counter[str]]:
    portfolio = contract["portfolio"]
    initial = float(portfolio["initial_capital"])
    risk_budget = initial * float(portfolio["risk_fraction_per_trade"])
    notional_cap = initial * float(portfolio["notional_cap_fraction_per_trade"])
    grouped: dict[datetime, list[tuple[Candidate, Sequence[Any]]]] = defaultdict(list)
    for item in candidates:
        grouped[item[0].entry_ts].append(item)
    open_positions: list[tuple[datetime, str | None]] = []
    trades: list[GrossTrade] = []
    skipped: Counter[str] = Counter()
    for entry_ts in sorted(grouped):
        open_positions = [item for item in open_positions if item[0] > entry_ts]
        ranked = sorted(grouped[entry_ts], key=lambda item: (
            -item[0].morning_volume_multiple, -item[0].morning_return, item[0].symbol))
        for candidate, bars in ranked:
            if len(open_positions) >= int(portfolio["max_positions"]):
                skipped["slot_cap"] += 1
                continue
            family = _family(candidate.symbol, contract)
            if family is not None and sum(item[1] == family for item in open_positions) >= int(
                    portfolio["max_positions_per_exposure_family"]):
                skipped["family_cap"] += 1
                continue
            per_share = candidate.entry_price - candidate.stop
            qty = math.floor(min(risk_budget / per_share, notional_cap / candidate.entry_price))
            if qty < 1:
                skipped["qty_lt_1"] += 1
                continue
            exit_ts, exit_price, reason = close_candidate(candidate, bars)
            trades.append(GrossTrade(
                symbol=candidate.symbol, day=candidate.day, signal_ts=candidate.signal_ts,
                entry_ts=candidate.entry_ts, exit_ts=exit_ts, entry_price=candidate.entry_price,
                exit_price=exit_price, qty=qty, exit_reason=reason,
                morning_return=candidate.morning_return,
                morning_volume_multiple=candidate.morning_volume_multiple,
            ))
            open_positions.append((exit_ts, family))
    return trades, skipped


def _net(trade: GrossTrade, pct_points: float) -> float:
    rate = pct_points / 100.0
    gross = (trade.exit_price - trade.entry_price) * trade.qty
    cost = (trade.entry_price + trade.exit_price) * trade.qty * rate / 2.0
    return gross - cost


def _stats(trades: Sequence[GrossTrade], days: Sequence[date],
           folds: Sequence[tuple[str, date, date]], pct_points: float,
           initial: float) -> dict[str, Any]:
    pnls = [_net(trade, pct_points) for trade in trades]
    by_day: dict[date, float] = defaultdict(float)
    for trade, pnl in zip(trades, pnls):
        by_day[trade.day] += pnl
    cash = peak = initial
    mdd = 0.0
    for day in days:
        cash += by_day.get(day, 0.0)
        peak = max(peak, cash)
        if peak > 0:
            mdd = max(mdd, (peak - cash) / peak)
    gains = sum(pnl for pnl in pnls if pnl > 0)
    losses = sum(pnl for pnl in pnls if pnl < 0)
    fold_pnl = {
        name: sum(pnl for trade, pnl in zip(trades, pnls) if start <= trade.day <= end)
        for name, start, end in folds
    }
    total = sum(pnls)
    return {
        "trade_count": len(trades), "trading_days_with_trade": len(by_day),
        "cumulative_net_pnl": total,
        "net_expectancy": total / len(trades) if trades else 0.0,
        "profit_factor": gains / abs(losses) if losses else "Infinity",
        "mdd": mdd, "positive_folds": sum(value > 0 for value in fold_pnl.values()),
        "fold_pnl": fold_pnl,
    }


def _gate(stats: Mapping[str, Mapping[str, Any]], defects: Mapping[str, Any],
          contract: Mapping[str, Any]) -> dict[str, Any]:
    gate = contract["development_gate"]
    official = stats["official"]
    stress = stats["stress"]
    pf = official["profit_factor"]
    items = {
        "D1": official["trade_count"] >= int(gate["D1_min_trades"]),
        "D2": official["trading_days_with_trade"] >= int(gate["D2_min_trading_days"]),
        "D3": official["net_expectancy"] > float(gate["D3_official_expectancy_gt"]),
        "D4": pf == "Infinity" or float(pf) >= float(gate["D4_official_profit_factor_gte"]),
        "D5": official["mdd"] <= float(gate["D5_official_mdd_lte"]),
        "D6": official["positive_folds"] >= int(gate["D6_min_positive_folds"]),
        "D7": stress["cumulative_net_pnl"] >= float(gate["D7_stress_cumulative_net_gte"]),
        "D9": defects["incomplete_symbol_day_ratio"] <= float(
            gate["D9_max_incomplete_symbol_day_ratio"])
            and defects["new_conflicts"] <= int(gate["D9_max_new_conflicts"])
            and defects["major_defects"] == 0,
    }
    failed = [name for name, passed in items.items() if not passed]
    verdict = "INCONCLUSIVE_DATA" if not items["D9"] else (
        "NO_SELECTION" if failed else "GO_CANDIDATE")
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
    eval_days = evaluation_days(contract)
    eval_set = set(eval_days)
    symbols = tuple(contract["universe"])
    lookback = int(contract["signal"]["morning_volume_lookback_complete_days"])
    histories = {symbol: deque(maxlen=lookback) for symbol in symbols}
    candidates: list[tuple[Candidate, Sequence[Any]]] = []
    exclusions: Counter[str] = Counter()
    filters: Counter[str] = Counter()
    incomplete: set[tuple[str, date]] = set()
    major: set[tuple[str, date, str]] = set()
    new_conflicts = 0
    for day in data_days(contract):
        for symbol in symbols:
            candles = load_symbol_day(snapshot_path, symbol, day)
            raw_defects = detect_data_defects(candles)
            if raw_defects:
                if day in eval_set:
                    major.update((symbol, day, reason) for reason in raw_defects)
                    exclusions.update(raw_defects)
                continue
            bars, defects = complete_regular_5m(candles, day, symbol)
            if defects:
                if day in eval_set:
                    incomplete.add((symbol, day))
                    exclusions.update(defects)
                    new_conflicts += int("duplicate_timestamp_value_conflict" in defects)
                continue
            volume = morning_volume(bars, contract)
            if day in eval_set:
                history = histories[symbol]
                if len(history) < lookback:
                    exclusions["insufficient_morning_volume_lookback"] += 1
                else:
                    candidate, reasons = detect_candidate(
                        symbol, day, bars, exact_median(tuple(history)), contract)
                    filters.update(reasons)
                    if candidate is not None:
                        candidates.append((candidate, bars))
            histories[symbol].append(volume)
    trades, skipped = _allocate(candidates, contract)
    folds = fold_boundaries(contract)
    stats = {
        level: _stats(trades, eval_days, folds, float(pct),
                      float(contract["portfolio"]["initial_capital"]))
        for level, pct in contract["costs"]["round_trip_pct_points"].items()
    }
    total_symbol_days = len(eval_days) * len(symbols)
    defects = {
        "incomplete_symbol_days": len(incomplete), "total_symbol_days": total_symbol_days,
        "incomplete_symbol_day_ratio": len(incomplete) / total_symbol_days,
        "new_conflicts": new_conflicts, "major_defects": len(major),
    }
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA, "strategy_id": contract["strategy_id"],
        "scope": "development-only", "blind_oos_opened": False,
        "prereg": {"json_sha256": PREREG_JSON_SHA256, "md_sha256": PREREG_MD_SHA256},
        "identity": [asdict(check) for check in identity.checks],
        "window": {"start": eval_days[0].isoformat(), "end": eval_days[-1].isoformat(),
                   "trading_days": len(eval_days)},
        "candidate_count_before_portfolio": len(candidates), "trade_count": len(trades),
        "stats": stats, "defects": defects, "gate": _gate(stats, defects, contract),
        "exit_reasons": dict(sorted(Counter(trade.exit_reason for trade in trades).items())),
        "exclusions": dict(sorted(exclusions.items())),
        "filters": dict(sorted(filters.items())),
        "portfolio_skips": dict(sorted(skipped.items())),
        "execution_audit": [{
            "phase": "post_result_merge_review",
            "issue": "signal_completion_window_compared_against_start_label",
            "resolution": "compare_canonical_5m_start_label_plus_5_minutes",
            "performance_values_inspected_before_fix": True,
            "strategy_rules_changed": False,
            "result_regenerated": True,
        }],
        "trades_sha256": hashlib.sha256(canonical_json_bytes({
            "trades": [{**asdict(trade), "day": trade.day.isoformat(),
                        "signal_ts": trade.signal_ts.isoformat(),
                        "entry_ts": trade.entry_ts.isoformat(),
                        "exit_ts": trade.exit_ts.isoformat()} for trade in trades]
        })).hexdigest(),
    }
    result["payload_sha256"] = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    return result


def write_result(result: Mapping[str, Any], path: str) -> None:
    Path(path).write_bytes(json.dumps(
        result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    ).encode("utf-8") + b"\n")
