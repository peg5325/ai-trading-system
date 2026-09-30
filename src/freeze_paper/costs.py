"""Paper fee model and derived official/stress cost views.

The recorded Paper ledger books commission + SEC fee inside FillEvent.fee so the
single FIFO engine keeps ``net == gross - slippage - commission``.  Official and
stress figures are read-only aggregations over recorded fills; they never
re-match lots or change positions.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from src.freeze_paper.binding import ContractBinding
from src.risk.models import SELL

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class CostModel:
    commission_bps: float
    sec_fee_pct_of_sell_notional: float
    sec_fee_minimum_usd: float

    @classmethod
    def from_binding(cls, binding: ContractBinding) -> CostModel:
        return cls(binding.commission_bps, binding.sec_fee_pct_of_sell_notional,
                   binding.sec_fee_minimum_usd)

    def commission(self, notional: float) -> float:
        return abs(notional) * self.commission_bps / 10_000.0

    def sec_fee(self, sell_notional: float) -> float:
        """max(minimum, sell notional * rate rounded up to the cent), in Decimal."""
        rate = Decimal(str(self.sec_fee_pct_of_sell_notional)) / Decimal(100)
        raw = (Decimal(str(abs(sell_notional))) * rate).quantize(_CENT, rounding=ROUND_CEILING)
        return float(max(Decimal(str(self.sec_fee_minimum_usd)), raw))

    def fee_parts(self, side: str, notional: float) -> dict[str, float]:
        sec = self.sec_fee(notional) if side == SELL else 0.0
        return {"commission": self.commission(notional), "sec_fee": sec}

    def fee(self, side: str, notional: float) -> float:
        parts = self.fee_parts(side, notional)
        return parts["commission"] + parts["sec_fee"]


def derived_cost_drags(fills: Iterable[Mapping], binding: ContractBinding) -> dict[str, float]:
    """Per-side incremental drags over recorded filled notional (report-only views)."""
    notional = sum(float(f["quantity"]) * float(f["price"]) for f in fills)
    return {
        "filled_notional": notional,
        "official_drag": notional * binding.official_increment_pct_points_per_side / 100.0,
        "stress_drag": notional * binding.stress_increment_pct_points_per_side / 100.0,
    }
