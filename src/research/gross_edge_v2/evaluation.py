"""Aggregation, gates G1-G8, bootstrap/Holm, inverse-leg policy and diagnostics (§5-§6)."""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import numpy as np

from src.research.gross_edge_v2.arms import ARM_SYMBOLS, INVERSE, Trade, valid_bars_for
from src.research.gross_edge_v2.data import EXIT_MINUTE, DayBars

COST_BASELINE = 0.34
G1_MIN_TRADES = 100
G2_MIN_DAYS = 50
G3_MIN_MEAN = 0.45
G6_ALPHA = 0.05
G8_START = date(2025, 1, 2)
G8_END = date(2026, 1, 20)
G8_MIN_TRADES = 30
LEG_MIN_TRADES = 50
LEG_MIN_DAYS = 25
BOOTSTRAP_SEED = 20261001
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_BLOCK = 5
HOLM_FAMILY = 8
COSTS = {"commission_only": 0.2, "realistic": 0.3, "charter_official": 0.34, "stress": 0.51}
INVERSE_LEG_ARMS = ("A1", "B1", "B2", "B3", "C1", "C2", "D1")


class StatisticsContractError(RuntimeError):
    """Coded statistics constants differ from the frozen contract."""


def verify_statistics(contract: Mapping[str, Any]) -> None:
    gate = contract["screen_gate"]
    stats = contract["statistics"]
    g8 = gate["G8_recent_year"]
    observed = (
        gate["G1_min_trades"], gate["G2_min_trading_days"],
        gate["G3_min_mean_gross_pct_points"], gate["G4_each_half_mean_gt_pct_points"],
        gate["G5_mean_excluding_best_month_by_sum_gte_pct_points"],
        gate["G6_holm_adjusted_p_lt"],
        gate["G7_mean_excluding_best_symbol_by_sum_gte_pct_points"],
        g8["trade_date_start"], g8["trade_date_end"], g8["min_trades"],
        g8["mean_gt_pct_points"], stats["bootstrap_block_length"],
        stats["bootstrap_resamples"], dict(contract["cost_diagnostics_pct_points"]),
        tuple(contract["inverse_leg_policy"]["arms_with_inverse_legs"]),
    )
    expected = (
        G1_MIN_TRADES, G2_MIN_DAYS, G3_MIN_MEAN, COST_BASELINE, COST_BASELINE, G6_ALPHA,
        COST_BASELINE, G8_START.isoformat(), G8_END.isoformat(), G8_MIN_TRADES,
        COST_BASELINE, BOOTSTRAP_BLOCK, BOOTSTRAP_RESAMPLES, {**COSTS, "gating": False},
        INVERSE_LEG_ARMS,
    )
    if observed != expected:
        raise StatisticsContractError(f"statistics constants differ: {observed}")
    if "default_rng(20261001)" not in stats["bootstrap_rng"]:
        raise StatisticsContractError("bootstrap RNG seed differs")


