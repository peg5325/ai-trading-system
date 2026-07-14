"""세션 bar 커버리지 — 정규장 캘린더 + timeframe 기준 expected/observed/missing 계산.

`src/history/quality.py` 의 정규장 window 기반 커버리지 로직을 bar 단위로 옮긴 순수 계산.
매매/주문과 무관한 관측이며 결정론적(now()/난수 미사용, 관측 시각을 입력받음).

- expected bars = (정규장 마감 - 개장) // timeframe. 78 을 하드코딩하지 않는다.
  조기폐장(session_close_min)·timeframe 변경이 자동 반영된다.
- missing bar index/interval 을 원본으로 저장하고 open/mid/close zone 은 파생한다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from src.models import regular_session_window


@dataclass
class SessionCoverage:
    market: str
    bar_minutes: int
    regular_open_min: int
    regular_close_min: int
    expected_bars: int
    observed_bars: int
    missing_bars: int
    coverage_pct: float
    reached_end: bool                          # 마지막 expected bar 를 관측했는가(FULL 판정용)
    missing_bar_indexes: list[int] = field(default_factory=list)       # 원본
    missing_intervals: list[list[int]] = field(default_factory=list)   # [start,end] 포함
    missing_zones: dict = field(default_factory=lambda: {"open": False, "mid": False, "close": False})

    @property
    def complete(self) -> bool:
        return self.expected_bars > 0 and self.missing_bars == 0

    def to_dict(self) -> dict:
        # manifest 에는 raw dict 로 영속(SessionCoverage 로 역직렬화하지 않음).
        return asdict(self)


def compute_coverage(market: str, bar_minutes: int, observed_exchange_times,
                     *, session_close_min: int | None = None) -> SessionCoverage:
    """관측된 5분봉(ts_exchange) 목록 → SessionCoverage.

    observed_exchange_times: 이 세션에서 관측/처리한 bar 의 거래소 로컬 datetime 목록.
    session_close_min: 조기폐장 시 마감 분(거래소 로컬). 미지정 시 정규 마감.
    """
    open_min, close_min = regular_session_window(market)
    if session_close_min is not None:
        close_min = session_close_min
    tf = int(bar_minutes) if bar_minutes else 0
    expected = max(0, (close_min - open_min) // tf) if tf > 0 else 0

    observed_idx: set[int] = set()
    for t in observed_exchange_times or []:
        mod = t.hour * 60 + t.minute
        if open_min <= mod < close_min:
            idx = (mod - open_min) // tf
            if 0 <= idx < expected:
                observed_idx.add(idx)

    observed = len(observed_idx)
    missing_idx = [i for i in range(expected) if i not in observed_idx]
    missing = len(missing_idx)
    coverage_pct = round(observed / expected * 100.0, 2) if expected else 0.0
    reached_end = expected > 0 and (expected - 1) in observed_idx
    return SessionCoverage(
        market=market, bar_minutes=tf, regular_open_min=open_min, regular_close_min=close_min,
        expected_bars=expected, observed_bars=observed, missing_bars=missing,
        coverage_pct=coverage_pct, reached_end=reached_end,
        missing_bar_indexes=missing_idx,
        missing_intervals=_contiguous_intervals(missing_idx),
        missing_zones=_zones(missing_idx, expected))


def _contiguous_intervals(indexes: list[int]) -> list[list[int]]:
    """정렬된 인덱스 → 연속 구간 [start, end](포함) 목록."""
    out: list[list[int]] = []
    for i in indexes:
        if out and i == out[-1][1] + 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return out


def _zones(missing_idx: list[int], expected: int) -> dict:
    """결측 인덱스를 3등분(개장/중간/마감) zone 으로 파생. 편향 방향 판별용(#006R)."""
    z = {"open": False, "mid": False, "close": False}
    if expected <= 0:
        return z
    third = expected / 3.0
    for i in missing_idx:
        if i < third:
            z["open"] = True
        elif i < 2 * third:
            z["mid"] = True
        else:
            z["close"] = True
    return z
