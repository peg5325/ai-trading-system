"""Costs, stop, sizing and the daily kill switch (spec §2, §3.3, §3.5)."""
from __future__ import annotations

import math
from dataclasses import dataclass

START_EQUITY = 2000.0
SLIPPAGE = 0.02  # USD per share, against us on every fill
COMMISSION = 0.005  # USD per share per side
REF_COST_RATE = 0.001  # reference path: 0.10% of notional per side, no slippage (report only)
MAX_PRICE = 250.0
RISK_FRACTION = 0.01
ATR_MULT = 1.5
TP1_R = 1.5
KILL_LOSS_FRACTION = 0.025
KILL_LOSING_TRADES = 2


@dataclass(frozen=True)
class EntryPlan:
    side: int  # +1 long, -1 short
    raw: float  # next bar open
    fill: float  # raw +/- slippage
    stop: float
    risk_per_share: float  # R = |fill - stop|
    shares: int
    target: float


class RiskManager:
    def __init__(self, slippage: float = SLIPPAGE, commission: float = COMMISSION) -> None:
        self.slippage = slippage
        self.commission = commission

    def entry_fill(self, side: int, raw: float) -> float:
        return raw + side * self.slippage

    def exit_fill(self, side: int, raw: float) -> float:
        return raw - side * self.slippage

    @staticmethod
    def initial_stop(side: int, fill: float, swing: float, atr: float) -> float:
        """Long: max(swing low, fill - 1.5 ATR); short: min(swing high, fill + 1.5 ATR) (the nearer one)."""
        if side > 0:
            return max(swing, fill - ATR_MULT * atr)
        return min(swing, fill + ATR_MULT * atr)

    @staticmethod
    def shares(day_start_equity: float, cash: float, fill: float, risk_per_share: float) -> int:
        """floor(1% of day-start equity / R), then reduced so shares * fill <= cash. 0 means no entry."""
        if risk_per_share <= 0 or fill <= 0 or cash <= 0:
            return 0
        n = math.floor(RISK_FRACTION * day_start_equity / risk_per_share)
        n = min(n, math.floor(cash / fill))
        return max(n, 0)

    def plan(self, side: int, raw: float, swing: float, atr: float, day_start_equity: float,
             cash: float) -> tuple[EntryPlan | None, str]:
        """Entry plan for a signal, or (None, reason) when the signal is skipped."""
        if raw > MAX_PRICE:
            return None, "price_above_250"
        fill = self.entry_fill(side, raw)
        stop = self.initial_stop(side, fill, swing, atr)
        r = (fill - stop) * side
        if r <= 0:
            return None, "stop_beyond_fill"
        n = self.shares(day_start_equity, cash, fill, r)
        if n <= 0:
            return None, "zero_shares"
        return EntryPlan(side, raw, fill, stop, r, n, fill + side * TP1_R * r), ""

    @staticmethod
    def kill_switch(day_start_equity: float, realized_today: float, losing_trades_today: int) -> bool:
        """True when new entries must stop for the rest of the day."""
        return (realized_today <= -KILL_LOSS_FRACTION * day_start_equity
                or losing_trades_today >= KILL_LOSING_TRADES)
