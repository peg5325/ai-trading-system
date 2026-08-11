"""blind OOS 게이트를 통과하는 성과 로더와 완전 5분봉 파생."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from src.bars.resample import resample_1m
from src.history.oos_guard import TRACK_INTRADAY, research_load
from src.models import ET, SESSION_REGULAR, Candle, regular_session_window
from src.research.orb.windows import regular_close_minute_checked


def load_symbol_day(db_path: str, symbol: str, day: date) -> tuple[Candle, ...]:
    start = datetime(day.year, day.month, day.day, tzinfo=ET)
    return tuple(research_load(
        db_path, symbol, "1m", start=start, end=start + timedelta(days=1),
        purpose="performance", track=TRACK_INTRADAY, market="US",
    ))


def complete_regular_5m(candles: tuple[Candle, ...], day: date, symbol: str
                        ) -> tuple[tuple[Candle, ...], tuple[str, ...]]:
    regular = [
        candle for candle in candles
        if candle.symbol == symbol and candle.session == SESSION_REGULAR
        and candle.ts_exchange.date() == day
    ]
    by_minute: dict[int, Candle] = {}
    defects: set[str] = set()
    for candle in regular:
        if candle.ts_exchange.second or candle.ts_exchange.microsecond:
            defects.add("off_grid_timestamp")
        minute = candle.ts_exchange.hour * 60 + candle.ts_exchange.minute
        previous = by_minute.get(minute)
        if previous is not None:
            old = (previous.open, previous.high, previous.low, previous.close, previous.volume)
            new = (candle.open, candle.high, candle.low, candle.close, candle.volume)
            if old != new:
                defects.add("duplicate_timestamp_value_conflict")
        else:
            by_minute[minute] = candle
    open_minute, _ = regular_session_window("US")
    close_minute = regular_close_minute_checked(day)
    if set(by_minute) != set(range(open_minute, close_minute)):
        defects.add("incomplete_regular_session")
    if defects:
        return (), tuple(sorted(defects))
    close_time = datetime(
        day.year, day.month, day.day, close_minute // 60, close_minute % 60, tzinfo=ET)
    bars = tuple(sorted(
        (bar for bar in resample_1m(
            list(by_minute.values()), 5, min_bars=5, closed_only=True,
            evaluation_time=close_time,
        ) if bar.session == SESSION_REGULAR),
        key=lambda bar: bar.ts_utc,
    ))
    if len(bars) != (close_minute - open_minute) // 5:
        return (), ("incomplete_5m_resample",)
    return bars, ()
