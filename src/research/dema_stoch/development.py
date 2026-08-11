"""Single-pass development backtest for DEMA60 + Stochastic %D momentum."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.models import Candle
from src.research.dema_stoch.contract import (
    PREREG_JSON_SHA256,
    PREREG_MD_SHA256,
    development_data_days,
    development_eval_days,
    fold_boundaries,
    load_contract,
)
from src.research.dema_stoch.indicators import dema, stochastic_d
from src.research.dema_stoch.loading import complete_regular_5m, load_symbol_day
from src.research.gap_rvol.simulator import detect_data_defects
from src.research.orb.identity import require_identity, verify_snapshot_identity

RESULT_SCHEMA = "p0-us30-dema-stoch-momentum-development/v1"
TRACKED_MANIFEST = "docs/research/P0_US30_ORB_CLEAN_1M_MANIFEST.json"


@dataclass(frozen=True)
class Candidate:
    symbol: str
    day: date
    side: str
    signal_ts: datetime
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    dema_distance_fraction: float
    stochastic_distance: float


@dataclass(frozen=True)
class Trade:
    symbol: str
    day: date
    side: str
    signal_ts: datetime
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    qty: int


def cross_side(
    previous: Candle,
    current: Candle,
    *,
    previous_dema: float,
    current_dema: float,
    previous_stoch_d: float | None,
    current_stoch_d: float | None,
    centerline: float,
) -> str | None:
    if previous_stoch_d is None or current_stoch_d is None:
        return None
    long = (
        previous.close <= previous_dema
        and current.close > current_dema
        and current.close > current.open
        and previous_stoch_d <= centerline
        and current_stoch_d > centerline
    )
    short = (
        previous.close >= previous_dema
        and current.close < current_dema
        and current.close < current.open
        and previous_stoch_d >= centerline
        and current_stoch_d < centerline
    )
    if long and short:
        raise ValueError("signal cannot be both LONG and SHORT")
    return "LONG" if long else "SHORT" if short else None


def candidates_in_segment(
    symbol: str,
    bars: Sequence[Candle],
    eval_days: set[date],
    contract: Mapping[str, Any],
) -> tuple[Candidate, ...]:
    if not bars:
        return ()
    closes = [float(bar.close) for bar in bars]
    highs = [float(bar.high) for bar in bars]
    lows = [float(bar.low) for bar in bars]
    signal = contract["signal"]
    demas = dema(closes, int(signal["dema_period"]))
    stoch = stochastic_d(
        highs,
        lows,
        closes,
        k_period=int(signal["stochastic_k_period"]),
        d_period=int(signal["stochastic_d_smoothing"]),
    )
    by_day: dict[date, list[int]] = defaultdict(list)
    for index, bar in enumerate(bars):
        by_day[bar.ts_exchange.date()].append(index)
    hold = int(contract["exit"]["holding_bars"])
    center = float(signal["centerline"])
    out: list[Candidate] = []
    for day in sorted(eval_days & set(by_day)):
        indices = by_day[day]
        for position, index in enumerate(indices):
            if index == 0 or position + hold >= len(indices):
                continue
            side = cross_side(
                bars[index - 1],
                bars[index],
                previous_dema=demas[index - 1],
                current_dema=demas[index],
                previous_stoch_d=stoch[index - 1],
                current_stoch_d=stoch[index],
                centerline=center,
            )
            if side is None:
                continue
            entry = bars[indices[position + 1]]
            exit_bar = bars[indices[position + hold]]
            out.append(
                Candidate(
                    symbol=symbol,
                    day=day,
                    side=side,
                    signal_ts=bars[index].ts_utc,
                    entry_ts=entry.ts_utc,
                    exit_ts=exit_bar.ts_utc,
                    entry_price=float(entry.open),
                    exit_price=float(exit_bar.close),
                    dema_distance_fraction=abs(
                        float(bars[index].close) - demas[index]
                    )
                    / float(bars[index].close),
                    stochastic_distance=abs(float(stoch[index]) - center),
                )
            )
            break
    return tuple(out)


def allocate(
    candidates: Sequence[Candidate], contract: Mapping[str, Any]
) -> tuple[tuple[Trade, ...], Counter[str]]:
    portfolio = contract["portfolio"]
    max_positions = int(portfolio["max_positions"])
    notional = float(portfolio["initial_capital"]) * float(
        portfolio["notional_cap_fraction_per_trade"]
    )
    grouped: dict[datetime, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.entry_ts].append(candidate)
    open_until: list[datetime] = []
    trades: list[Trade] = []
    skipped: Counter[str] = Counter()
    for entry_ts in sorted(grouped):
        open_until = [exit_ts for exit_ts in open_until if exit_ts > entry_ts]
        ranked = sorted(
            grouped[entry_ts],
            key=lambda item: (
                -item.dema_distance_fraction,
                -item.stochastic_distance,
                item.symbol,
            ),
        )
        for candidate in ranked:
            if len(open_until) >= max_positions:
                skipped["slot_cap"] += 1
                continue
            qty = math.floor(notional / candidate.entry_price)
            if qty < 1:
                skipped["qty_lt_1"] += 1
                continue
            trades.append(
                Trade(
                    symbol=candidate.symbol,
                    day=candidate.day,
                    side=candidate.side,
                    signal_ts=candidate.signal_ts,
                    entry_ts=candidate.entry_ts,
                    exit_ts=candidate.exit_ts,
                    entry_price=candidate.entry_price,
                    exit_price=candidate.exit_price,
                    qty=qty,
                )
            )
            open_until.append(candidate.exit_ts)
    return tuple(trades), skipped


def trade_pnl(trade: Trade, round_trip_pct_points: float) -> float:
    direction = 1.0 if trade.side == "LONG" else -1.0
    gross = direction * (trade.exit_price - trade.entry_price) * trade.qty
    rate = round_trip_pct_points / 100.0
    cost = (
        (trade.entry_price * trade.qty + trade.exit_price * trade.qty)
        * rate
        / 2.0
    )
    return gross - cost


def _stats(
    trades: Sequence[Trade],
    days: Sequence[date],
    folds: Sequence[tuple[str, date, date]],
    pct_points: float,
    initial_capital: float,
) -> dict[str, Any]:
    pnls = [trade_pnl(trade, pct_points) for trade in trades]
    by_day: dict[date, float] = defaultdict(float)
    for trade, pnl in zip(trades, pnls):
        by_day[trade.day] += pnl
    cash = initial_capital
    peak = initial_capital
    mdd = 0.0
    for day in days:
        cash += by_day.get(day, 0.0)
        peak = max(peak, cash)
        if peak > 0:
            mdd = max(mdd, (peak - cash) / peak)
    gains = sum(value for value in pnls if value > 0)
    losses = sum(value for value in pnls if value < 0)
    fold_pnl = {
        name: sum(
            pnl
            for trade, pnl in zip(trades, pnls)
            if start <= trade.day <= end
        )
        for name, start, end in folds
    }
    total = sum(pnls)
    return {
        "trade_count": len(trades),
        "trading_days_with_trade": len(by_day),
        "cumulative_net_pnl": total,
        "net_expectancy": total / len(trades) if trades else 0.0,
        "profit_factor": gains / abs(losses) if losses else "Infinity",
        "win_rate": sum(value > 0 for value in pnls) / len(pnls) if pnls else 0.0,
        "mdd": mdd,
        "positive_folds": sum(value > 0 for value in fold_pnl.values()),
        "fold_pnl": fold_pnl,
    }


def _gate(
    combined: Mapping[str, Mapping[str, Any]],
    defects: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    gate = contract["development_gate"]
    official = combined["official"]
    stress = combined["stress"]
    profit_factor = official["profit_factor"]
    items = {
        "D1": official["trade_count"] >= int(gate["D1_min_trades"]),
        "D2": official["trading_days_with_trade"]
        >= int(gate["D2_min_trading_days"]),
        "D3": official["net_expectancy"]
        > float(gate["D3_official_expectancy_gt"]),
        "D4": profit_factor == "Infinity"
        or float(profit_factor) >= float(gate["D4_official_profit_factor_gte"]),
        "D5": official["mdd"] <= float(gate["D5_official_mdd_lte"]),
        "D6": official["positive_folds"] >= int(gate["D6_min_positive_folds"]),
        "D7": stress["cumulative_net_pnl"]
        >= float(gate["D7_stress_cumulative_net_gte"]),
        "D9": defects["incomplete_symbol_day_ratio"]
        <= float(gate["D9_max_incomplete_symbol_day_ratio"])
        and defects["new_conflicts"] <= int(gate["D9_max_new_conflicts"])
        and defects["major_defects"] == 0,
    }
    failed = [name for name, passed in items.items() if not passed]
    verdict = (
        "INCONCLUSIVE_DATA"
        if not items["D9"]
        else "NO_SELECTION"
        if failed
        else "GO_CANDIDATE"
    )
    return {"items": items, "failed": failed, "verdict": verdict}


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def run(
    snapshot_path: str,
    raw_root: str,
    *,
    producer_commit: str,
    review_evidence_url: str,
) -> dict[str, Any]:
    contract = load_contract()
    identity = verify_snapshot_identity(
        contract=contract,
        snapshot_path=snapshot_path,
        raw_root=raw_root,
        manifest_path=TRACKED_MANIFEST,
    )
    require_identity(identity)
    data_days = development_data_days(contract)
    eval_days = development_eval_days(contract)
    eval_set = set(eval_days)
    candidates: list[Candidate] = []
    incomplete: set[tuple[str, date]] = set()
    major: set[tuple[str, date, str]] = set()
    exclusions: Counter[str] = Counter()
    for symbol in map(str, contract["universe"]):
        segments: list[list[Candle]] = []
        current: list[Candle] = []
        for day in data_days:
            candles = load_symbol_day(snapshot_path, symbol, day)
            raw_defects = detect_data_defects(candles)
            bars, grid_defects = complete_regular_5m(candles, day, symbol)
            defects = tuple(sorted(set(raw_defects) | set(grid_defects)))
            if defects:
                if current:
                    segments.append(current)
                    current = []
                if day in eval_set:
                    if grid_defects:
                        incomplete.add((symbol, day))
                    major.update((symbol, day, reason) for reason in raw_defects)
                    exclusions.update(defects)
                continue
            current.extend(bars)
        if current:
            segments.append(current)
        for segment in segments:
            candidates.extend(candidates_in_segment(symbol, segment, eval_set, contract))
    trades, skipped = allocate(candidates, contract)
    costs = contract["costs"]["round_trip_pct_points"]
    folds = fold_boundaries(contract)
    initial = float(contract["portfolio"]["initial_capital"])
    slices = {
        "combined": trades,
        "long": tuple(trade for trade in trades if trade.side == "LONG"),
        "short": tuple(trade for trade in trades if trade.side == "SHORT"),
    }
    stats = {
        name: {
            level: _stats(items, eval_days, folds, float(cost), initial)
            for level, cost in costs.items()
        }
        for name, items in slices.items()
    }
    total_symbol_days = len(eval_days) * len(contract["universe"])
    defects = {
        "incomplete_symbol_days": len(incomplete),
        "total_symbol_days": total_symbol_days,
        "incomplete_symbol_day_ratio": len(incomplete) / total_symbol_days,
        "new_conflicts": sum(
            count
            for reason, count in exclusions.items()
            if reason == "duplicate_regular_minute"
        ),
        "major_defects": len(major),
    }
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "strategy_id": contract["strategy_id"],
        "scope": "development-only",
        "blind_oos_opened": False,
        "external_provider_used": False,
        "short_borrow_modeled": False,
        "producer_commit": producer_commit,
        "review_evidence_url": review_evidence_url,
        "source_image_sha256": contract["source_image_sha256"],
        "prereg": {
            "json_sha256": PREREG_JSON_SHA256,
            "md_sha256": PREREG_MD_SHA256,
        },
        "identity": [asdict(check) for check in identity.checks],
        "window": {
            "start": eval_days[0].isoformat(),
            "end": eval_days[-1].isoformat(),
            "trading_days": len(eval_days),
        },
        "candidate_count_before_portfolio": len(candidates),
        "trade_count": len(trades),
        "direction_counts": dict(sorted(Counter(t.side for t in trades).items())),
        "stats": stats,
        "defects": defects,
        "gate": _gate(stats["combined"], defects, contract),
        "exclusions": dict(sorted(exclusions.items())),
        "portfolio_skips": dict(sorted(skipped.items())),
        "trades_sha256": hashlib.sha256(
            canonical_json_bytes(
                {
                    "trades": [
                        {
                            **asdict(trade),
                            "day": trade.day.isoformat(),
                            "signal_ts": trade.signal_ts.isoformat(),
                            "entry_ts": trade.entry_ts.isoformat(),
                            "exit_ts": trade.exit_ts.isoformat(),
                        }
                        for trade in trades
                    ]
                }
            )
        ).hexdigest(),
    }
    result["payload_sha256"] = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    return result


def write_result(result: Mapping[str, Any], path: str) -> None:
    Path(path).write_bytes(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )

