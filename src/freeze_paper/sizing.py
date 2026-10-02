"""Literal port of contract ``portfolio.sizing_procedure`` (v1.3, fractional).

    cap = max_exposure_pct * E; used = 0
    for each eligible candidate with k remaining (including itself):
        room = cap - used; budget = room / k
        qty = floor_to_6dp(budget / cash_per_share)
        if qty * entry_fill_price < minimum_order_notional_usd: NO_TRADE_SYMBOL
        used += qty * cash_per_share

The arithmetic runs in ``decimal.Decimal`` on the shortest decimal form (``str``) of the
float inputs, with every rounding toward floor, so the 6-dp quantity never
drifts above the budget and ``used`` never exceeds ``cap``.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import ROUND_FLOOR, Decimal, localcontext

from src.freeze_paper.binding import ContractBinding

EXPOSURE_ROOM = "EXPOSURE_ROOM"
BELOW_MIN_ORDER_NOTIONAL = "BELOW_MIN_ORDER_NOTIONAL"


@dataclass(frozen=True)
class SizedEntry:
    symbol: str
    rank: int
    reference_open: float
    entry_fill_price: float
    cash_per_share: float
    quantity: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class NoTradeSymbol:
    symbol: str
    rank: int
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def entry_fill_price(reference_open: float, binding: ContractBinding) -> float:
    return reference_open * (1 + binding.slippage_bps / 10_000.0)


def cash_per_share(reference_open: float, binding: ContractBinding) -> float:
    return entry_fill_price(reference_open, binding) * (1 + binding.commission_bps / 10_000.0)


def size_entries(
    equity: float,
    eligible: Sequence[tuple[str, int, float]],
    binding: ContractBinding,
) -> tuple[list[SizedEntry], list[NoTradeSymbol]]:
    """``eligible`` = (symbol, rank, reference_open) in rank order, all with a 09:35 bar."""
    step = Decimal(1).scaleb(-binding.quantity_decimals)
    minimum = Decimal(str(binding.min_order_notional_usd))
    sized: list[SizedEntry] = []
    skipped: list[NoTradeSymbol] = []
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_FLOOR
        cap = Decimal(str(binding.max_exposure_pct)) * Decimal(str(equity))
        used = Decimal(0)
        for index, (symbol, rank, reference_open) in enumerate(eligible):
            fill = entry_fill_price(reference_open, binding)
            per_share = cash_per_share(reference_open, binding)
            budget = (cap - used) / (len(eligible) - index)
            qty = (budget / Decimal(str(per_share))).quantize(step, rounding=ROUND_FLOOR)
            if qty <= 0:
                skipped.append(NoTradeSymbol(symbol, rank, EXPOSURE_ROOM))
                continue
            if qty * Decimal(str(fill)) < minimum:
                skipped.append(NoTradeSymbol(symbol, rank, BELOW_MIN_ORDER_NOTIONAL))
                continue
            used += qty * Decimal(str(per_share))
            sized.append(SizedEntry(symbol, rank, reference_open, fill, per_share, float(qty)))
    return sized, skipped
