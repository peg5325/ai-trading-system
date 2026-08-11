"""사전등록된 결정론적 지표 계산."""
from __future__ import annotations

from collections.abc import Sequence


def ema(values: Sequence[float], period: int) -> tuple[float, ...]:
    if period <= 0 or not values:
        raise ValueError("EMA 입력이 유효하지 않다")
    alpha = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1.0 - alpha) * out[-1])
    return tuple(out)


def wilder_atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
               period: int) -> tuple[float | None, ...]:
    if period <= 0 or not (len(highs) == len(lows) == len(closes)):
        raise ValueError("ATR 입력이 유효하지 않다")
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return tuple(out)
    true_ranges = []
    for i, (high, low) in enumerate(zip(highs, lows)):
        if i == 0:
            true_ranges.append(float(high) - float(low))
        else:
            previous = float(closes[i - 1])
            true_ranges.append(max(
                float(high) - float(low), abs(float(high) - previous),
                abs(float(low) - previous),
            ))
    average = sum(true_ranges[:period]) / period
    out[period - 1] = average
    for i in range(period, len(closes)):
        average = ((period - 1) * average + true_ranges[i]) / period
        out[i] = average
    return tuple(out)


def cumulative_vwap(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
                    volumes: Sequence[float]) -> tuple[float | None, ...]:
    if not (len(highs) == len(lows) == len(closes) == len(volumes)):
        raise ValueError("VWAP 입력 길이가 다르다")
    numerator = 0.0
    denominator = 0.0
    out: list[float | None] = []
    for high, low, close, volume in zip(highs, lows, closes, volumes):
        typical = (float(high) + float(low) + float(close)) / 3.0
        numerator += typical * float(volume)
        denominator += float(volume)
        out.append(numerator / denominator if denominator > 0 else None)
    return tuple(out)


def exact_median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median 입력이 비었다")
    ordered = sorted(float(v) for v in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0
