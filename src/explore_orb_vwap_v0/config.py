"""Engine configurations: V0 (spec V0, reproduced exactly) and V1 / V1_LONGONLY (spec V1 §1~§3).

V1 spec: docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V1.md. Every V1 change was chosen after seeing the V0
result on the same window (spec V1 §0): V1 results are in-sample post-hoc adjustments, not evidence of an edge.
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class EngineConfig:
    name: str
    universe: tuple[str, ...]
    max_price: float | None  # skip signals whose entry raw price is above this (None: no skip)
    fractional: bool  # False: integer shares (floor); True: shares rounded to 4 dp (spec V1 §3.4)
    direction_filter: bool  # daily EMA20 direction filter (spec V1 §3.2)
    breakout_volume_mult: float | None  # breakout bar volume >= mult * (OR 15-min volume / 15) (spec V1 §3.3)
    min_range_atr_mult: float | None  # exclude when ORB range < mult * prior daily ATR14 (spec V1 §3.3)
    allow_short: bool

    @property
    def daily_filters(self) -> bool:
        """True when the daily EMA20 / ATR14 must be ready for a symbol-day to be a candidate (spec V1 §3.1)."""
        return self.direction_filter or self.min_range_atr_mult is not None

    @property
    def has_v1_filters(self) -> bool:
        return (self.daily_filters or self.breakout_volume_mult is not None or not self.allow_short)


V0 = EngineConfig(
    name="V0",
    universe=("QQQ", "SPY", "IWM", "AMD", "AAPL", "AMZN", "PLTR"),
    max_price=250.0,
    fractional=False,
    direction_filter=False,
    breakout_volume_mult=None,
    min_range_atr_mult=None,
    allow_short=True,
)

V1 = EngineConfig(
    name="V1",
    universe=("QQQ", "SPY", "AMD", "AAPL", "AMZN", "PLTR"),
    max_price=None,
    fractional=True,
    direction_filter=True,
    breakout_volume_mult=1.3,
    min_range_atr_mult=0.15,
    allow_short=True,
)

V1_LONGONLY = replace(V1, name="V1_LONGONLY", allow_short=False)

CONFIGS = {c.name: c for c in (V0, V1, V1_LONGONLY)}
