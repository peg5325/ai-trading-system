"""NYSE 거래소 캘린더 — 휴장·조기폐장을 데이터 결측과 구별한다(무의존, 추적 artifact).

계약: `docs/research/DATA_CONTRACT_4STRATEGY_V1.md` §2.4 / AMENDMENT A1 · 이슈 #75 Phase 1B.

배경: 기존 품질 도구(`quality.py:_weekdays`)는 **주말만** 제외하고 공휴일을 결측으로 오탐한다.
조기폐장일(13:00 ET 마감)은 정규 390분 기준으로 커버리지를 과대계상한다(계약 §2.4). 이 모듈은
저장소에 이미 없는 **거래소 휴장/조기폐장 캘린더**를 **의존성 없이** 고정 테이블로 제공한다.

★ 의존성 결정(ADR-0001 참조): scipy/exchange-calendars/pandas-market-calendars 미설치·미선언.
  저장소 관례(주말은 `_weekdays`, 세션은 분 윈도우로 손수 모델링)와 표준환경 재현성을 위해
  **신규 의존성을 추가하지 않고** 3년 공식 구간의 휴장·조기폐장을 **재현 가능한 추적 상수**로 고정한다.

출처: NYSE 공식 휴일/조기폐장 일정(관찰 규칙 적용됨). **artifact 성격**이므로 구간을 넓히거나
  재사용할 때는 NYSE 공지로 재검증한다(연말·특별휴장 National Day of Mourning 포함).

순수 함수만(현재시각/난수/네트워크 미사용). 주문/전략/config 를 import 하지 않는다.
"""
from __future__ import annotations

from datetime import date, timedelta

# 정규장(미국) 창: 09:30–16:00 ET = 390분. 조기폐장: 09:30–13:00 ET = 210분.
REGULAR_MINUTES = 390
EARLY_CLOSE_MINUTES = 210
REGULAR_OPEN_MIN = 9 * 60 + 30      # 09:30 ET
REGULAR_CLOSE_MIN = 16 * 60         # 16:00 ET
EARLY_CLOSE_MIN = 13 * 60           # 13:00 ET

# 이 artifact 가 커버하는 구간(3년 공식 1m 구간 + 여유). 이 밖의 일자는 NYSE 미검증 → 예외.
CALENDAR_START = date(2023, 1, 1)
CALENDAR_END = date(2026, 12, 31)

# NYSE 전휴장(정규장 미개장) — 관찰(observed) 규칙 반영. (연도별 그룹, 재검증 용이)
_FULL_HOLIDAYS: frozenset[str] = frozenset({
    # 2023
    "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29",
    "2023-06-19", "2023-07-04", "2023-09-04", "2023-11-23", "2023-12-25",
    # 2024
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
    "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    # 2025 (2025-01-09 = President Jimmy Carter National Day of Mourning, 특별휴장)
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27",
    "2025-12-25",
    # 2026 (Jul 4 = 토 → 07-03 관찰 휴장)
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
})

# NYSE 조기폐장(13:00 ET) — 독립기념일 전일·Black Friday·크리스마스이브(평일일 때).
_EARLY_CLOSES: frozenset[str] = frozenset({
    "2023-07-03", "2023-11-24",
    "2024-07-03", "2024-11-29", "2024-12-24",
    "2025-07-03", "2025-11-28", "2025-12-24",
    "2026-11-27", "2026-12-24",
})


class CalendarRangeError(ValueError):
    """artifact 커버 구간 밖의 일자 조회(재검증 없이 조용히 거래일로 오판 방지)."""


def _iso(d: date) -> str:
    return d.isoformat()


def _check_range(d: date) -> None:
    if not (CALENDAR_START <= d <= CALENDAR_END):
        raise CalendarRangeError(
            f"{_iso(d)} 는 캘린더 artifact 구간({_iso(CALENDAR_START)}~{_iso(CALENDAR_END)}) 밖 "
            f"— NYSE 공지로 재검증 후 상수를 확장하라(추측 금지).")


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def is_holiday(d: date) -> bool:
    """전휴장(주말 또는 NYSE 휴장일)."""
    _check_range(d)
    return is_weekend(d) or _iso(d) in _FULL_HOLIDAYS


