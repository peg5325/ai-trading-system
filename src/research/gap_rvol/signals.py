"""Gap-retention pullback and strict continuation state machine."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from src.models import Candle
from src.research.gap_rvol.contract import SpecParams
from src.research.gap_rvol.features import GapRvolDayFeatures
from src.research.orb.windows import minute_of_day


@dataclass(frozen=True)
class GapRvolSignal:
    symbol: str
    day: date
    spec_name: str
    signal_minute: int
    fill_minute: int
    pullback_low: float
    rvol: float
    gap: float
    target_r_multiple: float


@dataclass(frozen=True)
class SignalScan:
    signal: GapRvolSignal | None
    filter_reason: str | None


def rank_signals(signals: Sequence[GapRvolSignal]) -> tuple[GapRvolSignal, ...]:
    return tuple(sorted(signals, key=lambda item: (-item.rvol, -item.gap, item.symbol)))


def detect_signal(
    *,
    features: GapRvolDayFeatures,
    bars: Sequence[Candle],
    spec: SpecParams,
    decision_minutes: int,
    observation_start_minute: int,
    latest_signal_minute: int,
    latest_fill_minute: int,
) -> SignalScan:
    if features.excluded_reasons:
        return SignalScan(None, None)
    if features.gap is None or features.rvol is None or features.retention_floor is None:
        return SignalScan(None, None)
    if features.gap < spec.gap_threshold:
        return SignalScan(None, "gap_below_threshold")
    if features.rvol < spec.rvol_threshold:
        return SignalScan(None, "rvol_below_threshold")

    ordered = sorted(bars, key=minute_of_day)
    armed = False
    pullback_low: float | None = None
    for previous, current in pairwise(ordered):
        minute = minute_of_day(current)
        if minute < observation_start_minute:
            continue
        if current.close < features.retention_floor:
            return SignalScan(None, "retention_broken")
        was_armed = armed
        if not armed and current.close < previous.close:
            armed = True
            pullback_low = current.low
        elif armed:
            pullback_low = min(float(pullback_low), current.low)
        # A bar that first arms the setup cannot also trigger it.
        if (
            was_armed
            and minute <= latest_signal_minute
            and current.close > previous.high
            and current.close > features.vwap_by_minute[minute]
            and current.close > current.open
            and current.close >= features.retention_floor
        ):
            fill_minute = minute + decision_minutes
            if fill_minute > latest_fill_minute:
                return SignalScan(None, "continuation_not_triggered")
            return SignalScan(
                GapRvolSignal(
                    symbol=features.symbol,
                    day=features.day,
                    spec_name=spec.name,
                    signal_minute=minute,
                    fill_minute=fill_minute,
                    pullback_low=float(pullback_low),
                    rvol=features.rvol,
                    gap=features.gap,
                    target_r_multiple=spec.target_r_multiple,
                ),
                None,
            )
    return SignalScan(None, "pullback_not_armed" if not armed else "continuation_not_triggered")
