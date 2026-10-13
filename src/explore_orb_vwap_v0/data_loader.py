"""1m regular-session bars via the research gate only (spec §1).

Prices come only through `src.history.oos_guard.research_load(..., purpose="performance", track=TRACK_INTRADAY,
market="US")`, in month chunks. This module additionally refuses any request whose end reaches 2026-01-21 00:00 ET
(the sealed block 2026-01-21 ~ 2026-07-21 and everything after it are not used).

DB `ts_exchange` is the bar END minute; `src.entry_study_v1.bars.candle_bar` (wrapping `store._bar`) converts it
to the bar start and classes the session by the exchange calendar. Only REGULAR bars are kept.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta

from src.daytrade_paper.core import Bar
from src.entry_study_v1.bars import candle_bar
from src.history.exchange_calendar import CALENDAR_START, regular_close_minute
from src.history.oos_guard import TRACK_INTRADAY, research_load
from src.models import ET

from .indicators import OPEN_MIN

DEV_CUTOFF = datetime(2026, 1, 21, tzinfo=ET)
DEV_LAST_DAY = Date(2026, 1, 20)
# The exchange calendar (used to class sessions and early closes) starts 2023-01-01; earlier bars cannot be
# classified, so loading starts there. SPY 1m data starts 2023-04-03, so earlier days could not be scored anyway.
LOAD_START = CALENDAR_START
UNIVERSE = ("QQQ", "SPY", "IWM", "AMD", "AAPL", "AMZN", "PLTR")


class DevBoundError(Exception):
    """A load reached 2026-01-21 00:00 ET or later (or the request was otherwise outside the allowed window)."""


def et_midnight(d: Date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=ET)


def assert_dev_window(lo: datetime, hi: datetime) -> None:
    """Loads must lie strictly before 2026-01-21 00:00 ET (`hi` is exclusive)."""
    if lo >= DEV_CUTOFF or hi > DEV_CUTOFF:
        raise DevBoundError(f"load [{lo}, {hi}) reaches {DEV_CUTOFF}; refused")
    if lo >= hi:
        raise DevBoundError(f"empty or inverted load window [{lo}, {hi})")


def month_chunks(d0: Date, d1: Date):
    """[lo, hi) ET-midnight month chunks covering the inclusive dates d0..d1."""
    lo = d0
    while lo <= d1:
        nxt = Date(lo.year + (lo.month == 12), lo.month % 12 + 1, 1)
        hi = min(nxt, d1 + timedelta(days=1))
        yield et_midnight(lo), et_midnight(hi)
        lo = hi


def is_complete(bars: list[Bar], day: Date) -> bool:
    """True only if every regular minute 09:30..close-1 is present exactly once (early closes from the calendar)
    and every bar has positive prices, low <= open/close <= high and volume >= 0."""
    close = regular_close_minute(day)
    if close is None:
        return False
    if [b.t for b in bars] != list(range(OPEN_MIN, close)):
        return False
    return all(min(b.open, b.high, b.low, b.close) > 0 and b.low <= min(b.open, b.close)
               and max(b.open, b.close) <= b.high and b.volume >= 0 for b in bars)


@dataclass
class SymbolBars:
    symbol: str
    regular: dict[str, list[Bar]] = field(default_factory=dict)  # ET day (ISO) -> REGULAR bars sorted by t
    row_count: int = 0


class DataLoader:
    """Reads 1m bars for the universe over the inclusive dates d0..d1 (d1 <= 2026-01-20)."""

    def __init__(self, db_path: str, start: Date = LOAD_START, end: Date = DEV_LAST_DAY) -> None:
        if start < LOAD_START:
            raise DevBoundError(f"start {start} is before the exchange calendar start {LOAD_START}")
        if end < start:
            raise DevBoundError(f"end {end} is before start {start}")
        assert_dev_window(et_midnight(start), et_midnight(end + timedelta(days=1)))
        self.db_path = str(db_path)
        self.start = start
        self.end = end

    def load_symbol(self, symbol: str) -> SymbolBars:
        assert_dev_window(et_midnight(self.start), et_midnight(self.end + timedelta(days=1)))
        out = SymbolBars(symbol)
        first, last = self.start.isoformat(), self.end.isoformat()
        for lo, hi in month_chunks(self.start, self.end):
            assert_dev_window(lo, hi)
            rows = research_load(self.db_path, symbol, "1m", start=lo, end=hi, purpose="performance",
                                 track=TRACK_INTRADAY, market="US")
            for c in rows:
                out.row_count += 1
                parsed = candle_bar(c)
                if parsed is None:
                    continue
                day, session, bar = parsed
                if not first <= day <= last:
                    raise DevBoundError(f"{symbol} bar dated {day} outside {first}..{last}")
                if session == "REGULAR":
                    out.regular.setdefault(day, []).append(bar)
        for bars in out.regular.values():
            bars.sort(key=lambda b: b.t)
        return out

    def load(self, symbols=UNIVERSE) -> dict[str, SymbolBars]:
        return {s: self.load_symbol(s) for s in symbols}
