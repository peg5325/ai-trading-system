"""큰 갭하락 후 VWAP 회복 development 단일 실행."""
from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter, defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.history.daily_manifest import build_manifest
from src.history.exchange_calendar import trading_days
from src.history.raw_snapshot import RawSnapshotStore
from src.history.research_snapshot import file_sha256
from src.research.gap_down_fade.contract import (
    PREREG_JSON_SHA256,
    PREREG_MD_SHA256,
    data_days,
    evaluation_days,
    fold_boundaries,
    load_contract,
)
from src.research.gap_down_fade.indicators import (
    cumulative_vwap,
    exact_median,
    wilder_atr,
    wilder_rsi,
)
from src.research.gap_down_fade.loading import (
    complete_regular_5m,
    load_daily_closes,
    load_symbol_day,
)
from src.research.orb.simulator import detect_data_defects

RESULT_SCHEMA = "p0-us30-gap-down-fade-development/v1"
TRACKED_INTRADAY_MANIFEST = "docs/research/P0_US30_ORB_CLEAN_1M_MANIFEST.json"
TRACKED_SNAPSHOT_RECEIPT = "docs/research/P0_US30_ORB_CLEAN_SNAPSHOT_RECEIPT.json"
TRACKED_DAILY_MANIFEST = "docs/research/P0_US30_DAILY_10Y_MANIFEST.json"
UNIVERSE_YAML = "config/universe_p0_us30.yaml"
DAILY_DEVELOPMENT_CONTENT_SHA256 = (
    "0550100cdf9b6a04ec5ba191ab0037c189dae7ea4feeab222cadf0140b980f1c"
)
DAILY_DEVELOPMENT_ROW_COUNT = 72_940


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
    gap_return: float
    opening_volume_multiple: float


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
    gap_return: float
    opening_volume_multiple: float


def _minute(bar: Any) -> int:
    return bar.ts_exchange.hour * 60 + bar.ts_exchange.minute


def _completion_minute(bar: Any) -> int:
    """Return the completion minute for canonical start-labelled 5m bars."""
    return _minute(bar) + 5


def opening_volume(bars: Sequence[Any], contract: Mapping[str, Any]) -> float:
    eligibility = contract["eligibility"]
    start = int(eligibility["opening_start_minute_et"])
    end = int(eligibility["opening_end_exclusive_minute_et"])
    selected = [bar for bar in bars if start <= _minute(bar) < end]
    if len(selected) != (end - start) // 5:
        raise ValueError("개장 30분 완전 5분봉이 아니다")
    return sum(float(bar.volume) for bar in selected)


