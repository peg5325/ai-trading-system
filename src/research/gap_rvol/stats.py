"""Gap+RVOL development fold statistics."""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from src.research.gap_rvol.contract import require
from src.research.gap_rvol.execution import TradeRecord
from src.research.gap_rvol.portfolio import family_for
from src.research.gap_rvol.windows import Fold, fold_of


@dataclass(frozen=True)
class PerformanceStats:
    cost_level: str
    trade_count: int
    entered_trade_count: int
    trading_days_with_trade: int
    cumulative_net_pnl: float
    net_expectancy: float
    net_expectancy_per_r: float
    profit_factor: float
    mdd: float
    equity_curve: tuple[float, ...]
    fold_pnl: Mapping[str, float]
    positive_folds: int
    family_profit_share: Mapping[str, float]
    day_profit_share: Mapping[str, float]


def maximum_drawdown(curve: Sequence[float]) -> float:
    if not curve:
        return 0.0
    peak = float(curve[0])
    worst = 0.0
    for value in curve:
        peak = max(peak, float(value))
        if peak > 0:
            worst = max(worst, (peak - float(value)) / peak)
    return worst


def profit_factor(net_pnls: Sequence[float]) -> float:
    gains = sum(value for value in net_pnls if value > 0)
    losses = sum(value for value in net_pnls if value < 0)
    if losses == 0:
        return math.inf
    return gains / abs(losses)


def _shares(values: Mapping[str, float], total: float) -> Mapping[str, float]:
    if total <= 0:
        return {key: math.inf for key in sorted(values)}
    return {key: values[key] / total for key in sorted(values)}


def compute_stats(
    *,
    trades: Sequence[TradeRecord],
    evaluation_days: Sequence[date],
    folds: Sequence[Fold],
    cost_level: str,
    contract: Mapping[str, object],
) -> PerformanceStats:
    closable = [
        trade for trade in trades
        if trade.exit is not None and cost_level in trade.costs
    ]
    net = [trade.costs[cost_level].net_pnl for trade in closable]
    initial = float(require(contract, "account_model", "initial_capital"))
    by_day: dict[date, float] = defaultdict(float)
    by_family: dict[str, float] = defaultdict(float)
    risk_units: list[float] = []
    for trade in closable:
        value = trade.costs[cost_level].net_pnl
        by_day[trade.position.day] += value
        by_family[family_for(trade.position.symbol, contract)] += value
        risk = trade.position.one_r * trade.position.qty
        risk_units.append(value / risk if risk > 0 else 0.0)

    curve = [initial]
    cash = initial
    for day in evaluation_days:
        cash += by_day.get(day, 0.0)
        curve.append(cash)

    fold_pnl = {fold.name: 0.0 for fold in folds}
    for day, value in by_day.items():
        name = fold_of(day, folds)
        if name is not None:
            fold_pnl[name] += value
    total = sum(net)
    return PerformanceStats(
        cost_level=cost_level,
        trade_count=len(closable),
        entered_trade_count=len(trades),
        trading_days_with_trade=len(by_day),
        cumulative_net_pnl=total,
        net_expectancy=(total / len(closable)) if closable else 0.0,
        net_expectancy_per_r=(
            sum(risk_units) / len(risk_units) if risk_units else 0.0
        ),
        profit_factor=profit_factor(net),
        mdd=maximum_drawdown(curve),
        equity_curve=tuple(curve),
        fold_pnl=fold_pnl,
        positive_folds=sum(value > 0 for value in fold_pnl.values()),
        family_profit_share=_shares(by_family, total),
        day_profit_share=_shares(
            {day.isoformat(): value for day, value in by_day.items()}, total
        ),
    )
