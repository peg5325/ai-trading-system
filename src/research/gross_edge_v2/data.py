"""Windows, guarded 1m loading and per-day minute arrays for screen v2 (§2).

Prices come only from ``research_load(purpose="performance", track=intraday)``. The load
window is [2023-01-03, 2026-01-21 00:00 ET), which ends before blind OOS, and every loaded
bar is re-checked against ``DATA_END`` (fail closed). H2 is never requested.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np

from src.history import exchange_calendar as cal
from src.history.oos_guard import TRACK_INTRADAY, research_load
from src.models import ET, SESSION_REGULAR, Candle

OPEN_MINUTE = 570  # 09:30 ET
SLOTS = 390  # 09:30..15:59
EXIT_MINUTE = 955  # 15:55 ET
LOAD_START = date(2023, 1, 3)
DATA_END = date(2026, 1, 20)
EVAL_START = date(2023, 5, 18)
EXCLUDED_BLOCK = (date(2024, 7, 1), date(2024, 8, 30))
VALID_SHARE = 0.95


class DataDefectError(RuntimeError):
    """Ambiguous canonical input that must not be silently resolved."""


class DataWindowError(RuntimeError):
    """A bar outside the permitted development window reached the screen."""


def hm(text: str) -> int:
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


@dataclass(frozen=True)
class Windows:
    trade_days: tuple[date, ...]  # 2023-05-18..2026-01-20 NYSE days (670)
    excluded_block_days: tuple[date, ...]  # 44
    evaluated_days: tuple[date, ...]  # minus block and early closes (619)


def build_windows(contract: Mapping[str, object]) -> Windows:
    windows = contract["windows"]  # type: ignore[index]
    start = date.fromisoformat(windows["trade_eval_start"])
    end = date.fromisoformat(windows["trade_eval_end"])
    block_lo = date.fromisoformat(windows["excluded_block_start"])
    block_hi = date.fromisoformat(windows["excluded_block_end"])
    if (start, end, (block_lo, block_hi)) != (EVAL_START, DATA_END, EXCLUDED_BLOCK):
        raise DataWindowError("contract windows differ from the code constants")
    days = tuple(cal.trading_days(start, end))
    block = tuple(day for day in days if block_lo <= day <= block_hi)
    evaluated = tuple(
        day for day in days
        if not (block_lo <= day <= block_hi) and not cal.is_early_close(day)
    )
    return Windows(days, block, evaluated)


def window_defects(contract: Mapping[str, object], windows: Windows) -> list[str]:
    """Compare derived day counts with the frozen counts (670 / 44 / 619 / 12,380)."""
    spec = contract["windows"]  # type: ignore[index]
    quality = contract["data_quality"]  # type: ignore[index]
    defects = []
    if len(windows.trade_days) != int(spec["trade_eval_trading_days"]):
        defects.append(f"trade_eval_trading_days={len(windows.trade_days)}")
    if len(windows.excluded_block_days) != int(spec["excluded_block_trading_days"]):
        defects.append(f"excluded_block_trading_days={len(windows.excluded_block_days)}")
    if len(windows.evaluated_days) != int(spec["evaluated_trading_days"]):
        defects.append(f"evaluated_trading_days={len(windows.evaluated_days)}")
    universe = len(contract["universe"])  # type: ignore[arg-type]
    if universe * len(windows.evaluated_days) != int(quality["evaluated_symbol_days"]):
        defects.append("evaluated_symbol_days mismatch")
    return defects


class DayBars:
    """One symbol's REGULAR 1m bars of one day as minute-indexed arrays (NaN = missing)."""

    def __init__(self, symbol: str, day: date, rows: Mapping[int, tuple[float, ...]]):
        self.symbol = symbol
        self.day = day
        arrays = np.full((5, SLOTS), np.nan)
        for minute, values in rows.items():
            arrays[:, minute - OPEN_MINUTE] = values
        self.open, self.high, self.low, self.close, self.volume = arrays
        self.present = ~np.isnan(self.open)
        self.count = int(self.present.sum())
        typical = (self.high + self.low + self.close) / 3.0 * self.volume
        self._cum_pv = np.cumsum(np.where(self.present, typical, 0.0))
        self._cum_v = np.cumsum(np.where(self.present, self.volume, 0.0))

    def has(self, minute: int) -> bool:
        index = minute - OPEN_MINUTE
        return 0 <= index < SLOTS and bool(self.present[index])

    def bar(self, minute: int) -> tuple[float, float, float, float] | None:
        """(open, high, low, close) of an exact minute, or None if that bar is missing."""
        if not self.has(minute):
            return None
        index = minute - OPEN_MINUTE
        return (
            float(self.open[index]), float(self.high[index]),
            float(self.low[index]), float(self.close[index]),
        )

    def vwap_through(self, minute: int) -> float | None:
        """Session VWAP through ``minute`` inclusive (missing bars contribute nothing).

        Zero cumulative volume -> undefined (None): no signal and no target (§3.1).
        """
        index = minute - OPEN_MINUTE
        if index < 0:
            return None
        index = min(index, SLOTS - 1)
        volume = float(self._cum_v[index])
        if volume <= 0:
            return None
        return float(self._cum_pv[index]) / volume

    def daily_ohlc(self) -> tuple[float, float, float, float]:
        """First open, max high, min low, last close of the REGULAR bars (§3.1)."""
        idx = np.flatnonzero(self.present)
        return (
            float(self.open[idx[0]]), float(np.nanmax(self.high)),
            float(np.nanmin(self.low)), float(self.close[idx[-1]]),
        )