def detect_candidate(
    symbol: str,
    day: date,
    bars: Sequence[Any],
    previous_close: float,
    baseline_opening_volume: float,
    contract: Mapping[str, Any],
) -> tuple[Candidate | None, Counter[str]]:
    signal = contract["signal"]
    eligibility = contract["eligibility"]
    closes = [float(bar.close) for bar in bars]
    highs = [float(bar.high) for bar in bars]
    lows = [float(bar.low) for bar in bars]
    volumes = [float(bar.volume) for bar in bars]
    vwaps = cumulative_vwap(highs, lows, closes, volumes)
    atrs = wilder_atr(highs, lows, closes, int(signal["atr_period"]))
    rsis = wilder_rsi(closes, int(signal["rsi_period"]))
    reasons: Counter[str] = Counter()

    opening_start = int(eligibility["opening_start_minute_et"])
    opening_end = int(eligibility["opening_end_exclusive_minute_et"])
    opening_indices = [
        index for index, bar in enumerate(bars)
        if opening_start <= _minute(bar) < opening_end
    ]
    if len(opening_indices) != 6:
        reasons["incomplete_opening_window"] += 1
        return None, reasons
    first, last = opening_indices[0], opening_indices[-1]
    opening_open = float(bars[first].open)
    gap_return = opening_open / previous_close - 1.0
    if gap_return > float(signal["gap_return_lte"]):
        reasons["gap_too_small"] += 1
        return None, reasons
    opening_low = min(lows[index] for index in opening_indices)
    if opening_low / opening_open - 1.0 > float(signal["opening_selloff_return_lte"]):
        reasons["opening_selloff_too_small"] += 1
        return None, reasons
    current_opening_volume = sum(volumes[index] for index in opening_indices)
    volume_multiple = (
        current_opening_volume / baseline_opening_volume
        if baseline_opening_volume > 0 else math.inf
    )
    if volume_multiple < float(signal["opening_volume_min_median_multiple"]):
        reasons["opening_volume"] += 1
        return None, reasons
    opening_rsis = [rsis[index] for index in opening_indices if rsis[index] is not None]
    if not opening_rsis or min(float(value) for value in opening_rsis) > float(
        signal["opening_min_rsi_lte"]
    ):
        reasons["opening_rsi"] += 1
        return None, reasons

    signal_start = int(eligibility["signal_start_minute_et"])
    signal_end = int(eligibility["signal_end_minute_et"])
    higher_low_lookback = int(signal["higher_low_lookback_bars"])
    volume_lookback = int(signal["signal_volume_lookback_bars"])
    start_index = max(last + 1, int(signal["atr_period"]), volume_lookback)
    for index in range(start_index, len(bars) - 1):
        minute = _completion_minute(bars[index])
        if not signal_start <= minute <= signal_end:
            continue
        atr = atrs[index]
        current_vwap = vwaps[index]
        previous_vwap = vwaps[index - 1]
        current_rsi = rsis[index]
        previous_rsi = rsis[index - 1]
        if any(value is None for value in (atr, current_vwap, previous_vwap, current_rsi, previous_rsi)):
            reasons["indicator_not_ready"] += 1
            continue
        if not (closes[index - 1] <= float(previous_vwap) and closes[index] > float(current_vwap)):
            reasons["vwap_not_crossed"] += 1
            continue
        if lows[index] <= min(lows[index - higher_low_lookback:index]):
            reasons["no_higher_low"] += 1
            continue
        if float(current_rsi) <= float(previous_rsi):
            reasons["rsi_not_rising"] += 1
            continue
        intraday_median = exact_median(volumes[index - volume_lookback:index])
        if intraday_median <= 0 or volumes[index] / intraday_median < float(
            signal["signal_volume_min_median_multiple"]
        ):
            reasons["signal_volume"] += 1
            continue
        entry = float(bars[index + 1].open)
        session_low = min(lows[:index + 1])
        stop = session_low - float(contract["exit"]["stop_atr_below_session_low"]) * float(atr)
        one_r = entry - stop
        if one_r <= 0:
            reasons["invalid_geometry"] += 1
            continue
        return Candidate(
            symbol=symbol,
            day=day,
            entry_index=index + 1,
            signal_ts=bars[index].ts_utc,
            entry_ts=bars[index + 1].ts_utc,
            entry_price=entry,
            stop=stop,
            target=entry + float(contract["exit"]["target_r_multiple"]) * one_r,
            gap_return=gap_return,
            opening_volume_multiple=volume_multiple,
        ), reasons
    reasons["no_signal"] += 1
    return None, reasons


def close_candidate(candidate: Candidate, bars: Sequence[Any]) -> tuple[datetime, float, str]:
    for bar in bars[candidate.entry_index:]:
        open_price = float(bar.open)
        if open_price <= candidate.stop:
            return bar.ts_utc, open_price, "stop_gap"
        if open_price >= candidate.target:
            return bar.ts_utc, open_price, "target_gap"
        stop_hit = float(bar.low) <= candidate.stop
        target_hit = float(bar.high) >= candidate.target
        if stop_hit:
            return (
                bar.ts_utc + timedelta(minutes=5),
                candidate.stop,
                "stop_first" if target_hit else "stop",
            )
        if target_hit:
            return bar.ts_utc + timedelta(minutes=5), candidate.target, "target"
    last = bars[-1]
    return last.ts_utc + timedelta(minutes=5), float(last.close), "eod"


