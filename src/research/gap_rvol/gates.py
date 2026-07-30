"""Gap+RVOL development gates with explicit economic-vs-defect outcomes."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.research.gap_rvol.contract import require
from src.research.gap_rvol.features import DATA_EXCLUSION_REASONS
from src.research.gap_rvol.simulator import DaySimulationResult
from src.research.orb.stats import PerformanceStats

OPERATIONAL_REASONS = frozenset({"stale_exit", "eod_fallback", "unclosable"})


@dataclass(frozen=True)
class DefectMetrics:
    data_excluded_symbol_days: int
    total_symbol_days: int
    operational_failure_trades: int
    entered_trades: int
    major_defect_count: int

    @property
    def data_ratio(self) -> float:
        return (
            self.data_excluded_symbol_days / self.total_symbol_days
            if self.total_symbol_days else math.inf
        )

    @property
    def operational_ratio(self) -> float:
        return (
            self.operational_failure_trades / self.entered_trades
            if self.entered_trades else 0.0
        )


@dataclass(frozen=True)
class GateItem:
    name: str
    actual: object
    passed: bool


@dataclass(frozen=True)
class GateDecision:
    items: tuple[GateItem, ...]
    verdict: str | None
    next_stage: str | None
    failed: tuple[str, ...]


def build_defect_metrics(
    *,
    day_results: Sequence[DaySimulationResult],
    total_symbol_days: int,
    identity_failure_count: int = 0,
    manifest_conflict_count: int = 0,
) -> DefectMetrics:
    excluded = {
        (result.day, symbol)
        for result in day_results
        for symbol, reason in result.excluded
        if reason in DATA_EXCLUSION_REASONS
    }
    trades = [trade for result in day_results for trade in result.trades]
    major = {
        (result.day, symbol, reason)
        for result in day_results
        for symbol, reason in result.major_defects
    }
    return DefectMetrics(
        data_excluded_symbol_days=len(excluded),
        total_symbol_days=total_symbol_days,
        operational_failure_trades=sum(
            trade.operational_reason in OPERATIONAL_REASONS for trade in trades),
        entered_trades=len(trades),
        major_defect_count=(
            len(major) + identity_failure_count + manifest_conflict_count),
    )


def _opposite_sign_count(
    primary: PerformanceStats, neighbors: Sequence[PerformanceStats]
) -> int:
    if primary.net_expectancy == 0:
        return 0
    return sum(
        item.trade_count > 0
        and item.net_expectancy * primary.net_expectancy < 0
        for item in neighbors
    )


def evaluate_development(
    *,
    primary_official: PerformanceStats,
    primary_stress: PerformanceStats,
    neighbor_official: Sequence[PerformanceStats],
    defects: DefectMetrics,
    contract: Mapping[str, object],
    preconditions_ok: bool = True,
) -> GateDecision:
    if not preconditions_ok:
        return GateDecision((), "NOT_RUN", None, ("P1_P5",))
    gate = require(contract, "development_gate")
    policy = require(contract, "defect_policy")
    opposite = _opposite_sign_count(primary_official, neighbor_official)
    items = (
        GateItem("D1", primary_official.trade_count,
                 primary_official.trade_count >= int(require(gate, "D1_min_trades"))),
        GateItem("D2", primary_official.trading_days_with_trade,
                 primary_official.trading_days_with_trade >= int(
                     require(gate, "D2_min_trading_days_with_trade"))),
        GateItem("D3", primary_official.net_expectancy,
                 primary_official.net_expectancy > 0),
        GateItem("D4", primary_official.profit_factor,
                 primary_official.profit_factor >= float(
                     require(gate, "D4_profit_factor_gte"))),
        GateItem("D5", primary_official.mdd,
                 abs(primary_official.mdd) <= float(require(gate, "D5_abs_mdd_lte"))),
        GateItem("D6", primary_official.positive_folds,
                 primary_official.positive_folds >= int(
                     require(gate, "D6_min_positive_folds"))),
        GateItem("D7", primary_stress.cumulative_net_pnl,
                 primary_stress.cumulative_net_pnl >= 0),
        GateItem("D8", opposite, opposite <= 1),
        GateItem("D9", {
            "data_ratio": defects.data_ratio,
            "operational_ratio": defects.operational_ratio,
            "major_defects": defects.major_defect_count,
        }, (
            defects.data_ratio <= float(require(policy, "data_exclusion_ratio_lte"))
            and defects.operational_ratio <= float(
                require(policy, "operational_failure_ratio_lte"))
            and defects.major_defect_count == int(
                require(policy, "major_defects_must_equal"))
        )),
    )
    failed = tuple(item.name for item in items if not item.passed)
    if "D9" in failed:
        return GateDecision(items, "INCONCLUSIVE_DATA", None, failed)
    if failed:
        return GateDecision(items, "NO_SELECTION", None, failed)
    return GateDecision(items, None, "blind_oos", ())
