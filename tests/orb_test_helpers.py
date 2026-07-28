from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.models import Candle
from src.research.orb.features import OpeningRange, SymbolDayFeatures

ET = ZoneInfo("America/New_York")


def bar(
    day: date,
    minute: int,
    *,
    symbol: str = "AAPL",
    o: float = 101.0,
    h: float = 102.0,
    low: float = 100.0,
    c: float = 101.5,
    volume: float = 100.0,
    interval: str = "5m",
) -> Candle:
    moment = datetime(
        day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=ET)
    return Candle.from_moment(
        symbol, interval, moment, o, h, low, c, volume, "USD", True)


def features(
    day: date,
    *,
    symbol: str = "AAPL",
    or_high: float = 100.0,
    or_low: float = 95.0,
    gap: float = 0.01,
    rvol: float = 2.0,
    excluded: tuple[str, ...] = (),
) -> SymbolDayFeatures:
    return SymbolDayFeatures(
        symbol=symbol,
        day=day,
        opening_range=OpeningRange(or_high, or_low),
        gap=gap,
        rvol=rvol,
        excluded_reasons=excluded,
    )
