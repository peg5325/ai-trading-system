"""박스권 평균회귀 성과 로더. blind OOS 게이트를 우회하지 않는다."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from src.bars.resample import resample_1m
from src.history.oos_guard import TRACK_INTRADAY, research_load
from src.models import ET, SESSION_REGULAR, Candle, regular_session_window
from src.research.orb.windows import regular_close_minute_checked


def load_symbol_day(db_path: str, symbol: str, day: date) -> tuple[Candle, ...]:
    lo = datetime(day.year, day.month, day.day, tzinfo=ET)
    hi = lo + timedelta(days=1)
    return tuple(research_load(
        db_path, symbol, "1m", start=lo, end=hi,
        purpose="performance", track=TRACK_INTRADAY, market="US",
    ))


def complete_regular_5m(candles: tuple[Candle, ...], day: date, symbol: str
                        ) -> tuple[tuple[Candle, ...], tuple[str, ...]]:
    """정규장 1분 그리드가 완전한 종목-일만 5분봉으로 변환한다."""
    regular = [
        c for c in candles
        if c.symbol == symbol and c.session == SESSION_REGULAR
        and c.ts_exchange.date() == day
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
    expected = set(range(open_minute, close_minute))
    if set(by_minute) != expected:
        defects.add("incomplete_regular_session")
    if defects:
        return (), tuple(sorted(defects))
    close_time = datetime(
        day.year, day.month, day.day, close_minute // 60, close_minute % 60, tzinfo=ET)
    bars = resample_1m(
        list(by_minute.values()), 5, min_bars=5, closed_only=True,
        evaluation_time=close_time,
    )
    expected_bars = (close_minute - open_minute) // 5
    bars = sorted(
        (b for b in bars if b.session == SESSION_REGULAR), key=lambda b: b.ts_utc)
    if len(bars) != expected_bars:
        return (), ("incomplete_5m_resample",)
    return tuple(bars), ()