def is_early_close(d: date) -> bool:
    _check_range(d)
    return _iso(d) in _EARLY_CLOSES


def is_trading_day(d: date) -> bool:
    """정규장이 열리는 날(조기폐장 포함, 전휴장 제외)."""
    return not is_holiday(d)


def expected_regular_minutes(d: date) -> int:
    """그 날 기대되는 정규장 분수 — 휴장 0 / 조기폐장 210 / 정상 390.

    조기폐장일 커버리지를 정규 390 으로 과대계상하지 않도록 커버리지 산정에 쓴다(계약 §2.4).
    """
    if is_holiday(d):
        return 0
    return EARLY_CLOSE_MINUTES if is_early_close(d) else REGULAR_MINUTES


def regular_close_minute(d: date) -> int | None:
    """그 날 정규장 마감(분, ET). 휴장이면 None, 조기폐장이면 780(13:00), 정상 960(16:00)."""
    if is_holiday(d):
        return None
    return EARLY_CLOSE_MIN if is_early_close(d) else REGULAR_CLOSE_MIN


def trading_days(start: date, end: date) -> list[date]:
    """[start, end] 폐구간의 거래일(정렬). 구간은 artifact 커버 범위 안이어야 한다."""
    _check_range(start)
    _check_range(end)
    if start > end:
        return []
    out: list[date] = []
    d = start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def early_close_days(start: date, end: date) -> list[date]:
    return [d for d in _daterange(start, end) if is_early_close(d)]


def holidays_in_range(start: date, end: date) -> list[date]:
    """주말을 제외한 **거래소 휴장일**만(예외일 목록 — coverage 미달 설명용)."""
    return [d for d in _daterange(start, end)
            if not is_weekend(d) and _iso(d) in _FULL_HOLIDAYS]


def max_consecutive_nontrading_days(start: date, end: date) -> tuple[int, date | None]:
    """구간 내 최장 연속 비거래일(주말+휴장) 길이와 그 시작일 — gap-skip 예산 근거.

    pagination 이 빈 페이지를 '소진'으로 오판하지 않으려면 gap-skip 예산이 이 값을 커버해야 한다.
    """
    _check_range(start)
    _check_range(end)
    best = 0
    best_start: date | None = None
    run = 0
    run_start: date | None = None
    for d in _daterange(start, end):
        if not is_trading_day(d):
            if run == 0:
                run_start = d
            run += 1
            if run > best:
                best, best_start = run, run_start
        else:
            run = 0
    return best, best_start


def recommended_max_consecutive_empty(gap_skip_minutes: int, start: date, end: date,
                                       *, margin: int = 2) -> int:
    """구간 최장 비거래 span 을 gap-skip 예산(연속 빈 창 허용수)으로 환산.

    빈 하루는 `_shift_before` 로 최대 ~(1440/gap_skip_minutes + 1) 회의 빈 창을 소비한다.
    최장 연속 비거래일 × 하루당 빈창 + margin 을 권장 `max_consecutive_empty` 로 돌려준다.
    """
    if gap_skip_minutes <= 0:
        raise ValueError("gap_skip_minutes 는 양수여야 한다(gap-skip 활성).")
    span, _ = max_consecutive_nontrading_days(start, end)
    per_day = 1440 // gap_skip_minutes + 1
    return span * per_day + margin


def _daterange(start: date, end: date):
    _check_range(start)
    _check_range(end)
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def artifact_summary() -> dict:
    """추적 artifact 결정론 요약(재현 검증용). 성과·수익률 미계산."""
    return {
        "schema": "nyse_calendar_artifact/v1",
        "range": {"start": _iso(CALENDAR_START), "end": _iso(CALENDAR_END)},
        "full_holiday_count": len(_FULL_HOLIDAYS),
        "early_close_count": len(_EARLY_CLOSES),
        "full_holidays": sorted(_FULL_HOLIDAYS),
        "early_closes": sorted(_EARLY_CLOSES),
        "regular_minutes": REGULAR_MINUTES,
        "early_close_minutes": EARLY_CLOSE_MINUTES,
        "source": "NYSE official holiday/early-close schedule (observed rules applied)",
    }
