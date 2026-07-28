"""ORB D1~D9/O1~O11 기계 판정 — 계약 JSON 수치만 사용."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from src.research.orb.contract import _require
from src.research.orb.features import DATA_EXCLUSION_REASONS
from src.research.orb.simulator import DaySimulationResult
from src.research.orb.stats import PerformanceStats

OPERATIONAL_FAILURE_REASONS = frozenset({
    "stale_exit",
    "eod_fallback",
    "unclosable",
})


@dataclass(frozen=True)
class DefectMetrics:
    data_excluded_symbol_days: int
    total_symbol_days: int
    operational_failure_trades: int
    entered_trades: int
    major_defect_count: int = 0
    conflict_total: int = 28

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
    criterion: object
    actual: object
    passed: bool


@dataclass(frozen=True)
class GateDecision:
    items: tuple[GateItem, ...]
    development_gate: str
    verdict: str | None
    next_stage: str | None
    failed: tuple[str, ...]


def build_defect_metrics(
    *,
    day_results: Sequence[DaySimulationResult],
    total_symbol_days: int,
    major_defect_count: int,
    conflict_total: int,
) -> DefectMetrics:
    """Primary 결과에서 [A]/[B]를 서로 다른 단위로 집계한다.

    [A]는 폐쇄된 데이터 사유가 하나 이상인 고유 종목-일이고, 전략 임계
    미달·용량 skip·운영 실패는 포함하지 않는다. [B]는 진입 거래 중 운영
    실패 거래다. `unclosable`도 진입 거래이므로 분모에 남는다.
    """
    data_symbol_days = {
        (result.day, symbol)
        for result in day_results
        for symbol, reason in result.excluded
        if reason in DATA_EXCLUSION_REASONS
    }
    trades = [
        trade
        for result in day_results
        for trade in result.trades
    ]
    return DefectMetrics(
        data_excluded_symbol_days=len(data_symbol_days),
        total_symbol_days=total_symbol_days,
        operational_failure_trades=sum(
            trade.operational_reason in OPERATIONAL_FAILURE_REASONS
            for trade in trades
        ),
        entered_trades=len(trades),
        major_defect_count=major_defect_count,
        conflict_total=conflict_total,
    )


def _opposite_sign_count(
    primary: PerformanceStats,
    neighbors: Sequence[PerformanceStats],
) -> int:
    if primary.net_expectancy == 0:
        return 0
    return sum(
        neighbor.trade_count > 0
        and neighbor.net_expectancy * primary.net_expectancy < 0
        for neighbor in neighbors
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
    """P1~P3와 D1~D9를 결합하되 절차 실패를 전략 실패로 위장하지 않는다."""
    if not preconditions_ok:
        return GateDecision(
            items=(), development_gate="NOT_RUN", verdict="NOT_RUN",
            next_stage=None, failed=("P1_P3",))

    g = _require(contract, "development_gate")
    d = _require(contract, "defect_policy")
    data_threshold = float(_require(d, "escalation_ratio", "threshold"))
    operational_threshold = float(_require(d, "operational_failure_ratio", "threshold"))
    baseline = int(_require(d, "duplicate_ts_conflict_baseline", "manifest_conflict_total"))
    opposite = _opposite_sign_count(primary_official, neighbor_official)
    d9 = (
        defects.data_ratio <= data_threshold
        and defects.operational_ratio <= operational_threshold
        and defects.major_defect_count == 0
        and defects.conflict_total <= baseline
    )
    items = (
        GateItem("D1", f">={int(_require(g, 'D1_min_trades'))}",
                 primary_official.trade_count,
                 primary_official.trade_count >= int(_require(g, "D1_min_trades"))),
        GateItem("D2", f">={int(_require(g, 'D2_min_trading_days_with_trade'))}",
                 primary_official.trading_days_with_trade,
                 primary_official.trading_days_with_trade
                 >= int(_require(g, "D2_min_trading_days_with_trade"))),
        GateItem("D3", ">0", primary_official.net_expectancy,
                 primary_official.net_expectancy > float(_require(g, "D3_net_expectancy_gt"))),
        GateItem("D4", f">={float(_require(g, 'D4_profit_factor_gte'))}",
                 primary_official.profit_factor,
                 primary_official.profit_factor >= float(_require(g, "D4_profit_factor_gte"))),
        GateItem("D5", f"<={float(_require(g, 'D5_abs_mdd_lte'))}",
                 primary_official.mdd,
                 abs(primary_official.mdd) <= float(_require(g, "D5_abs_mdd_lte"))),
        GateItem("D6", f">={int(_require(g, 'D6_min_positive_folds'))}",
                 primary_official.positive_folds,
                 primary_official.positive_folds
                 >= int(_require(g, "D6_min_positive_folds"))),
        GateItem("D7", f">={float(_require(g, 'D7_stress_cumulative_pnl_gte'))}",
                 primary_stress.cumulative_net_pnl,
                 primary_stress.cumulative_net_pnl
                 >= float(_require(g, "D7_stress_cumulative_pnl_gte"))),
        GateItem("D8", "<=1 opposite-sign neighbors", opposite, opposite <= 1),
        GateItem("D9", "defect policy", {
            "data_ratio": defects.data_ratio,
            "operational_ratio": defects.operational_ratio,
            "major_defect_count": defects.major_defect_count,
            "conflict_total": defects.conflict_total,
        }, d9),
    )
    failed = tuple(item.name for item in items if not item.passed)
    if not d9:
        return GateDecision(
            items=items, development_gate="FAIL", verdict="INCONCLUSIVE_DATA",
            next_stage=None, failed=failed)
    if failed:
        return GateDecision(
            items=items, development_gate="FAIL", verdict="NO_SELECTION",
            next_stage=None, failed=failed)
    return GateDecision(
        items=items, development_gate="PASS", verdict=None,
        next_stage="blind_oos", failed=())


def evaluate_oos(
    *,
    primary_official: PerformanceStats,
    primary_stress: PerformanceStats,
    neighbor_official: Sequence[PerformanceStats],
    positive_months: int,
    major_defect_count: int,
    contract: Mapping[str, object],
) -> tuple[GateItem, ...]:
    """OOS 결과가 주어졌을 때만 쓰는 순수 판정 함수. 4-B/4-C는 호출하지 않는다."""
    g = _require(contract, "oos_gate")
    opposite = _opposite_sign_count(primary_official, neighbor_official)
    max_family = max(primary_official.family_profit_share.values(), default=math.inf)
    max_day = max(primary_official.day_profit_share.values(), default=math.inf)
    return (
        GateItem("O1", ">0", primary_official.net_expectancy,
                 primary_official.net_expectancy > float(_require(g, "O1_net_expectancy_gt"))),
        GateItem("O2", f">={float(_require(g, 'O2_profit_factor_gte'))}",
                 primary_official.profit_factor,
                 primary_official.profit_factor >= float(_require(g, "O2_profit_factor_gte"))),
        GateItem("O3", f">={int(_require(g, 'O3_min_trading_days_with_trade'))}",
                 primary_official.trading_days_with_trade,
                 primary_official.trading_days_with_trade
                 >= int(_require(g, "O3_min_trading_days_with_trade"))),
        GateItem("O4", f">={int(_require(g, 'O4_min_trades'))}",
                 primary_official.trade_count,
                 primary_official.trade_count >= int(_require(g, "O4_min_trades"))),
        GateItem("O5", f">={int(_require(g, 'O5_min_positive_months'))}",
                 positive_months,
                 positive_months >= int(_require(g, "O5_min_positive_months"))),
        GateItem("O6", f"<={float(_require(g, 'O6_abs_mdd_lte'))}",
                 primary_official.mdd,
                 primary_official.mdd <= float(_require(g, "O6_abs_mdd_lte"))),
        GateItem("O7", f">={float(_require(g, 'O7_stress_cumulative_pnl_gte'))}",
                 primary_stress.cumulative_net_pnl,
                 primary_stress.cumulative_net_pnl
                 >= float(_require(g, "O7_stress_cumulative_pnl_gte"))),
        GateItem("O8", f"<={float(_require(g, 'O8_max_single_symbol_profit_share'))}",
                 max_family,
                 max_family <= float(_require(g, "O8_max_single_symbol_profit_share"))),
        GateItem("O9", f"<={float(_require(g, 'O9_max_single_day_profit_share'))}",
                 max_day, max_day <= float(_require(g, "O9_max_single_day_profit_share"))),
        GateItem("O10", "<=1 opposite-sign neighbors", opposite, opposite <= 1),
        GateItem("O11", "==0", major_defect_count, major_defect_count == 0),
    )


def oos_months(contract: Mapping[str, object]) -> tuple[tuple[str, date, date], ...]:
    """달력월이 아닌 사전등록 OOS 시작일 앵커 6구간."""
    return tuple(
        (str(item["month"]), date.fromisoformat(str(item["start"])),
         date.fromisoformat(str(item["end"])))
        for item in _require(contract, "oos_gate", "O5_month_boundaries")
    )


def disqualified_non_primary_specs(spec_names: Sequence[str]) -> tuple[str, ...]:
    """이 스냅샷에서 primary 외 모든 ORB 파라미터화를 영구 실격 표기로 반환."""
    return tuple(sorted(name for name in spec_names if name != "primary"))
