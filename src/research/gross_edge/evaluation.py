"""Daily-clustered statistics and verdicts for gross-edge candidates (#124)."""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date

from src.research.gross_edge.engine import GrossTrade
from src.research.orb.windows import Fold, fold_of


@dataclass(frozen=True)
class CandidateResult:
    candidate_id: str
    trade_count: int
    trading_days: int
    mean_daily_gross_pct_points: float
    median_daily_gross_pct_points: float
    positive_days: int
    nonzero_days: int
    sign_test_raw_p: float
    sign_test_bonferroni_p: float
    fold_mean_gross_pct_points: Mapping[str, float]
    positive_folds: int
    net_after_cost_pct_points: Mapping[str, float]
    gross_edge_gates: Mapping[str, bool]
    gross_edge_pass: bool
    cost_survival: Mapping[str, bool]
    economic_pass: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def exact_positive_sign_p(positive: int, nonzero: int) -> float:
    """P[X >= positive], X~Binomial(nonzero, .5), exact one-sided."""
    if nonzero < 0 or positive < 0 or positive > nonzero:
        raise ValueError("invalid sign-test counts")
    if nonzero == 0:
        return 1.0
    numerator = sum(math.comb(nonzero, k) for k in range(positive, nonzero + 1))
    return numerator / (2**nonzero)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def evaluate_candidate(
    *,
    candidate_id: str,
    trades: Sequence[GrossTrade],
    folds: Sequence[Fold],
    contract: Mapping[str, object],
) -> CandidateResult:
    by_day: dict[date, list[float]] = defaultdict(list)
    for trade in trades:
        if trade.candidate_id == candidate_id:
            by_day[trade.day].append(trade.gross_return_pct_points)
    daily = {
        day: math.fsum(values) / len(values)
        for day, values in by_day.items()
        if values
    }
    daily_values = [daily[day] for day in sorted(daily)]
    mean_gross = math.fsum(daily_values) / len(daily_values) if daily_values else 0.0
    nonzero = sum(value != 0 for value in daily_values)
    positive = sum(value > 0 for value in daily_values)
    raw_p = exact_positive_sign_p(positive, nonzero)
    adjusted_p = min(raw_p * len(contract["candidates"]), 1.0)  # type: ignore[arg-type,index]

    fold_values: dict[str, list[float]] = {fold.name: [] for fold in folds}
    for day, value in daily.items():
        name = fold_of(day, folds)
        if name is not None:
            fold_values[name].append(value)
    fold_means = {
        name: (math.fsum(values) / len(values) if values else 0.0)
        for name, values in fold_values.items()
    }
    positive_folds = sum(value > 0 for value in fold_means.values())

    gate = contract["gross_edge_gate"]  # type: ignore[index]
    gates = {
        "G1_min_trades": len(trades) >= int(gate["min_trades"]),
        "G2_min_trading_days": len(daily) >= int(gate["min_trading_days"]),
        "G3_mean_gross_positive": mean_gross > float(gate["mean_gross_gt_pct_points"]),
        "G4_positive_folds": positive_folds >= int(gate["min_positive_folds"]),
        "G5_bonferroni_significance": adjusted_p < float(gate["adjusted_p_lt"]),
    }
    costs = contract["cost_hurdles_pct_points"]  # type: ignore[index]
    net_after = {name: mean_gross - float(value) for name, value in costs.items()}
    cost_survival = {name: value > 0 for name, value in net_after.items()}
    gross_pass = all(gates.values())
    economic_pass = gross_pass and cost_survival["conservative"]
    return CandidateResult(
        candidate_id=candidate_id,
        trade_count=len(trades),
        trading_days=len(daily),
        mean_daily_gross_pct_points=mean_gross,
        median_daily_gross_pct_points=_median(daily_values),
        positive_days=positive,
        nonzero_days=nonzero,
        sign_test_raw_p=raw_p,
        sign_test_bonferroni_p=adjusted_p,
        fold_mean_gross_pct_points=fold_means,
        positive_folds=positive_folds,
        net_after_cost_pct_points=net_after,
        gross_edge_gates=gates,
        gross_edge_pass=gross_pass,
        cost_survival=cost_survival,
        economic_pass=economic_pass,
    )


def overall_verdict(results: Sequence[CandidateResult]) -> str:
    if any(result.economic_pass for result in results):
        return "SHORTLIST"
    if any(result.gross_edge_pass for result in results):
        return "GROSS_EDGE_NOT_ECONOMIC"
    return "NO_GROSS_EDGE"