def _family(symbol: str, contract: Mapping[str, Any]) -> str | None:
    for name, members in contract["exposure_families"].items():
        if symbol in members:
            return str(name)
    return None


def _allocate(
    candidates: Sequence[tuple[Candidate, Sequence[Any]]], contract: Mapping[str, Any]
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
        open_positions = [position for position in open_positions if position[0] > entry_ts]
        ranked = sorted(grouped[entry_ts], key=lambda item: (
            item[0].gap_return,
            -item[0].opening_volume_multiple,
            item[0].symbol,
        ))
        for candidate, bars in ranked:
            if len(open_positions) >= int(portfolio["max_positions"]):
                skipped["slot_cap"] += 1
                continue
            family = _family(candidate.symbol, contract)
            if family is not None and sum(
                position[1] == family for position in open_positions
            ) >= int(portfolio["max_positions_per_exposure_family"]):
                skipped["family_cap"] += 1
                continue
            per_share = candidate.entry_price - candidate.stop
            qty = math.floor(min(risk_budget / per_share, notional_cap / candidate.entry_price))
            if qty < 1:
                skipped["qty_lt_1"] += 1
                continue
            exit_ts, exit_price, reason = close_candidate(candidate, bars)
            trades.append(GrossTrade(
                symbol=candidate.symbol,
                day=candidate.day,
                signal_ts=candidate.signal_ts,
                entry_ts=candidate.entry_ts,
                exit_ts=exit_ts,
                entry_price=candidate.entry_price,
                exit_price=exit_price,
                qty=qty,
                exit_reason=reason,
                gap_return=candidate.gap_return,
                opening_volume_multiple=candidate.opening_volume_multiple,
            ))
            open_positions.append((exit_ts, family))
    return trades, skipped


def _net(trade: GrossTrade, pct_points: float) -> float:
    rate = pct_points / 100.0
    gross = (trade.exit_price - trade.entry_price) * trade.qty
    cost = (trade.entry_price + trade.exit_price) * trade.qty * rate / 2.0
    return gross - cost


def _stats(
    trades: Sequence[GrossTrade],
    days: Sequence[date],
    folds: Sequence[tuple[str, date, date]],
    pct_points: float,
    initial: float,
) -> dict[str, Any]:
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
        "trade_count": len(trades),
        "trading_days_with_trade": len(by_day),
        "cumulative_net_pnl": total,
        "net_expectancy": total / len(trades) if trades else 0.0,
        "profit_factor": gains / abs(losses) if losses else "Infinity",
        "mdd": mdd,
        "positive_folds": sum(value > 0 for value in fold_pnl.values()),
        "fold_pnl": fold_pnl,
    }


def _gate(
    stats: Mapping[str, Mapping[str, Any]],
    defects: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
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
        "D9": (
            defects["incomplete_symbol_day_ratio"]
            <= float(gate["D9_max_incomplete_symbol_day_ratio"])
            and defects["missing_previous_close_ratio"]
            <= float(gate["D9_max_missing_previous_close_ratio"])
            and defects["new_conflicts"] <= int(gate["D9_max_new_conflicts"])
            and defects["major_defects"] == 0
        ),
    }
    failed = [name for name, passed in items.items() if not passed]
    verdict = "INCONCLUSIVE_DATA" if not items["D9"] else (
        "NO_SELECTION" if failed else "GO_CANDIDATE"
    )
    return {"items": items, "failed": failed, "verdict": verdict}


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _read_json(path: str) -> Mapping[str, Any]:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise TypeError(f"identity JSON object 아님: {path}")
    return value


def _identity_check(name: str, expected: str, actual: str) -> dict[str, str]:
    status = "match" if actual == expected else "mismatch"
    if status != "match":
        raise RuntimeError(
            f"identity 불일치: {name} expected={expected} actual={actual}"
        )
    return {"name": name, "expected": expected, "actual": actual, "status": status}


