"""Gap, 15-minute RVOL, retention, and causal VWAP features."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.models import Candle, regular_session_window
from src.research.gap_rvol.contract import SpecParams
from src.research.orb.features import RvolWindow, window_sum_for_lookback
from src.research.orb.windows import minute_of_day, regular_close_minute_checked

DATA_EXCLUSION_REASONS = frozenset({
    "no_previous_regular_close",
    "incomplete_rvol_window",
    "insufficient_rvol_lookback",
    "zero_cumulative_volume",
    "incomplete_decision_bar",
})
STRATEGY_FILTER_REASONS = frozenset({
    "gap_below_threshold",
    "rvol_below_threshold",
    "retention_broken",
    "pullback_not_armed",
    "continuation_not_triggered",
})


@dataclass(frozen=True)
class GapRvolDayFeatures:
    symbol: str
    day: date
    previous_close: float | None
    today_open: float | None
    gap: float | None
    rvol: float | None
    retention_floor: float | None
    vwap_by_minute: dict[int, float]
    excluded_reasons: tuple[str, ...]
    filter_reasons: tuple[str, ...]


def _one_symbol_day(candles: tuple[Candle, ...], *, symbol: str, day: date) -> None:
    if any(candle.symbol != symbol or candle.ts_exchange.date() != day for candle in candles):
        raise ValueError("candles must contain exactly one requested symbol-day")
    keys: dict[int, tuple[object, ...]] = {}
    for candle in candles:
        minute = minute_of_day(candle)
        value = (
            candle.open, candle.high, candle.low, candle.close, candle.volume,
            candle.session, candle.interval,
        )
        if minute in keys and keys[minute] != value:
            raise ValueError(f"conflicting duplicate minute: {minute}")
        keys[minute] = value


def causal_vwap_by_bar(
    regular_minutes: tuple[Candle, ...],
    decision_bars: tuple[Candle, ...],
    *,
    symbol: str,
    day: date,
    bar_minutes: int,
) -> tuple[dict[int, float], tuple[str, ...]]:
    _one_symbol_day(regular_minutes, symbol=symbol, day=day)
    _one_symbol_day(decision_bars, symbol=symbol, day=day)
    minutes = {minute_of_day(candle): candle for candle in regular_minutes}
    output: dict[int, float] = {}
    cumulative_pv = 0.0
    cumulative_volume = 0.0
    open_minute, _ = regular_session_window("US")
    next_minute = open_minute
    for bar in sorted(decision_bars, key=minute_of_day):
        start = minute_of_day(bar)
        if start != next_minute:
            return {}, ("incomplete_decision_bar",)
        for minute in range(start, start + bar_minutes):
            candle = minutes.get(minute)
            if candle is None:
                return {}, ("incomplete_decision_bar",)
            typical = (candle.high + candle.low + candle.close) / 3.0
            cumulative_pv += typical * candle.volume
            cumulative_volume += candle.volume
        if cumulative_volume == 0:
            return {}, ("zero_cumulative_volume",)
        output[start] = cumulative_pv / cumulative_volume
        next_minute += bar_minutes
    return output, ()


def compute_day_features(
    *,
    symbol: str,
    day: date,
    previous_day: date,
    previous_regular_1m: tuple[Candle, ...],
    today_regular_1m: tuple[Candle, ...],
    decision_bars: tuple[Candle, ...],
    rvol_window: RvolWindow,
    spec: SpecParams,
    rvol_minutes: int,
    decision_minutes: int,
) -> GapRvolDayFeatures:
    _one_symbol_day(today_regular_1m, symbol=symbol, day=day)
    _one_symbol_day(previous_regular_1m, symbol=symbol, day=previous_day)
    close_minute = regular_close_minute_checked(previous_day)
    previous = [
        candle for candle in previous_regular_1m
        if minute_of_day(candle) < close_minute
    ]
    open_minute, _ = regular_session_window("US")
    today_open_bars = [
        candle for candle in today_regular_1m
        if minute_of_day(candle) == open_minute
    ]
    reasons: set[str] = set()
    filters: set[str] = set()
    previous_close = max(previous, key=minute_of_day).close if previous else None
    today_open = today_open_bars[0].open if today_open_bars else None
    if previous_close is None or today_open is None:
        reasons.add("no_previous_regular_close")
    gap = (
        (today_open - previous_close) / previous_close
        if previous_close not in (None, 0) and today_open is not None
        else None
    )
    if gap is not None and gap < spec.gap_threshold:
        filters.add("gap_below_threshold")

    window = [
        candle for candle in today_regular_1m
        if open_minute <= minute_of_day(candle) < open_minute + rvol_minutes
    ]
    if len({minute_of_day(candle) for candle in window}) < rvol_minutes:
        reasons.add("incomplete_rvol_window")
        today_sum = None
    else:
        today_sum = sum(candle.volume for candle in window)
    rvol = None if today_sum is None else rvol_window.rvol(day, today_sum)
    if today_sum == 0:
        reasons.add("zero_cumulative_volume")
    elif today_sum is not None and rvol is None:
        reasons.add("insufficient_rvol_lookback")
    if rvol is not None and rvol < spec.rvol_threshold:
        filters.add("rvol_below_threshold")
    history_sum = window_sum_for_lookback(
        today_regular_1m, day=day, minutes=rvol_minutes
    )
    rvol_window.push(day, history_sum)

    vwap, vwap_reasons = causal_vwap_by_bar(
        today_regular_1m,
        decision_bars,
        symbol=symbol,
        day=day,
        bar_minutes=decision_minutes,
    )
    reasons.update(vwap_reasons)
    retention_floor = (
        previous_close + spec.retention_fraction * (today_open - previous_close)
        if previous_close is not None and today_open is not None
        else None
    )
    return GapRvolDayFeatures(
        symbol=symbol,
        day=day,
        previous_close=previous_close,
        today_open=today_open,
        gap=gap,
        rvol=rvol,
        retention_floor=retention_floor,
        vwap_by_minute=vwap,
        excluded_reasons=tuple(sorted(reasons)),
        filter_reasons=tuple(sorted(filters)),
    )
