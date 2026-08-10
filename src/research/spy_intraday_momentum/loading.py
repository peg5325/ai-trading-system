"""blind OOS 게이트를 통과하는 SPY 일봉·1분봉 로더와 완전 세션 검증."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Sequence

from src.history.oos_guard import TRACK_INTRADAY, research_load
from src.models import ET, SESSION_REGULAR, Candle, regular_session_window
from src.research.orb.windows import regular_close_minute_checked

STRATEGY_ID = "spy-first-to-last-half-hour-momentum-v1"


def load_symbol_day(db_path: str, symbol: str, day: date) -> tuple[Candle, ...]:
    start = datetime(day.year, day.month, day.day, tzinfo=ET)
    return tuple(research_load(
        db_path,
        symbol,
        "1m",
        start=start,
        end=start + timedelta(days=1),
        purpose="performance",
        track=TRACK_INTRADAY,
        strategy_id=STRATEGY_ID,
        market="US",
    ))


def load_daily_closes(
    db_path: str, symbol: str, start_day: date, end_day: date
) -> dict[date, float]:
    start = datetime(start_day.year, start_day.month, start_day.day, tzinfo=ET)
    end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=ET) + timedelta(days=1)
    candles = research_load(
        db_path,
        symbol,
        "1d",
        start=start,
        end=end,
        purpose="performance",
        track=TRACK_INTRADAY,
        strategy_id=STRATEGY_ID,
        market="US",
    )
    return {candle.ts_exchange.date(): float(candle.close) for candle in candles}


def _bar_key(bar: Candle) -> tuple[object, ...]:
    return (
        bar.symbol,
        bar.ts_utc,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.session,
        bar.interval,
    )


def complete_regular_1m(
    candles: Sequence[Candle], day: date, symbol: str
) -> tuple[tuple[Candle, ...], tuple[str, ...]]:
    """정규장 기대 minute grid가 정확할 때만 정렬 1분봉을 반환한다.

    `classify_session`은 조기폐장 이후 16:00까지 REGULAR로 분류할 수 있으므로 캘린더의
    실제 마감 minute 미만만 취한다.
    """
    open_minute, _ = regular_session_window("US")
    close_minute = regular_close_minute_checked(day)
    by_minute: dict[int, Candle] = {}
    defects: set[str] = set()
    for candle in candles:
        if (
            candle.symbol != symbol
            or candle.ts_exchange.date() != day
            or candle.session != SESSION_REGULAR
        ):
            continue
        minute = candle.ts_exchange.hour * 60 + candle.ts_exchange.minute
        if minute < open_minute or minute >= close_minute:
            continue
        if candle.ts_exchange.second or candle.ts_exchange.microsecond:
            defects.add("off_grid_timestamp")
        previous = by_minute.get(minute)
        if previous is not None and _bar_key(previous) != _bar_key(candle):
            defects.add("duplicate_timestamp_value_conflict")
        else:
            by_minute.setdefault(minute, candle)
    expected = set(range(open_minute, close_minute))
    if set(by_minute) != expected:
        defects.add("incomplete_regular_session")
    if defects:
        return (), tuple(sorted(defects))
    return tuple(by_minute[minute] for minute in range(open_minute, close_minute)), ()
