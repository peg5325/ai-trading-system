"""Literal port of contract ``portfolio.sizing_procedure``.

    cap = max_exposure_pct * E; used = 0
    for each eligible candidate with k remaining (including itself):
        room = cap - used; budget = room / k
        qty = floor(budget / cash_per_share + 1e-9)
        if qty == 0 and cash_per_share <= room: qty = 1
        if qty == 0: NO_TRADE_SYMBOL(EXPOSURE_ROOM)
        used += qty * cash_per_share
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from src.freeze_paper.binding import ContractBinding

QTY_FLOOR_EPS = 1e-9
EXPOSURE_ROOM = "EXPOSURE_ROOM"


@dataclass(frozen=True)
class SizedEntry:
    symbol: str
    rank: int
    reference_open: float
    entry_fill_price: float
    cash_per_share: float
    quantity: int

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
    cap = binding.max_exposure_pct * equity
    used = 0.0
    sized: list[SizedEntry] = []
    skipped: list[NoTradeSymbol] = []
    for index, (symbol, rank, reference_open) in enumerate(eligible):
        per_share = cash_per_share(reference_open, binding)
        room = cap - used
        budget = room / (len(eligible) - index)
        qty = math.floor(budget / per_share + QTY_FLOOR_EPS)
        if qty == 0 and per_share <= room:
            qty = 1
        if qty == 0:
            skipped.append(NoTradeSymbol(symbol, rank, EXPOSURE_ROOM))
            continue
        used += qty * per_share
        sized.append(SizedEntry(symbol, rank, reference_open,
                                entry_fill_price(reference_open, binding), per_share, qty))
    return sized, skipped
