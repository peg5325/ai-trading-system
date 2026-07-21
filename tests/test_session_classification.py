"""세션 분류 · DST 특성화 테스트 (계약 §2.2~§2.3, 이슈 #68 Phase 1A).

기존 src/models.py:classify_session / Candle.from_moment 의 계약을 고정한다(프로덕션 변경 없음).
Toss 일봉 timestamp 의 DST 거동(00:00 ET 고정)은 PROBE_REPORT 실측을 회귀로 박제한다.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.models import (Candle, classify_session, MARKET_US, MARKET_KR,
                        SESSION_PRE, SESSION_REGULAR, SESSION_POST,
                        SESSION_OVERNIGHT, SESSION_CLOSED)

ET = ZoneInfo("America/New_York")


def _us(hh: int, mm: int) -> str:
    return classify_session(datetime(2026, 7, 21, hh, mm, tzinfo=ET), MARKET_US)


def test_us_session_boundaries():
    assert _us(3, 59) == SESSION_OVERNIGHT
    assert _us(4, 0) == SESSION_PRE          # PRE 시작 포함
    assert _us(9, 29) == SESSION_PRE
    assert _us(9, 30) == SESSION_REGULAR     # REGULAR 시작 포함
    assert _us(15, 59) == SESSION_REGULAR
    assert _us(16, 0) == SESSION_POST        # POST 시작 포함
    assert _us(19, 59) == SESSION_POST
    assert _us(20, 0) == SESSION_OVERNIGHT   # POST 끝 배타
    assert _us(0, 0) == SESSION_OVERNIGHT    # 자정 = 일봉 라벨 위치(무의미)


def test_kr_session_boundaries():
    KST = ZoneInfo("Asia/Seoul")
    def _kr(hh, mm):
        return classify_session(datetime(2026, 7, 21, hh, mm, tzinfo=KST), MARKET_KR)
    assert _kr(7, 59) == SESSION_CLOSED
    assert _kr(8, 0) == SESSION_PRE
    assert _kr(9, 0) == SESSION_REGULAR
    assert _kr(15, 29) == SESSION_REGULAR
    assert _kr(15, 30) == SESSION_POST
    assert _kr(17, 59) == SESSION_POST
    assert _kr(18, 0) == SESSION_CLOSED


def test_dst_regular_open_summer_and_winter():
    # 09:35 ET 는 여름(EDT)·겨울(EST) 모두 REGULAR 여야 한다(오프셋은 zoneinfo 가 처리).
    summer = Candle.from_moment("SPY", "1m", datetime(2026, 7, 21, 9, 35, tzinfo=ET),
                                1, 1, 1, 1, 1, currency="USD", adjusted=True, market="US")
    winter = Candle.from_moment("SPY", "1m", datetime(2026, 1, 15, 9, 35, tzinfo=ET),
                                1, 1, 1, 1, 1, currency="USD", adjusted=True, market="US")
    assert summer.session == SESSION_REGULAR and winter.session == SESSION_REGULAR
    # UTC 오프셋이 계절에 따라 -04:00(EDT) / -05:00(EST) 로 다름을 확인.
    assert summer.ts_utc.hour == 13 and winter.ts_utc.hour == 14


def test_daily_bar_ktz_timestamp_maps_to_midnight_et_year_round():
    # PROBE 실측: Toss 일봉은 여름 13:00+09:00, 겨울 14:00+09:00 → 둘 다 00:00 ET.
    # 거래일 date 가 연중 올바르게(밀림 없이) 복원되는지 회귀로 고정.
    summer = Candle.from_moment("SPY", "1d",
                                datetime.fromisoformat("2026-07-21T13:00:00.000+09:00"),
                                1, 1, 1, 1, 1, currency="USD", adjusted=True, market="US")
    winter = Candle.from_moment("SPY", "1d",
                                datetime.fromisoformat("2026-02-27T14:00:00.000+09:00"),
                                1, 1, 1, 1, 1, currency="USD", adjusted=True, market="US")
    assert summer.ts_exchange.hour == 0 and summer.ts_exchange.date().isoformat() == "2026-07-21"
    assert winter.ts_exchange.hour == 0 and winter.ts_exchange.date().isoformat() == "2026-02-27"
    # 자정 ET 일봉은 OVERNIGHT 로 라벨된다(일봉 session 은 무의미 — 계약 §2.3).
    assert summer.session == SESSION_OVERNIGHT and winter.session == SESSION_OVERNIGHT
