"""SPY 첫 30분 → 마지막 30분 장중 모멘텀 development 단일 실행."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.history.daily_manifest import build_manifest
from src.history.exchange_calendar import trading_days
from src.research.orb.identity import require_identity, verify_snapshot_identity
from src.research.orb.simulator import detect_data_defects
from src.research.orb.windows import regular_close_minute_checked
from src.research.spy_intraday_momentum.contract import (
    PREREG_JSON_SHA256,
    PREREG_MD_SHA256,
    evaluation_days,
    fold_boundaries,
    load_contract,
)
from src.research.spy_intraday_momentum.loading import (
    complete_regular_1m,
    load_daily_closes,
    load_symbol_day,
)

RESULT_SCHEMA = "spy-intraday-momentum-development/v1"
TRACKED_INTRADAY_MANIFEST = "docs/research/P0_US30_ORB_CLEAN_1M_MANIFEST.json"
UNIVERSE_YAML = "config/universe_p0_us30.yaml"


@dataclass(frozen=True)
class GrossTrade:
    day: date
    direction: str
    signal_return: float
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    qty: int


def signal_direction(first_half_hour_return: float) -> str | None:
    if first_half_hour_return > 0:
        return "LONG"
    if first_half_hour_return < 0:
        return "SHORT"
    return None


def trade_for_day(
    day: date,
    bars: Sequence[Any],
    previous_close: float,
    contract: Mapping[str, Any],
) -> GrossTrade | None:
    """완전 정규장 1분봉에서 사전등록의 세 가격만 사용한다."""
    by_minute = {
        bar.ts_exchange.hour * 60 + bar.ts_exchange.minute: bar for bar in bars
    }
    eligibility = contract["eligibility"]
    first_half_close_minute = int(eligibility["first_half_hour_end_exclusive_minute_et"]) - 1
    first_half_close = float(by_minute[first_half_close_minute].close)
    signal_return = first_half_close / previous_close - 1.0
    direction = signal_direction(signal_return)
    if direction is None:
        return None
    close_minute = regular_close_minute_checked(day)
    entry = by_minute[close_minute - int(eligibility["last_half_hour_minutes"])]
    exit_bar = by_minute[close_minute - 1]
    notional = (
        float(contract["portfolio"]["initial_capital"])
        * float(contract["portfolio"]["notional_fraction_per_trade"])
    )
    qty = math.floor(notional / float(entry.open))
    if qty < 1:
        return None
    return GrossTrade(
        day=day,
        direction=direction,
        signal_return=signal_return,
        entry_ts=entry.ts_utc.isoformat(),
        exit_ts=exit_bar.ts_utc.isoformat(),
        entry_price=float(entry.open),
        exit_price=float(exit_bar.close),
        qty=qty,
    )


def gross_pnl(trade: GrossTrade) -> float:
    move = trade.exit_price - trade.entry_price
    return move * trade.qty if trade.direction == "LONG" else -move * trade.qty


def net_pnl(trade: GrossTrade, round_trip_pct_points: float) -> float:
    rate = round_trip_pct_points / 100.0
    cost = (trade.entry_price + trade.exit_price) * trade.qty * rate / 2.0
    return gross_pnl(trade) - cost


def _stats(
    trades: Sequence[GrossTrade],
    days: Sequence[date],
    folds: Sequence[tuple[str, date, date]],
    pct_points: float,
    initial: float,
) -> dict[str, Any]:
    pnls = [net_pnl(trade, pct_points) for trade in trades]
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


def _direction_stats(
    trades: Sequence[GrossTrade], pct_points: float
) -> dict[str, dict[str, float | int]]:
    result: dict[str, dict[str, float | int]] = {}
    for direction in ("LONG", "SHORT"):
        selected = [trade for trade in trades if trade.direction == direction]
        pnls = [net_pnl(trade, pct_points) for trade in selected]
        result[direction] = {
            "trade_count": len(selected),
            "cumulative_net_pnl": sum(pnls),
            "net_expectancy": sum(pnls) / len(selected) if selected else 0.0,
        }
    return result


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
            defects["incomplete_spy_day_ratio"]
            <= float(gate["D9_max_incomplete_spy_day_ratio"])
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


def _verify_data_identity(
    contract: Mapping[str, Any], snapshot_path: str, raw_root: str, daily_db_path: str
) -> list[dict[str, Any]]:
    identity = contract["data_identity"]
    intraday = verify_snapshot_identity(
        contract=contract,
        snapshot_path=snapshot_path,
        raw_root=raw_root,
        manifest_path=TRACKED_INTRADAY_MANIFEST,
    )
    require_identity(intraday)
    daily = build_manifest(
        daily_db_path,
        UNIVERSE_YAML,
        collection_params={},
        collection_code_commit=None,
        generated_at_utc="identity-check",
        as_of="2026-07-21",
        collection_completed_through="2026-07-21",
    )
    daily_hash = str(daily["data_content_sha256"])
    daily_rows = int(daily["summary"]["total_rows"])
    expected_hash = str(identity["daily_data_content_sha256"])
    expected_rows = int(identity["daily_row_count"])
    if daily_hash != expected_hash or daily_rows != expected_rows:
        raise RuntimeError("일봉 data identity 불일치")
    checks = [asdict(check) for check in intraday.checks]
    checks.extend([
        {"name": "daily_content", "expected": expected_hash, "actual": daily_hash,
         "status": "match"},
        {"name": "daily_rows", "expected": str(expected_rows), "actual": str(daily_rows),
         "status": "match"},
    ])
    return checks


def run(snapshot_path: str, daily_db_path: str, raw_root: str) -> dict[str, Any]:
    contract = load_contract()
    identity_checks = _verify_data_identity(contract, snapshot_path, raw_root, daily_db_path)
    days = evaluation_days(contract)
    symbol = str(contract["instrument"]["symbol"])
    calendar = tuple(trading_days(days[0] - timedelta(days=10), days[-1]))
    previous_day = {calendar[index]: calendar[index - 1] for index in range(1, len(calendar))}
    daily_closes = load_daily_closes(daily_db_path, symbol, calendar[0], days[-1])
    initial = float(contract["portfolio"]["initial_capital"])
    trades: list[GrossTrade] = []
    signs: Counter[str] = Counter()
    exclusions: Counter[str] = Counter()
    incomplete: set[date] = set()
    missing_previous: set[date] = set()
    major: set[tuple[date, str]] = set()
    new_conflicts = 0
    for day in days:
        candles = load_symbol_day(snapshot_path, symbol, day)
        raw_defects = detect_data_defects(candles)
        if raw_defects:
            major.update((day, reason) for reason in raw_defects)
            exclusions.update(raw_defects)
            continue
        bars, defects = complete_regular_1m(candles, day, symbol)
        if defects:
            incomplete.add(day)
            exclusions.update(defects)
            new_conflicts += int("duplicate_timestamp_value_conflict" in defects)
            continue
        prior = daily_closes.get(previous_day[day])
        if prior is None:
            missing_previous.add(day)
            exclusions["missing_previous_daily_close"] += 1
            continue
        trade = trade_for_day(day, bars, prior, contract)
        if trade is None:
            signs["ZERO"] += 1
            exclusions["zero_signal"] += 1
            continue
        signs[trade.direction] += 1
        trades.append(trade)
    folds = fold_boundaries(contract)
    stats = {
        level: _stats(trades, days, folds, float(pct), initial)
        for level, pct in contract["costs"]["round_trip_pct_points"].items()
    }
    defects = {
        "incomplete_spy_days": len(incomplete),
        "missing_previous_close_days": len(missing_previous),
        "total_spy_days": len(days),
        "incomplete_spy_day_ratio": len(incomplete) / len(days),
        "missing_previous_close_ratio": len(missing_previous) / len(days),
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
            "start": days[0].isoformat(),
            "end": days[-1].isoformat(),
            "trading_days": len(days),
        },
        "trade_count": len(trades),
        "signal_sign_counts": dict(sorted(signs.items())),
        "stats": stats,
        "official_direction_attribution": _direction_stats(
            trades, float(contract["costs"]["round_trip_pct_points"]["official"])
        ),
        "defects": defects,
        "gate": _gate(stats, defects, contract),
        "exclusions": dict(sorted(exclusions.items())),
        "trades_sha256": hashlib.sha256(canonical_json_bytes({
            "trades": [{**asdict(trade), "day": trade.day.isoformat()} for trade in trades]
        })).hexdigest(),
    }
    result["payload_sha256"] = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    return result


def write_result(result: Mapping[str, Any], path: str) -> None:
    Path(path).write_bytes(json.dumps(
        result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    ).encode("utf-8") + b"\n")
