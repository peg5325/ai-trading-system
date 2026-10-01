"""Daily bars from REGULAR 1m bars and the t-1 indicators of §3.1.

All indicators are computed over the sequence of *valid* daily bars (§2.4 threshold met,
not an early close). Valid days inside the excluded trade block still feed this sequence.
An indicator value is ``None`` when undefined; undefined means no trade.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from src.research.gross_edge_v2.data import DayBars, is_valid_symbol_day


@dataclass(frozen=True)
class DailyBar:
    day: date
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Indicators:
    """Indicator values as of the close of one valid day (used as t-1 for day t)."""

    day: date
    close: float
    range: float
    nr7: bool | None
    nr4: bool | None
    inside: bool | None
    stretch10: float | None
    sma20: float | None
    sma200: float | None
    atr10: float | None
    atr14: float | None
    rsi2: float | None
    rsi3_roc1: float | None


def valid_daily_bars(bars_by_day: Mapping[date, DayBars]) -> list[DailyBar]:
    out = []
    for day in sorted(bars_by_day):
        bars = bars_by_day[day]
        if bars.count and is_valid_symbol_day(day, bars.count):
            out.append(DailyBar(day, *bars.daily_ohlc()))
    return out


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values)


def wilder(values: Sequence[float], n: int) -> list[float | None]:
    """Wilder smoothing: seed = mean of the first n values, then (prev*(n-1)+x)/n."""
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    current = _mean(values[:n])
    out[n - 1] = current
    for index in range(n, len(values)):
        current = (current * (n - 1) + values[index]) / n
        out[index] = current
    return out


def rsi(series: Sequence[float], n: int) -> list[float | None]:
    """Wilder RSI on ``series``; undefined before 3n values of the series (§3.1).

    avg_loss == 0 -> 100, and both averages 0 -> 50.
    Reading of "3n": count values of the input series (RSI2 on close: 6 valid days), which
    is the only reading consistent with the explicit RSI3(ROC1) rule of 3*3 + 1 valid days.
    """
    out: list[float | None] = [None] * len(series)
    changes = [series[i] - series[i - 1] for i in range(1, len(series))]
    gains = wilder([max(change, 0.0) for change in changes], n)
    losses = wilder([max(-change, 0.0) for change in changes], n)
    for index in range(1, len(series)):
        gain, loss = gains[index - 1], losses[index - 1]
        if gain is None or loss is None or index + 1 < 3 * n:
            continue
        if loss == 0:
            out[index] = 50.0 if gain == 0 else 100.0
        else:
            out[index] = 100.0 - 100.0 / (1.0 + gain / loss)
    return out


def atr(bars: Sequence[DailyBar], n: int) -> list[float | None]:
    """Wilder ATR; first valid bar's TR is H-L; undefined before 3n valid bars."""
    true_ranges = []
    for index, bar in enumerate(bars):
        if index == 0:
            true_ranges.append(bar.high - bar.low)
        else:
            prev_close = bars[index - 1].close
            true_ranges.append(max(
                bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close)
            ))
    smoothed = wilder(true_ranges, n)
    return [value if index + 1 >= 3 * n else None for index, value in enumerate(smoothed)]


def compute_indicators(bars: Sequence[DailyBar]) -> dict[date, Indicators]:
    closes = [bar.close for bar in bars]
    ranges = [bar.high - bar.low for bar in bars]
    stretch = [min(bar.high - bar.open, bar.open - bar.low) for bar in bars]
    atr10, atr14 = atr(bars, 10), atr(bars, 14)
    rsi2 = rsi(closes, 2)
    # RSI3 of ROC1: ROC index j is close index j+1, so 3*3 ROC values = 3*3+1 valid days.
    roc1 = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    rsi3_roc = [None] + rsi(roc1, 3)

    def window_min_is_last(k: int, index: int) -> bool | None:
        if index + 1 < k:
            return None
        return ranges[index] <= min(ranges[index - k + 1:index + 1])

    def sma(k: int, index: int) -> float | None:
        return _mean(closes[index - k + 1:index + 1]) if index + 1 >= k else None

    out = {}
    for index, bar in enumerate(bars):
        inside = None
        if index >= 1:
            previous = bars[index - 1]
            inside = bar.high < previous.high and bar.low > previous.low
        out[bar.day] = Indicators(
            day=bar.day,
            close=bar.close,
            range=ranges[index],
            nr7=window_min_is_last(7, index),
            nr4=window_min_is_last(4, index),
            inside=inside,
            stretch10=_mean(stretch[index - 9:index + 1]) if index + 1 >= 10 else None,
            sma20=sma(20, index),
            sma200=sma(200, index),
            atr10=atr10[index],
            atr14=atr14[index],
            rsi2=rsi2[index],
            rsi3_roc1=rsi3_roc[index],
        )
    return out
