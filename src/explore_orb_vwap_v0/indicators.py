"""Pure indicator helpers (spec §3 notation, §3.3).

Bars are `src.daytrade_paper.core.Bar` with `t` = bar START minute of day (ET). A complete regular day has one bar
per minute from 09:30 (570) to the close (exclusive), so bar index i is minute 570 + i.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.daytrade_paper.core import Bar

OPEN_MIN = 570  # 09:30 ET
ATR_PERIOD = 14
EMA_PERIOD = 20
FIVE = 5


def vwap_series(bars: list[Bar]) -> list[float | None]:
    """VWAP after each bar: sum(typical * volume) / sum(volume) from 09:30 through that bar, typical=(H+L+C)/3.
    None while the cumulative volume is 0 (no VWAP -> no signal on that bar)."""
    out: list[float | None] = []
    pv = vol = 0.0
    for b in bars:
        pv += (b.high + b.low + b.close) / 3.0 * b.volume
        vol += b.volume
        out.append(pv / vol if vol > 0 else None)
    return out


@dataclass
class FiveBar:
    """One 5-minute bar of a regular session plus the indicator values after it (None until seeded)."""

    start: int  # start minute of day (570, 575, ...)
    open: float
    high: float
    low: float
    close: float
    atr: float | None = None
    ema: float | None = None


def five_minute_bars(bars: list[Bar]) -> list[FiveBar]:
    """Group a complete regular day's 1m bars into 5-minute bars from 09:30 (09:30~09:34, 09:35~09:39, ...).
    A trailing group with fewer than 5 bars is dropped (never happens on a complete day)."""
    out: list[FiveBar] = []
    for k in range(len(bars) // FIVE):
        g = bars[k * FIVE:(k + 1) * FIVE]
        if g[0].t != OPEN_MIN + k * FIVE or g[-1].t != g[0].t + FIVE - 1:
            raise ValueError(f"5-minute group {k} is not contiguous from 09:30")
        out.append(FiveBar(g[0].t, g[0].open, max(b.high for b in g), min(b.low for b in g), g[-1].close))
    return out


def completed_five(t: int) -> int:
    """Number of today's 5-minute bars complete once the 1m bar starting at minute t has completed."""
    return max(0, (t - OPEN_MIN + 1) // FIVE)


def completes_five(t: int) -> bool:
    """True if the 1m bar starting at t is the last minute of a 5-minute bar."""
    return t >= OPEN_MIN and (t - OPEN_MIN) % FIVE == FIVE - 1


class IndicatorState:
    """ATR(14) Wilder and EMA(20) on completed 5-minute bars, continuing across days (spec §3.3).

    ATR: true range (the first bar after a reset has no previous close -> high - low); first value = simple mean
    of the first 14 TRs, then atr = (atr * 13 + tr) / 14. EMA: first value = simple mean of the first 20 closes,
    then ema += 2 / 21 * (close - ema).
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.prev_close: float | None = None
        self.tr_seed: list[float] = []
        self.atr: float | None = None
        self.close_seed: list[float] = []
        self.ema: float | None = None

    @property
    def warm(self) -> bool:
        return self.atr is not None and self.ema is not None

    def update(self, high: float, low: float, close: float) -> tuple[float | None, float | None]:
        pc = self.prev_close
        tr = high - low if pc is None else max(high - low, abs(high - pc), abs(low - pc))
        if self.atr is None:
            self.tr_seed.append(tr)
            if len(self.tr_seed) == ATR_PERIOD:
                self.atr = sum(self.tr_seed) / ATR_PERIOD
        else:
            self.atr = (self.atr * (ATR_PERIOD - 1) + tr) / ATR_PERIOD
        if self.ema is None:
            self.close_seed.append(close)
            if len(self.close_seed) == EMA_PERIOD:
                self.ema = sum(self.close_seed) / EMA_PERIOD
        else:
            self.ema += 2.0 / (EMA_PERIOD + 1) * (close - self.ema)
        self.prev_close = close
        return self.atr, self.ema

    def feed_day(self, five: list[FiveBar]) -> None:
        """Update with a day's 5-minute bars in order; store the values after each bar on that bar."""
        for f in five:
            f.atr, f.ema = self.update(f.high, f.low, f.close)