def _verify_data_identity(
    contract: Mapping[str, Any], snapshot_path: str, raw_root: str, daily_db_path: str
) -> list[dict[str, Any]]:
    """Verify frozen receipts plus a live development-only daily content hash.

    The snapshot file SHA authenticates the exact frozen file without decoding OOS
    OHLCV. Its semantic content hash is then verified transitively through the
    tracked creation receipt. The live daily manifest is bounded at the development
    end date so blind-OOS daily values are never streamed into the run.
    """
    identity = contract["data_identity"]
    if not os.path.isfile(snapshot_path):
        raise RuntimeError("고정 intraday snapshot 부재")
    if not os.path.isdir(raw_root):
        raise RuntimeError("고정 raw snapshot root 부재")
    snapshot_receipt = _read_json(TRACKED_SNAPSHOT_RECEIPT)
    snapshot_receipt_data = snapshot_receipt["snapshot"]
    intraday_manifest = _read_json(TRACKED_INTRADAY_MANIFEST)
    daily_manifest_receipt = _read_json(TRACKED_DAILY_MANIFEST)
    snapshot_file_actual = file_sha256(snapshot_path)
    raw_actual = str(RawSnapshotStore(raw_root).manifest()["raw_content_sha256"])

    checks = [
        _identity_check(
            "snapshot_file",
            str(identity["intraday_snapshot_file_sha256"]),
            snapshot_file_actual,
        ),
        _identity_check(
            "snapshot_receipt_file",
            snapshot_file_actual,
            str(snapshot_receipt_data["file_sha256"]),
        ),
        _identity_check(
            "snapshot_content_receipt",
            str(identity["intraday_snapshot_content_sha256"]),
            str(snapshot_receipt_data["content_sha256"]),
        ),
        _identity_check(
            "manifest_data_receipt",
            str(identity["intraday_manifest_data_sha256"]),
            str(intraday_manifest["data_content_sha256"]),
        ),
        _identity_check(
            "raw_content_index",
            str(identity["raw_content_sha256"]),
            raw_actual,
        ),
        _identity_check(
            "daily_full_manifest_receipt",
            str(identity["daily_data_content_sha256"]),
            str(daily_manifest_receipt["data_content_sha256"]),
        ),
        _identity_check(
            "daily_full_rows_receipt",
            str(identity["daily_row_count"]),
            str(daily_manifest_receipt["summary"]["total_rows"]),
        ),
    ]
    development_end = str(contract["windows"]["development_eval_end"])
    daily_development = build_manifest(
        daily_db_path,
        UNIVERSE_YAML,
        collection_params={},
        collection_code_commit=None,
        generated_at_utc="identity-check",
        as_of=development_end,
        collection_completed_through=development_end,
    )
    checks.extend(
        [
            _identity_check(
                "daily_development_content",
                DAILY_DEVELOPMENT_CONTENT_SHA256,
                str(daily_development["data_content_sha256"]),
            ),
            _identity_check(
                "daily_development_rows",
                str(DAILY_DEVELOPMENT_ROW_COUNT),
                str(daily_development["summary"]["total_rows"]),
            ),
        ]
    )
    return checks


