"""ORB 후보·돌파 신호 — 사전등록 §5·§6.

피처의 데이터 완전성(`is_eligible`)과 전략 임계(gap/RVOL)는 서로 다른
조건이다. 이 모듈은 둘을 반드시 AND로 묶고, 완료된 결정봉 close의
strict breakout만 다음 결정봉 대기로 보낸다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from src.models import Candle
from src.research.orb.contract import SpecParams
from src.research.orb.features import SymbolDayFeatures
from src.research.orb.windows import minute_of_day


@dataclass(frozen=True)
class OrbSignal:
    symbol: str
    day: date
    spec_name: str
    signal_bar_ts_utc: datetime
    signal_minute: int
    fill_minute: int
    rvol: float
    gap: float
    or_high: float
    or_low: float
    target_r_multiple: float


def is_strategy_candidate(features: SymbolDayFeatures, spec: SpecParams) -> bool:
    """§5 데이터 완전성과 두 전략 임계를 모두 만족하는가."""
    return (
        features.is_eligible
        and features.opening_range is not None
        and features.gap is not None
        and features.rvol is not None
        and abs(features.gap) >= spec.gap_threshold_abs
        and features.rvol >= spec.rvol_threshold
    )


def detect_breakout(
    features: SymbolDayFeatures,
    bar: Candle,
    spec: SpecParams,
    *,
    decision_bar_minutes: int,
) -> OrbSignal | None:
    """완료 결정봉 close가 OR high를 strict 초과하면 다음 봉 신호를 만든다.

    OR를 구성하는 마지막 봉에서는 신호를 만들지 않는다. 예를 들어 15분
    OR의 첫 신호 가능 봉은 09:45 시작 5분봉이다.
    """
    if bar.symbol != features.symbol or bar.ts_exchange.date() != features.day:
        raise ValueError("신호 봉과 피처의 symbol/day가 다르다")
    if not is_strategy_candidate(features, spec):
        return None
    minute = minute_of_day(bar)
    first_signal_minute = 9 * 60 + 30 + spec.opening_range_minutes
    if minute < first_signal_minute:
        return None
    assert features.opening_range is not None
    if not bar.close > features.opening_range.high:
        return None
    return OrbSignal(
        symbol=features.symbol,
        day=features.day,
        spec_name=spec.name,
        signal_bar_ts_utc=bar.ts_utc,
        signal_minute=minute,
        fill_minute=minute + decision_bar_minutes,
        rvol=float(features.rvol),
        gap=float(features.gap),
        or_high=features.opening_range.high,
        or_low=features.opening_range.low,
        target_r_multiple=spec.target_r_multiple,
    )


def rank_signals(signals: list[OrbSignal] | tuple[OrbSignal, ...]) -> tuple[OrbSignal, ...]:
    """동시 신호의 유일한 순위: RVOL↓, |gap|↓, symbol ASCII↑."""
    return tuple(sorted(signals, key=lambda s: (-s.rvol, -abs(s.gap), s.symbol)))
