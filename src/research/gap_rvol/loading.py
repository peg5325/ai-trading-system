"""Guarded Gap+RVOL candle loading.

Performance code must not import ``src.history.loader.load_candles`` or use
``purpose="coverage"``.  There is deliberately no official runner here.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from src.bars.resample import resample_1m
from src.history.oos_guard import TRACK_INTRADAY, research_load
from src.models import ET, SESSION_REGULAR, Candle
from src.research.gap_rvol.contract import GapDecisionSpec
from src.research.orb.windows import minute_of_day, regular_close_minute_checked


def et_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=ET)
    return start, start + timedelta(days=1)


def load_symbol_day(db_path: str, symbol: str, day: date) -> tuple[Candle, ...]:
    start, end = et_day_bounds(day)
    return tuple(research_load(
        db_path,
        symbol,
        "1m",
        start=start,
        end=end,
        purpose="performance",
        track=TRACK_INTRADAY,
        market="US",
    ))


def regular_1m(candles: tuple[Candle, ...]) -> tuple[Candle, ...]:
    return tuple(candle for candle in candles if candle.session == SESSION_REGULAR)


def decision_bars(
    candles: tuple[Candle, ...],
    day: date,
    *,
    spec: GapDecisionSpec,
) -> tuple[Candle, ...]:
    same_day = tuple(candle for candle in candles if candle.ts_exchange.date() == day)
    bars = resample_1m(
        same_day, spec.minutes, min_bars=spec.min_bars, closed_only=True
    )
    close_minute = regular_close_minute_checked(day)
    return tuple(sorted(
        (
            bar for bar in bars
            if bar.session == SESSION_REGULAR and minute_of_day(bar) < close_minute
        ),
        key=lambda bar: bar.ts_utc,
    ))
