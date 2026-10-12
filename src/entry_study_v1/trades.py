"""Fills, costs, holding horizons and MAE (PREREG §4). Costs come from `src.daytrade_paper.rules` constants."""
from __future__ import annotations

from dataclasses import dataclass

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import Bar
from src.entry_study_v1.bars import InvalidRun
from src.entry_study_v1.config import Cell, forced_exit_minute


@dataclass(frozen=True)
class CostSet:
    name: str
    c: float  # commission per side
    s: float  # slippage per side (applied to the fill price)
    f: float  # SEC fee, sells only (on the sell amount)

    def round_trip_flat(self) -> float:
        """Net return when the price does not move (a negative number)."""
        return net_return(1.0, 1.0, self)


COSTS = {
    "base": CostSet("base", R.COMMISSION, R.SLIPPAGE, R.SEC_FEE),
    "stress": CostSet("stress", R.COMMISSION * 1.5, R.SLIPPAGE * 1.5, R.SEC_FEE * 1.5),
    "fee_only_0.20": CostSet("fee_only_0.20", R.COMMISSION, 0.0, R.SEC_FEE),
    "fee_only_0.10": CostSet("fee_only_0.10", R.COMMISSION * 0.5, 0.0, R.SEC_FEE),
}


def net_return(open_e: float, open_x: float, cost: CostSet) -> float:
    """r = (Px / Pe) * (1 - c - f) - (1 + c), Pe = open_e * (1 + s), Px = open_x * (1 - s). Notional = buy amount
    before commission."""
    pe = open_e * (1 + cost.s)
    px = open_x * (1 - cost.s)
    return (px / pe) * (1 - cost.c - cost.f) - (1 + cost.c)


def exit_minute(entry_t: int, hold: int | str, forced: int) -> int:
    """30/60 minutes after the entry bar, capped at the forced exit (15:55 bar); "close" = forced exit."""
    if isinstance(hold, str):
        return forced
    return min(entry_t + hold, forced)


def hold_exits(cell: Cell, entry_t: int) -> dict[str, int]:
    forced = forced_exit_minute(cell)
    out = {}
    for name, spec in cell.holds.items():
        out[name] = exit_minute(entry_t, spec, forced)
    return out


def trade(cell: Cell, entry_t: int, etf_bars: dict[int, Bar], day: str,
          costs: dict[str, CostSet] = COSTS) -> dict[str, dict]:
    """Per holding name: {"exit_t", "mae", "r": {cost name: net return}} for one trade entered at `entry_t`.

    A missing ETF bar on a scored day voids the run. MAE = min(lows of bars e..x-1, open_x) / open_e - 1,
    with no slippage in the prices."""
    out: dict[str, dict] = {}
    if entry_t not in etf_bars:
        raise InvalidRun(f"{day}: {cell.etf} has no bar {entry_t}")
    open_e = etf_bars[entry_t].open
    for name, x in hold_exits(cell, entry_t).items():
        if x not in etf_bars:
            raise InvalidRun(f"{day}: {cell.etf} has no exit bar {x}")
        lows = []
        for t in range(entry_t, x):
            if t not in etf_bars:
                raise InvalidRun(f"{day}: {cell.etf} has no bar {t}")
            lows.append(etf_bars[t].low)
        open_x = etf_bars[x].open
        mae = min([*lows, open_x]) / open_e - 1
        out[name] = {"exit_t": x, "mae": mae,
                     "r": {cn: net_return(open_e, open_x, cs) for cn, cs in costs.items()}}
    return out
