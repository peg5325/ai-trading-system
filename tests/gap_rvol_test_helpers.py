from __future__ import annotations

from datetime import date, datetime

from src.models import ET, MARKET_US, Candle
from src.research.gap_rvol.features import GapRvolDayFeatures


def bar(
    day: date,
    minute: int,
    *,
    symbol: str = "AAPL",
    interval: str = "5m",
    o: float = 100.0,
    h: float = 101.0,
    low: float = 99.0,
    c: float = 100.0,
    v: float = 100.0,
) -> Candle:
    moment = datetime(
        day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=ET
    )
    return Candle.from_moment(
        symbol=symbol,
        interval=interval,
        moment=moment,
        o=o,
        h=h,
        low=low,
        c=c,
        v=v,
        currency="USD",
        adjusted=True,
        market=MARKET_US,
    )


def minute_bars(
    day: date,
    *,
    symbol: str = "AAPL",
    start: int = 570,
    count: int = 390,
    price: float = 100.0,
    volume: float = 100.0,
) -> tuple[Candle, ...]:
    return tuple(
        bar(
            day,
            start + offset,
            symbol=symbol,
            interval="1m",
            o=price,
            h=price + 1,
            low=price - 1,
            c=price,
            v=volume,
        )
        for offset in range(count)
    )


def features(
    day: date,
    *,
    symbol: str = "AAPL",
    gap: float = 0.01,
    rvol: float = 2.0,
    floor: float = 99.0,
    vwap: float = 100.0,
    minutes: tuple[int, ...] = (570, 575, 580, 585, 590, 595, 600, 605),
) -> GapRvolDayFeatures:
    return GapRvolDayFeatures(
        symbol=symbol,
        day=day,
        previous_close=98.0,
        today_open=100.0,
        gap=gap,
        rvol=rvol,
        retention_floor=floor,
        vwap_by_minute={minute: vwap for minute in minutes},
        excluded_reasons=(),
        filter_reasons=(),
    )
