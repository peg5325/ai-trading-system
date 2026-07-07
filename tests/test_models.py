"""Canonical 모델 로직 테스트: 세션 분류 + 시각 변환."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.models import (
    SESSION_OVERNIGHT,
    SESSION_POST,
    SESSION_PRE,
    SESSION_REGULAR,
    Candle,
    classify_session,
)

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")


def _et(h: int, m: int) -> datetime:
    # 여름(EDT) 기준 임의의 평일
    return datetime(2026, 7, 6, h, m, tzinfo=ET)


@pytest.mark.parametrize("h,m,expected", [
    (3, 59, SESSION_OVERNIGHT),
    (4, 0, SESSION_PRE),
    (9, 29, SESSION_PRE),
    (9, 30, SESSION_REGULAR),
    (15, 59, SESSION_REGULAR),
    (16, 0, SESSION_POST),
    (19, 59, SESSION_POST),
    (20, 0, SESSION_OVERNIGHT),
    (0, 58, SESSION_OVERNIGHT),
])
def test_classify_session(h, m, expected):
    assert classify_session(_et(h, m)) == expected


def test_from_moment_converts_kst_to_utc_and_et():
    # Toss 가 주는 KST(+09:00) 시각을 넣으면 UTC/ET 로 정확히 변환되어야 한다.
    moment = datetime.fromisoformat("2026-07-06T13:47:00.000+09:00")  # 00:47 ET
    c = Candle.from_moment(
        symbol="SOXL", interval="1m", moment=moment,
        o=194.68, h=194.85, low=194.64, c=194.79, v=941,
        currency="USD", adjusted=True,
    )
    # 13:47 KST == 04:47 UTC == 00:47 EDT
    assert c.ts_utc.hour == 4 and c.ts_utc.minute == 47
    assert c.ts_exchange.hour == 0 and c.ts_exchange.minute == 47
    assert c.session == SESSION_OVERNIGHT
    assert c.currency == "USD"
    assert c.close == pytest.approx(194.79)


def test_from_moment_regular_session():
    moment = datetime.fromisoformat("2026-07-06T22:30:00+09:00")  # 09:30 ET
    c = Candle.from_moment(
        symbol="SOXL", interval="1m", moment=moment,
        o=1, h=1, low=1, c=1, v=1, currency="USD", adjusted=True,
    )
    assert c.session == SESSION_REGULAR


def test_from_moment_requires_tzaware():
    with pytest.raises(ValueError):
        Candle.from_moment(
            symbol="X", interval="1m", moment=datetime(2026, 7, 6, 9, 30),
            o=1, h=1, low=1, c=1, v=1, currency="USD", adjusted=True,
        )
