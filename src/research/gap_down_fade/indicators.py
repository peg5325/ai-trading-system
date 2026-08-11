"""사전등록된 결정론적 지표."""
from __future__ import annotations

from collections.abc import Sequence


def exact_median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median 입력이 비었다")
    ordered = sorted(float(value) for value in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def cumulative_vwap(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
                    volumes: Sequence[float]) -> tuple[float | None, ...]:
    if not (len(highs) == len(lows) == len(closes) == len(volumes)):
        raise ValueError("VWAP 입력 길이가 다르다")
    numerator = denominator = 0.0
    out: list[float | None] = []
    for high, low, close, volume in zip(highs, lows, closes, volumes):
        numerator += ((float(high) + float(low) + float(close)) / 3.0) * float(volume)
        denominator += float(volume)
        out.append(numerator / denominator if denominator > 0 else None)
    return tuple(out)


def wilder_atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
               period: int) -> tuple[float | None, ...]:
    if period <= 0 or not (len(highs) == len(lows) == len(closes)):
        raise ValueError("ATR 입력이 유효하지 않다")
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return tuple(out)
    ranges: list[float] = []
    for index, (high, low) in enumerate(zip(highs, lows)):
        previous = float(closes[index - 1]) if index else None
        ranges.append(float(high) - float(low) if previous is None else max(
            float(high) - float(low), abs(float(high) - previous), abs(float(low) - previous)))
    average = sum(ranges[:period]) / period
    out[period - 1] = average
    for index in range(period, len(ranges)):
        average = ((period - 1) * average + ranges[index]) / period
        out[index] = average
    return tuple(out)


def wilder_rsi(closes: Sequence[float], period: int) -> tuple[float | None, ...]:
    if period <= 0:
        raise ValueError("RSI period는 양수여야 한다")
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return tuple(out)
    changes = [float(closes[i]) - float(closes[i - 1]) for i in range(1, len(closes))]
    gain = sum(max(change, 0.0) for change in changes[:period]) / period
    loss = sum(max(-change, 0.0) for change in changes[:period]) / period

    def value() -> float:
        if loss == 0 and gain == 0:
            return 50.0
        if loss == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    out[period] = value()
    for index in range(period + 1, len(closes)):
        change = changes[index - 1]
        gain = ((period - 1) * gain + max(change, 0.0)) / period
        loss = ((period - 1) * loss + max(-change, 0.0)) / period
        out[index] = value()
    return tuple(out)
