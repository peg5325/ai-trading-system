"""사전등록된 지표 계산. 외부 통계 패키지 없이 결정론적으로 계산한다."""
from __future__ import annotations

from collections.abc import Sequence


def ema(values: Sequence[float], period: int) -> tuple[float, ...]:
    if period <= 0 or not values:
        raise ValueError("EMA period와 입력은 양수/비어있지 않아야 한다")
    alpha = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1.0 - alpha) * out[-1])
    return tuple(out)


def wilder_rsi(closes: Sequence[float], period: int) -> tuple[float | None, ...]:
    if period <= 0:
        raise ValueError("RSI period는 양수여야 한다")
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return tuple(out)
    gains = [max(0.0, float(closes[i]) - float(closes[i - 1])) for i in range(1, n)]
    losses = [max(0.0, float(closes[i - 1]) - float(closes[i])) for i in range(1, n)]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value() -> float:
        if avg_loss == 0.0:
            return 100.0 if avg_gain > 0.0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = value()
    for i in range(period + 1, n):
        avg_gain = ((period - 1) * avg_gain + gains[i - 1]) / period
        avg_loss = ((period - 1) * avg_loss + losses[i - 1]) / period
        out[i] = value()
    return tuple(out)


def wilder_atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
               period: int) -> tuple[float | None, ...]:
    if period <= 0 or not (len(highs) == len(lows) == len(closes)):
        raise ValueError("ATR 입력 길이와 period가 유효하지 않다")
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < period:
        return tuple(out)
    tr: list[float] = []
    for i in range(n):
        if i == 0:
            tr.append(float(highs[i]) - float(lows[i]))
        else:
            prev = float(closes[i - 1])
            tr.append(max(
                float(highs[i]) - float(lows[i]),
                abs(float(highs[i]) - prev),
                abs(float(lows[i]) - prev),
            ))
    avg = sum(tr[:period]) / period
    out[period - 1] = avg
    for i in range(period, n):
        avg = ((period - 1) * avg + tr[i]) / period
        out[i] = avg
    return tuple(out)


def exact_median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median 입력이 비었다")
    ordered = sorted(float(v) for v in values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0
