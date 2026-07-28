"""ORB 비용 3수준 — 동일 체결에 비용만 한 번 적용한다."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.research.orb.contract import _require


@dataclass(frozen=True)
class CostedPnl:
    level: str
    round_trip_pct_points: float
    entry_cost: float
    exit_cost: float
    gross_pnl: float
    net_pnl: float


def cost_levels(contract: Mapping[str, object]) -> Mapping[str, float]:
    levels = _require(contract, "costs", "round_trip_pct_points")
    return {str(k): float(v) for k, v in levels.items()}


def pnl_at_cost(
    *,
    entry_price: float,
    exit_price: float,
    qty: int,
    level: str,
    round_trip_pct_points: float,
) -> CostedPnl:
    """왕복 비용을 진입·청산 명목금액에 절반씩 배분한다."""
    half_rate = (round_trip_pct_points / 100.0) / 2.0
    entry_cost = entry_price * qty * half_rate
    exit_cost = exit_price * qty * half_rate
    gross = (exit_price - entry_price) * qty
    return CostedPnl(
        level=level,
        round_trip_pct_points=round_trip_pct_points,
        entry_cost=entry_cost,
        exit_cost=exit_cost,
        gross_pnl=gross,
        net_pnl=gross - entry_cost - exit_cost,
    )


def all_costed_pnls(
    contract: Mapping[str, object], *, entry_price: float, exit_price: float, qty: int
) -> Mapping[str, CostedPnl]:
    return {
        name: pnl_at_cost(
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            level=name,
            round_trip_pct_points=pct,
        )
        for name, pct in cost_levels(contract).items()
    }
