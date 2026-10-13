"""Costs, stop, sizing and the daily kill switch (spec §2, §3.3, §3.5; fractional sizing spec V1 §3.4)."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import V0, EngineConfig

START_EQUITY = 2000.0
SLIPPAGE = 0.02  # USD per share, against us on every fill
COMMISSION = 0.005  # USD per share per side
REF_COST_RATE = 0.001  # reference path: 0.10% of notional per side, no slippage (report only)
RISK_FRACTION = 0.01
ATR_MULT = 1.5
TP1_R = 1.5
SHARE_DP = 4  # fractional shares: 4 decimal places (spec V1 §3.4)
MIN_FRACTIONAL = 0.0001
KILL_LOSS_FRACTION = 0.025
KILL_LOSING_TRADES = 2


@dataclass(frozen=True)
class EntryPlan:
    side: int  # +1 long, -1 short
    raw: float  # next bar open
    fill: float  # raw +/- slippage
    stop: float
    risk_per_share: float  # R = |fill - stop|
    shares: int | float  # int under V0, 4-dp float when fractional
    target: float


class RiskManager:
    def __init__(self, slippage: float = SLIPPAGE, commission: float = COMMISSION,
                 config: EngineConfig = V0) -> None:
        self.slippage = slippage
        self.commission = commission
        self.config = config

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

    @staticmethod
    def fractional_shares(day_start_equity: float, cash: float, fill: float, risk_per_share: float) -> float:
        """Spec V1 §3.4: round(1% of day-start equity / R, 4); if that costs more than cash, floor(cash / fill) to
        4 dp; below 0.0001 means no entry (returned as 0.0)."""
        if risk_per_share <= 0 or fill <= 0 or cash <= 0:
            return 0.0
        n = round(RISK_FRACTION * day_start_equity / risk_per_share, SHARE_DP)
        if n * fill > cash:
            n = math.floor(cash / fill * 10 ** SHARE_DP) / 10 ** SHARE_DP
            while n > 0 and n * fill > cash:  # guard against float rounding in the floor
                n = round(n - MIN_FRACTIONAL, SHARE_DP)
        return n if n >= MIN_FRACTIONAL else 0.0

    def tp1_quantity(self, shares: float) -> float:
        """V0: floor(n / 2) (n = 1 -> all). Fractional: round(n / 2, 4) (0 -> all) (spec V1 §3.4)."""
        if self.config.fractional:
            q = round(shares / 2, SHARE_DP)
            return q if q > 0 else shares
        return shares // 2 if shares > 1 else shares

    def plan(self, side: int, raw: float, swing: float, atr: float, day_start_equity: float,
             cash: float) -> tuple[EntryPlan | None, str]:
        """Entry plan for a signal, or (None, reason) when the signal is skipped."""
        max_price = self.config.max_price
        if max_price is not None and raw > max_price:
            return None, "price_above_250"
        fill = self.entry_fill(side, raw)
        stop = self.initial_stop(side, fill, swing, atr)
        r = (fill - stop) * side
        if r <= 0:
            return None, "stop_beyond_fill"
        if self.config.fractional:
            n = self.fractional_shares(day_start_equity, cash, fill, r)
        else:
            n = self.shares(day_start_equity, cash, fill, r)
        if n <= 0:
            return None, "zero_shares"
        return EntryPlan(side, raw, fill, stop, r, n, fill + side * TP1_R * r), ""

    @staticmethod
    def kill_switch(day_start_equity: float, realized_today: float, losing_trades_today: int) -> bool:
        """True when new entries must stop for the rest of the day."""
        return (realized_today <= -KILL_LOSS_FRACTION * day_start_equity
                or losing_trades_today >= KILL_LOSING_TRADES)
