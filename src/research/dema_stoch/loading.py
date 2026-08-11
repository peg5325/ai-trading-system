"""OOS-guarded, complete-session 5-minute loading."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from src.bars.resample import resample_1m
from src.history.oos_guard import TRACK_INTRADAY, research_load
from src.models import ET, SESSION_REGULAR, Candle, regular_session_window
from src.research.orb.windows import minute_of_day, regular_close_minute_checked


def et_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=ET)
    return start, start + timedelta(days=1)


def load_symbol_day(db_path: str, symbol: str, day: date) -> tuple[Candle, ...]:
    start, end = et_day_bounds(day)
    return tuple(
        research_load(
            db_path,
            symbol,
            "1m",
            start=start,
            end=end,
            purpose="performance",
            track=TRACK_INTRADAY,
            market="US",
        )
    )


def complete_regular_5m(
    candles: tuple[Candle, ...], day: date, symbol: str
) -> tuple[tuple[Candle, ...], tuple[str, ...]]:
    if any(candle.symbol != symbol for candle in candles):
        raise ValueError("mixed symbol input")
    if any(candle.ts_exchange.date() != day for candle in candles):
        raise ValueError("mixed trading-day input")
    open_minute, _ = regular_session_window("US")
    close_minute = regular_close_minute_checked(day)
    expected_1m = tuple(range(open_minute, close_minute))
    regular = tuple(
        candle
        for candle in candles
        if candle.session == SESSION_REGULAR
        and open_minute <= minute_of_day(candle) < close_minute
    )
    observed = tuple(minute_of_day(candle) for candle in regular)
    if len(observed) != len(set(observed)):
        return (), ("duplicate_regular_minute",)
    if tuple(sorted(observed)) != expected_1m:
        return (), ("incomplete_regular_1m_grid",)
    bars = tuple(
        bar
        for bar in resample_1m(
            list(candles), 5, min_bars=5, closed_only=True
        )
        if bar.session == SESSION_REGULAR
        and open_minute <= minute_of_day(bar) < close_minute
    )
    expected_5m = tuple(range(open_minute, close_minute, 5))
    if tuple(minute_of_day(bar) for bar in bars) != expected_5m:
        return (), ("incomplete_regular_5m_grid",)
    return bars, ()