def run(snapshot_path: str, daily_db_path: str, raw_root: str) -> dict[str, Any]:
    contract = load_contract()
    identity_checks = _verify_data_identity(contract, snapshot_path, raw_root, daily_db_path)
    eval_days = evaluation_days(contract)
    eval_set = set(eval_days)
    all_data_days = data_days(contract)
    symbols = tuple(contract["universe"])
    lookback = int(contract["signal"]["opening_volume_lookback_complete_days"])
    histories = {symbol: deque(maxlen=lookback) for symbol in symbols}
    calendar = tuple(trading_days(all_data_days[0] - timedelta(days=10), all_data_days[-1]))
    previous_day = {calendar[index]: calendar[index - 1] for index in range(1, len(calendar))}
    daily_closes = {
        symbol: load_daily_closes(
            daily_db_path, symbol, calendar[0], all_data_days[-1]
        ) for symbol in symbols
    }
    candidates: list[tuple[Candidate, Sequence[Any]]] = []
    exclusions: Counter[str] = Counter()
    filters: Counter[str] = Counter()
    incomplete: set[tuple[str, date]] = set()
    missing_previous: set[tuple[str, date]] = set()
    major: set[tuple[str, date, str]] = set()
    new_conflicts = 0
    for day in all_data_days:
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
            volume = opening_volume(bars, contract)
            prior = daily_closes[symbol].get(previous_day[day])
            if prior is None:
                if day in eval_set:
                    missing_previous.add((symbol, day))
                    exclusions["missing_previous_daily_close"] += 1
                histories[symbol].append(volume)
                continue
            if day in eval_set:
                history = histories[symbol]
                if len(history) < lookback:
                    exclusions["insufficient_opening_volume_lookback"] += 1
                else:
                    candidate, reasons = detect_candidate(
                        symbol,
                        day,
                        bars,
                        prior,
                        exact_median(tuple(history)),
                        contract,
                    )
                    filters.update(reasons)
                    if candidate is not None:
                        candidates.append((candidate, bars))
            histories[symbol].append(volume)
    trades, skipped = _allocate(candidates, contract)
    folds = fold_boundaries(contract)
    stats = {
        level: _stats(
            trades,
            eval_days,
            folds,
            float(pct),
            float(contract["portfolio"]["initial_capital"]),
        )
        for level, pct in contract["costs"]["round_trip_pct_points"].items()
    }
    total_symbol_days = len(eval_days) * len(symbols)
    defects = {
        "incomplete_symbol_days": len(incomplete),
        "missing_previous_close_symbol_days": len(missing_previous),
        "total_symbol_days": total_symbol_days,
        "incomplete_symbol_day_ratio": len(incomplete) / total_symbol_days,
        "missing_previous_close_ratio": len(missing_previous) / total_symbol_days,
        "new_conflicts": new_conflicts,
        "major_defects": len(major),
    }
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "strategy_id": contract["strategy_id"],
        "scope": "development-only",
        "blind_oos_opened": False,
        "prereg": {"json_sha256": PREREG_JSON_SHA256, "md_sha256": PREREG_MD_SHA256},
        "identity": identity_checks,
        "window": {
            "start": eval_days[0].isoformat(),
            "end": eval_days[-1].isoformat(),
            "trading_days": len(eval_days),
        },
        "candidate_count_before_portfolio": len(candidates),
        "trade_count": len(trades),
        "stats": stats,
        "defects": defects,
        "gate": _gate(stats, defects, contract),
        "exit_reasons": dict(sorted(Counter(trade.exit_reason for trade in trades).items())),
        "exclusions": dict(sorted(exclusions.items())),
        "filters": dict(sorted(filters.items())),
        "portfolio_skips": dict(sorted(skipped.items())),
        "execution_audit": {
            "review_phase": "post_result_merge_review",
            "issue": "signal_completion_window_compared_against_start_label",
            "fix": "compare_canonical_5m_start_label_plus_5_minutes",
            "performance_values_inspected_before_fix": True,
            "strategy_rules_changed": False,
            "result_regenerated": True,
        },
        "trades_sha256": hashlib.sha256(canonical_json_bytes({
            "trades": [{
                **asdict(trade),
                "day": trade.day.isoformat(),
                "signal_ts": trade.signal_ts.isoformat(),
                "entry_ts": trade.entry_ts.isoformat(),
                "exit_ts": trade.exit_ts.isoformat(),
            } for trade in trades]
        })).hexdigest(),
    }
    result["payload_sha256"] = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    return result


def write_result(result: Mapping[str, Any], path: str) -> None:
    Path(path).write_bytes(json.dumps(
        result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    ).encode("utf-8") + b"\n")
