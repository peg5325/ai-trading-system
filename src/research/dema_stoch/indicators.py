"""Pure, dependency-free DEMA and stochastic indicator implementations."""
from __future__ import annotations

from collections.abc import Sequence


def ema(values: Sequence[float], period: int) -> tuple[float, ...]:
    if period < 1:
        raise ValueError("EMA period must be positive")
    if not values:
        return ()
    alpha = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1.0 - alpha) * out[-1])
    return tuple(out)


def dema(values: Sequence[float], period: int) -> tuple[float, ...]:
    first = ema(values, period)
    second = ema(first, period)
    return tuple(2.0 * one - two for one, two in zip(first, second))


def stochastic_d(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    k_period: int,
    d_period: int,
) -> tuple[float | None, ...]:
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("stochastic inputs must have equal lengths")
    if k_period < 1 or d_period < 1:
        raise ValueError("stochastic periods must be positive")
    raw: list[float | None] = [None] * len(closes)
    for index in range(k_period - 1, len(closes)):
        hi = max(float(value) for value in highs[index - k_period + 1 : index + 1])
        lo = min(float(value) for value in lows[index - k_period + 1 : index + 1])
        if hi > lo:
            raw[index] = 100.0 * (float(closes[index]) - lo) / (hi - lo)
    out: list[float | None] = [None] * len(closes)
    for index in range(d_period - 1, len(closes)):
        window = raw[index - d_period + 1 : index + 1]
        if all(value is not None for value in window):
            out[index] = sum(float(value) for value in window) / d_period
    return tuple(out)