def mean(values: Sequence[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def ordered(trades: Sequence[Trade]) -> list[Trade]:
    """Trade order of §5.1: (trading day, entry time, symbol) ascending."""
    return sorted(trades, key=Trade.sort_key)


def daily_returns(trades: Sequence[Trade]) -> list[tuple[date, float]]:
    """Arithmetic mean of the same day's trades; days without trades omitted."""
    by_day: dict[date, list[float]] = defaultdict(list)
    for trade in ordered(trades):
        by_day[trade.day].append(trade.gross_pct_points)
    return [(day, math.fsum(values) / len(values)) for day, values in sorted(by_day.items())]


def _mean_excluding_best_group(trades: Sequence[Trade], key) -> tuple[str | None, float | None]:
    """Drop the group with the largest gross sum; ties drop whichever leaves the lowest mean.

    The tie rule is not in the contract; the lowest remaining mean is the conservative choice.
    """
    groups: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        groups[key(trade)].append(trade.gross_pct_points)
    if not groups:
        return None, None
    best_sum = max(math.fsum(values) for values in groups.values())
    candidates = []
    for name in sorted(groups):
        if math.fsum(groups[name]) != best_sum:
            continue
        rest = [t.gross_pct_points for t in trades if key(t) != name]
        candidates.append((mean(rest), name))
    # None (nothing left) sorts first: it means the gate cannot pass.
    candidates.sort(key=lambda item: (item[0] is not None, item[0] or 0.0, item[1]))
    remaining, name = candidates[0]
    return name, remaining


def leg_statistics(trades: Sequence[Trade], registry_symbols: Sequence[str]) -> dict[str, Any]:
    """Per-trade statistics and gates G1-G5, G7, G8 for one trade set (arm or leg)."""
    trades = ordered(trades)
    values = [trade.gross_pct_points for trade in trades]
    n = len(values)
    days = daily_returns(trades)
    half = n // 2
    first_half, second_half = mean(values[:half]), mean(values[half:])
    best_month, ex_month = _mean_excluding_best_group(trades, lambda t: t.day.strftime("%Y-%m"))
    single_symbol = len(set(registry_symbols)) == 1
    best_symbol, ex_symbol = _mean_excluding_best_group(trades, lambda t: t.symbol)
    recent = [t.gross_pct_points for t in trades if G8_START <= t.day <= G8_END]
    recent_mean = mean(recent)
    trade_mean = mean(values)
    gates = {
        "G1": n >= G1_MIN_TRADES,
        "G2": len(days) >= G2_MIN_DAYS,
        "G3": trade_mean is not None and trade_mean >= G3_MIN_MEAN,
        "G4": (
            first_half is not None and second_half is not None
            and first_half > COST_BASELINE and second_half > COST_BASELINE
        ),
        "G5": ex_month is not None and ex_month >= COST_BASELINE,
        # G7 is not applicable (None) only when the registry symbol set is a single symbol.
        "G7": None if single_symbol else (ex_symbol is not None and ex_symbol >= COST_BASELINE),
        "G8": (
            len(recent) >= G8_MIN_TRADES and recent_mean is not None
            and recent_mean > COST_BASELINE
        ),
    }
    return {
        "trade_count": n,
        "trading_days": len(days),
        "mean_gross_pct_points": trade_mean,
        "mean_daily_gross_pct_points": mean([value for _, value in days]),
        "first_half_trades": half,
        "first_half_mean": first_half,
        "second_half_mean": second_half,
        "best_month_by_sum": best_month,
        "mean_excluding_best_month": ex_month,
        "best_symbol_by_sum": None if single_symbol else best_symbol,
        "mean_excluding_best_symbol": None if single_symbol else ex_symbol,
        "recent_year_trades": len(recent),
        "recent_year_mean": recent_mean,
        "gates": gates,
    }


def bootstrap_p_value(daily_values: Sequence[float]) -> float:
    """One-sided circular block bootstrap p for H0: mean daily return <= 0.34.

    Block length 5 over the index of days with trades, resample length n, uniform block
    starts, 10,000 resamples, fresh ``numpy.random.default_rng(20261001)`` (PCG64) per call
    so an arm's p does not depend on arm order. p = (#{mean* <= 0.34} + 1) / 10,001.
    An empty series gives p = 1 (no evidence).
    """
    n = len(daily_values)
    if n == 0:
        return 1.0
    series = np.asarray(daily_values, dtype=float)
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    blocks = math.ceil(n / BOOTSTRAP_BLOCK)
    starts = rng.integers(0, n, size=(BOOTSTRAP_RESAMPLES, blocks))
    index = (starts[:, :, None] + np.arange(BOOTSTRAP_BLOCK)) % n
    index = index.reshape(BOOTSTRAP_RESAMPLES, blocks * BOOTSTRAP_BLOCK)[:, :n]
    means = series[index].mean(axis=1)
    hits = int(np.count_nonzero(means <= COST_BASELINE))
    return (hits + 1) / (BOOTSTRAP_RESAMPLES + 1)


def holm_adjust(p_values: Mapping[str, float]) -> dict[str, float]:
    """Holm step-down over the 8-arm family: ascending, i-th x (8-i+1), cumulative max.

    Ties in p keep registry order. Adjusted values are capped at 1.
    """
    if len(p_values) != HOLM_FAMILY:
        raise ValueError("Holm family must contain exactly 8 arms")
    order = list(p_values)
    ranked = sorted(order, key=lambda arm: (p_values[arm], order.index(arm)))
    adjusted: dict[str, float] = {}
    running = 0.0
    for rank, arm in enumerate(ranked, start=1):
        running = max(running, min(1.0, p_values[arm] * (HOLM_FAMILY - rank + 1)))
        adjusted[arm] = running
    return {arm: adjusted[arm] for arm in order}


class BaselineCache:
    """Unconditional baseline (§5.4): mean (open_15:55 / open_m - 1) x 100 over all
    evaluated valid days of the symbol having both bars m and 15:55."""

    def __init__(
        self, bars_by_symbol: Mapping[str, Mapping[date, DayBars]], evaluated_days: Sequence[date]
    ):
        self._bars = bars_by_symbol
        self._days = tuple(sorted(set(evaluated_days)))
        self._cache: dict[tuple[str, int], float | None] = {}

    def value(self, symbol: str, minute: int) -> float | None:
        key = (symbol, minute)
        if key not in self._cache:
            values = []
            for day in self._days:
                bars = valid_bars_for(self._bars.get(symbol, {}), day)
                if bars is None:
                    continue
                start, end = bars.bar(minute), bars.bar(EXIT_MINUTE)
                if start is not None and end is not None:
                    values.append((end[0] / start[0] - 1.0) * 100.0)
            self._cache[key] = mean(values)
        return self._cache[key]


def _diagnostics(trades: Sequence[Trade], baseline: BaselineCache) -> dict[str, Any]:
    trades = ordered(trades)
    values = [trade.gross_pct_points for trade in trades]
    days = daily_returns(trades)
    best_day = None
    if days:
        day, value = max(days, key=lambda item: (item[1], -item[0].toordinal()))
        same_day = [t.gross_pct_points for t in trades if t.day == day]
        best_day = {
            "day": day.isoformat(),
            "daily_gross_pct_points": value,
            "trades": len(same_day),
            # contribution of that day's trades to the per-trade mean
            "contribution_to_trade_mean": math.fsum(same_day) / len(values),
        }
    per_symbol: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        per_symbol[trade.symbol].append(trade.gross_pct_points)
    baselines = [baseline.value(trade.symbol, trade.entry_minute) for trade in trades]
    baseline_mean = mean([value for value in baselines if value is not None])
    trade_mean = mean(values)
    return {
        "best_trading_day": best_day,
        "per_symbol": {
            symbol: {"trades": len(vals), "mean_gross_pct_points": mean(vals)}
            for symbol, vals in sorted(per_symbol.items())
        },
        "unconditional_baseline_mean": baseline_mean,
        "trade_mean_minus_baseline": (
            None if trade_mean is None or baseline_mean is None else trade_mean - baseline_mean
        ),
        "exit_reasons": {
            reason: sum(1 for t in trades if t.exit_reason == reason)
            for reason in ("stop", "target", "time")
        },
    }


def _passes(gates: Mapping[str, bool | None]) -> bool:
    return all(value is not False for value in gates.values())


def evaluate_arms(
    trades: Sequence[Trade], baseline: BaselineCache
) -> tuple[list[dict[str, Any]], str]:
    """Evaluate all 8 arms; returns (arm results in registry order, overall verdict)."""
    by_arm: dict[str, list[Trade]] = {arm: [] for arm in ARM_SYMBOLS}
    for trade in trades:
        by_arm[trade.arm_id].append(trade)
    raw_p = {
        arm: bootstrap_p_value([value for _, value in daily_returns(arm_trades)])
        for arm, arm_trades in by_arm.items()
    }
    holm = holm_adjust(raw_p)
    results = []
    for arm, arm_trades in by_arm.items():
        symbols = ARM_SYMBOLS[arm]
        overall = leg_statistics(arm_trades, symbols)
        overall["gates"]["G6"] = holm[arm] < G6_ALPHA
        overall["gates"] = dict(sorted(overall["gates"].items()))
        screen_pass = _passes(overall["gates"])
        non_inverse_symbols = [s for s in symbols if s not in INVERSE]
        non_inverse = leg_statistics(
            [t for t in arm_trades if not t.inverse], non_inverse_symbols
        )
        listing = {
            "min_trades": non_inverse["trade_count"] >= LEG_MIN_TRADES,
            "min_trading_days": non_inverse["trading_days"] >= LEG_MIN_DAYS,
            **{gate: non_inverse["gates"][gate] for gate in ("G3", "G4", "G5", "G7", "G8")},
        }
        inverse_leg = None
        if arm in INVERSE_LEG_ARMS:
            inverse_leg = leg_statistics(
                [t for t in arm_trades if t.inverse], [s for s in symbols if s in INVERSE]
            )
        # INVERSE_ONLY = the arm passes every screen gate but its non-inverse leg alone fails
        # the §5.3 listing requirements (the pass depends on SQQQ/SOXS trades).
        long_candidate = screen_pass and _passes(listing)
        sample_fail = not (overall["gates"]["G1"] and overall["gates"]["G2"])
        if long_candidate:
            status = "LONG_CANDIDATE"
        elif screen_pass:
            status = "INVERSE_ONLY"
        elif sample_fail:
            status = "SAMPLE"
        else:
            status = "FAIL"
        trade_mean = overall["mean_gross_pct_points"]
        results.append({
            "arm_id": arm,
            "all": overall,
            "bootstrap_p": raw_p[arm],
            "holm_p": holm[arm],
            "screen_pass": screen_pass,
            "non_inverse_leg": non_inverse,
            "inverse_leg": inverse_leg,
            "long_listing_checks": listing,
            "status": status,
            "net_after_cost_pct_points": {
                name: None if trade_mean is None else trade_mean - cost
                for name, cost in COSTS.items()
            },
            "diagnostics": _diagnostics(arm_trades, baseline),
        })
    verdict = (
        "SHORTLIST" if any(r["status"] == "LONG_CANDIDATE" for r in results)
        else "NO_GROSS_EDGE"
    )
    return results, verdict