def is_valid_symbol_day(day: date, count: int) -> bool:
    """§2.4/§3.1: >= 95% of expected REGULAR minutes (371/390) and not an early close."""
    if cal.is_early_close(day):
        return False
    return count >= VALID_SHARE * cal.expected_regular_minutes(day)


def group_day_bars(candles: Iterable[Candle], symbol: str) -> dict[date, DayBars]:
    """Group one symbol's candles into DayBars; input order does not matter.

    Only REGULAR bars inside [09:30, 16:00) of a trading day in [LOAD_START, DATA_END] are
    kept. A bar dated after DATA_END raises DataWindowError; a duplicate minute raises
    DataDefectError (fail closed instead of picking one).
    """
    rows: dict[date, dict[int, tuple[float, ...]]] = defaultdict(dict)
    for candle in candles:
        day = candle.ts_exchange.date()
        if day > DATA_END:
            raise DataWindowError(f"bar after {DATA_END} loaded: {symbol} {day}")
        if candle.symbol != symbol:
            raise DataDefectError(f"foreign symbol {candle.symbol} in {symbol} load")
        if candle.session != SESSION_REGULAR or day < LOAD_START:
            continue
        minute = candle.ts_exchange.hour * 60 + candle.ts_exchange.minute
        if not OPEN_MINUTE <= minute < OPEN_MINUTE + SLOTS:
            continue
        if candle.ts_exchange.second or candle.ts_exchange.microsecond:
            raise DataDefectError(f"off-grid timestamp {symbol} {candle.ts_exchange}")
        if minute in rows[day]:
            raise DataDefectError(f"duplicate regular minute {symbol} {day} {minute}")
        rows[day][minute] = (
            float(candle.open), float(candle.high), float(candle.low),
            float(candle.close), float(candle.volume),
        )
    return {day: DayBars(symbol, day, minutes) for day, minutes in sorted(rows.items())}


def assert_no_data_after_end(candles: Sequence[Candle]) -> None:
    if candles and max(candle.ts_exchange.date() for candle in candles) > DATA_END:
        raise DataWindowError(f"loaded data extends past {DATA_END}")


def load_symbol_bars(snapshot_path: str, symbol: str) -> dict[date, DayBars]:
    start = datetime(LOAD_START.year, LOAD_START.month, LOAD_START.day, tzinfo=ET)
    end_day = DATA_END + timedelta(days=1)
    end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=ET)
    candles = research_load(
        snapshot_path,
        symbol,
        "1m",
        start=start,
        end=end,
        purpose="performance",
        track=TRACK_INTRADAY,
        market="US",
    )
    assert_no_data_after_end(candles)
    return group_day_bars(candles, symbol)
