"""Per symbol-day preparation and the 09:45 candidate / watchlist filter (spec §1, §3.1)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date

from src.daytrade_paper.core import Bar
from src.history.exchange_calendar import regular_close_minute

from .data_loader import is_complete
from .indicators import (
    FiveBar,
    IndicatorState,
    daily_bar,
    five_minute_bars,
    vwap_series,
)

ORB_BARS = 15  # 09:30 ~ 09:44
RVOL_LOOKBACK = 20
RVOL_MIN = 1.2
GAP_MIN = 0.003
GAP_MAX = 0.02
WATCHLIST_SIZE = 2
RS_REF = "SPY"


@dataclass
class SymbolDay:
    """One symbol on one trading day. Fields other than `complete` are set only for complete days."""

    symbol: str
    day: str
    complete: bool
    has_data: bool = False  # any REGULAR bar present (complete or not)
    bars: list[Bar] = field(default_factory=list)
    close_minute: int = 0
    prior_close: float | None = None  # previous trading day's last regular close (None unless that day complete)
    rvol_base: float | None = None  # mean 09:30~09:44 volume over the prior 20 complete sessions
    warm: bool = False  # ATR/EMA seeded from earlier complete days and continuous into this day
    orb_high: float = 0.0
    orb_low: float = 0.0
    orb_volume: float = 0.0
    vwap: list[float | None] = field(default_factory=list)
    five: list[FiveBar] = field(default_factory=list)
    # Daily EMA20 / ATR14 through the PRIOR complete trading day (spec V1 §3.1); None unless seeded and continuous
    # into today. Not used by the V0 configuration.
    daily_ema: float | None = None
    daily_atr: float | None = None
    # Spec V4 §3: |open / prior close - 1| >= split_gap (only when prepare_symbol gets split_gap). The day is not a
    # candidate and every indicator / RVOL history restarts from it.
    split_break: bool = False

    @property
    def ready(self) -> bool:
        """Usable as a candidate today: complete, prior close known, RVOL base known, indicators warm."""
        return self.complete and self.prior_close is not None and self.rvol_base is not None and self.warm

    def is_ready(self, need_rvol: bool = True) -> bool:
        """`ready`, or without the RVOL base requirement when `need_rvol` is False (spec V3 §2)."""
        if need_rvol:
            return self.ready
        return self.complete and self.prior_close is not None and self.warm

    @property
    def daily_ready(self) -> bool:
        """Prior-day daily EMA20, ATR14 and close all known (spec V1 §3.1)."""
        return self.prior_close is not None and self.daily_ema is not None and self.daily_atr is not None

    @property
    def direction(self) -> int | None:
        """Spec V1 §3.2: +1 (long only) if prior close > prior daily EMA20, -1 (short only) if <, 0 if equal (no
        trade), None when the daily values are not ready."""
        if not self.daily_ready:
            return None
        return 1 if self.prior_close > self.daily_ema else -1 if self.prior_close < self.daily_ema else 0


def orb(bars: list[Bar]) -> tuple[float, float, float]:
    """(ORB_High, ORB_Low, volume) of the 15 bars 09:30~09:44."""
    first = bars[:ORB_BARS]
    return max(b.high for b in first), min(b.low for b in first), sum(b.volume for b in first)


def prepare_symbol(symbol: str, regular: dict[str, list[Bar]], trading_days: list[Date],
                   split_gap: float | None = None) -> dict[str, SymbolDay]:
    """Build SymbolDay for every calendar trading day, in order.

    Continuity rules (no filling, no stitching across gaps): the ATR/EMA state is reset whenever the previous
    trading day was not complete for this symbol, and a day is `warm` only if the state was seeded and continuous
    at its open. Prior close requires the previous trading day to be complete. RVOL uses the 20 most recent
    earlier complete sessions (they need not be consecutive). The daily EMA20 / ATR14 (spec V1 §3.1) follow the
    same reset rule on daily bars built from the complete 1m session; a day gets the values after the prior day.

    `split_gap` (spec V4 §3, None = off as in V0 ~ V3): a complete day whose first open is at least that far from the
    previous complete close is a data break, handled like a day after an incomplete one (no prior close, ATR/EMA and
    daily state reset) and the RVOL history is cleared too.
    """
    out: dict[str, SymbolDay] = {}
    state = IndicatorState()
    daily = IndicatorState()
    orb_vols: list[float] = []
    prev: SymbolDay | None = None
    for d in trading_days:
        key = d.isoformat()
        bars = regular.get(key, [])
        sd = SymbolDay(symbol, key, is_complete(bars, d), bool(bars))
        if sd.complete:
            sd.bars = bars
            sd.close_minute = regular_close_minute(d)
            prev_ok = prev is not None and prev.complete
            if (prev_ok and split_gap is not None
                    and abs(bars[0].open / prev.bars[-1].close - 1.0) >= split_gap):
                sd.split_break = True
                prev_ok = False
                orb_vols.clear()
            if not prev_ok:
                state.reset()
                daily.reset()
            if prev_ok:  # each stays None until seeded; daily_ready needs both
                sd.daily_ema, sd.daily_atr = daily.ema, daily.atr
            _o, dh, dl, dc = daily_bar(bars)
            daily.update(dh, dl, dc)
            sd.warm = prev_ok and state.warm
            sd.prior_close = prev.bars[-1].close if prev_ok else None
            if len(orb_vols) >= RVOL_LOOKBACK:
                sd.rvol_base = sum(orb_vols[-RVOL_LOOKBACK:]) / RVOL_LOOKBACK
            sd.orb_high, sd.orb_low, sd.orb_volume = orb(bars)
            sd.vwap = vwap_series(bars)
            sd.five = five_minute_bars(bars)
            state.feed_day(sd.five)
            orb_vols.append(sd.orb_volume)
        else:
            state.reset()
            daily.reset()
        out[key] = sd
        prev = sd
    return out


@dataclass(frozen=True)
class Candidate:
    symbol: str
    rvol: float | None  # None only without the RVOL filter when the 20-session base is not known yet
    gap: float
    rs: float


class UniverseFilter:
    """RVOL >= 1.2, 0.3% <= |gap| <= 2.0%, then the top-2 |RS| vs SPY (ties by symbol; all RS 0 without SPY).

    `rvol_min` None drops the RVOL rule and the RVOL readiness requirement; `gap_min` / `gap_max` None drop the gap
    rule (spec V3 §1)."""

    def __init__(self, rvol_min: float | None = RVOL_MIN, gap_min: float | None = GAP_MIN,
                 gap_max: float | None = GAP_MAX, size: int = WATCHLIST_SIZE) -> None:
        if (gap_min is None) != (gap_max is None):
            raise ValueError("gap_min and gap_max must both be set or both be None")
        self.rvol_min, self.gap_min, self.gap_max, self.size = rvol_min, gap_min, gap_max, size

    @staticmethod
    def metrics(sd: SymbolDay, spy: SymbolDay | None, need_rvol: bool = True) -> Candidate | None:
        """None unless both symbol-days are ready enough (spy only needs complete + prior close).
        `spy` None: no RS reference (spec V2 §1.3), RS is 0. `need_rvol` False: the RVOL base may be missing
        (rvol is then None)."""
        if need_rvol:
            if not sd.ready or not sd.rvol_base:
                return None
        elif not sd.is_ready(False):
            return None
        if spy is not None and (not spy.complete or spy.prior_close is None):
            return None
        rvol = sd.orb_volume / sd.rvol_base if sd.rvol_base else None
        gap = sd.bars[0].open / sd.prior_close - 1.0
        if spy is None:
            return Candidate(sd.symbol, rvol, gap, 0.0)
        ret = sd.bars[ORB_BARS - 1].close / sd.prior_close - 1.0
        spy_ret = spy.bars[ORB_BARS - 1].close / spy.prior_close - 1.0
        rs = 0.0 if sd.symbol == RS_REF else ret - spy_ret
        return Candidate(sd.symbol, rvol, gap, rs)

    def passes(self, c: Candidate) -> bool:
        if self.rvol_min is not None and not c.rvol >= self.rvol_min:
            return False
        return self.gap_min is None or self.gap_min <= abs(c.gap) <= self.gap_max

    def watchlist(self, day_rows: dict[str, SymbolDay], spy: SymbolDay | None) -> list[Candidate]:
        cands = []
        for sym in sorted(day_rows):
            c = self.metrics(day_rows[sym], spy, need_rvol=self.rvol_min is not None)
            if c is not None and self.passes(c):
                cands.append(c)
        cands.sort(key=lambda c: (-abs(c.rs), c.symbol))
        return cands[:self.size]


class SniperFilter:
    """Spec V4 §4.1: among ready, daily-ready symbol-days with prior close > prior daily EMA20, gap >= gap_up_min
    (signed) and RVOL >= rvol_min, keep the single highest RVOL (ties by symbol)."""

    def __init__(self, rvol_min: float, gap_up_min: float) -> None:
        self.rvol_min, self.gap_up_min = rvol_min, gap_up_min

    def passing(self, day_rows: dict[str, SymbolDay]) -> list[Candidate]:
        """All passing candidates, highest RVOL first."""
        cands = []
        for sym in sorted(day_rows):
            sd = day_rows[sym]
            if not sd.daily_ready or sd.direction != 1:
                continue
            c = UniverseFilter.metrics(sd, None)
            if c is not None and c.rvol >= self.rvol_min and c.gap >= self.gap_up_min:
                cands.append(c)
        cands.sort(key=lambda c: (-c.rvol, c.symbol))
        return cands

    def watchlist(self, day_rows: dict[str, SymbolDay], spy: SymbolDay | None = None) -> list[Candidate]:
        return self.passing(day_rows)[:1]
