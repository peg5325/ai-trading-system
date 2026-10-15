"""Canonical 데이터 모델. 설계 13_Data_Contract 를 따른다.

Provider 무관하게 내부는 항상 이 Candle 형식으로 다룬다.
지표 계산(IndicatorEngine)과 백테스트/실거래는 모두 이 형식을 공유한다.

market/exchange 인식: 미국(US, ET)과 국내(KR, KST)의 세션/타임존이 충돌하지 않도록
candle 마다 market 을 두고, 세션 분류를 market 별로 수행한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")

# 시장(거래소)
MARKET_US = "US"
MARKET_KR = "KR"
_EXCHANGE_TZ = {MARKET_US: ET, MARKET_KR: KST}

# 세션 구분
SESSION_PRE = "PRE"
SESSION_REGULAR = "REGULAR"
SESSION_POST = "POST"
SESSION_OVERNIGHT = "OVERNIGHT"   # US 연장/심야(토스 미국주식 24h)
SESSION_CLOSED = "CLOSED"         # KR 장외(정규+시간외 밖)


def exchange_tz(market: str) -> ZoneInfo:
    return _EXCHANGE_TZ.get(market, ET)


def market_from_currency(currency: str) -> str:
    return MARKET_KR if str(currency).upper() == "KRW" else MARKET_US


def _us_early_close(d: date) -> bool:
    """NYSE 조기폐장(13:00 ET 마감)일인가. 캘린더 artifact 구간 밖이면 False(고정 창 유지).

    수집(ingest)이 캘린더 구간 밖 일자(예: 2027-01-01)에서 깨지지 않도록 예외를 삼킨다 —
    구간 밖 조기폐장일은 상수 확장 전까지 REGULAR 09:30–16:00 으로 라벨된다.
    `src.history` 패키지가 이 모듈을 import 하므로 순환을 피하려고 지연 import 한다.
    """
    from src.history import exchange_calendar as cal

    if not (cal.CALENDAR_START <= d <= cal.CALENDAR_END):
        return False
    return cal.is_early_close(d)


def classify_session(ts_exchange: datetime, market: str = MARKET_US) -> str:
    """거래소 로컬 시각을 market 별 세션으로 분류한다."""
    minutes = ts_exchange.hour * 60 + ts_exchange.minute
    if market == MARKET_KR:
        # KRX: PRE 08:00–09:00, REGULAR 09:00–15:30, POST(시간외) 15:30–18:00, 그 외 CLOSED
        if 8 * 60 <= minutes < 9 * 60:
            return SESSION_PRE
        if 9 * 60 <= minutes < 15 * 60 + 30:
            return SESSION_REGULAR
        if 15 * 60 + 30 <= minutes < 18 * 60:
            return SESSION_POST
        return SESSION_CLOSED
    # US: PRE 04:00–09:30, REGULAR 09:30–16:00, POST 16:00–20:00, 그 외 OVERNIGHT.
    # NYSE 조기폐장일은 REGULAR 09:30–13:00, POST 13:00–20:00(사용자 결정 2026-10-15).
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return SESSION_PRE
    if 9 * 60 + 30 <= minutes < 16 * 60:
        if minutes >= 13 * 60 and _us_early_close(ts_exchange.date()):
            return SESSION_POST
        return SESSION_REGULAR
    if 16 * 60 <= minutes < 20 * 60:
        return SESSION_POST
    return SESSION_OVERNIGHT


# 정규장 개장 시각(거래소 로컬, 분): KR 09:00 KST / US 09:30 ET
_REGULAR_OPEN_MIN = {MARKET_US: 9 * 60 + 30, MARKET_KR: 9 * 60}
# 정규장 마감 시각(거래소 로컬, 분): KR 15:30 KST / US 16:00 ET
_REGULAR_CLOSE_MIN = {MARKET_US: 16 * 60, MARKET_KR: 15 * 60 + 30}
# US 애프터마켓 마감(기존 레버리지 EOD 정책 호환): 20:00 ET
_US_EXT_CLOSE_MIN = 20 * 60


def regular_session_window(market: str) -> tuple[int, int]:
    """market 정규장 [개장, 마감) 분(거래소 로컬). US 570~960 / KR 540~930(둘 다 390분).

    expected bar 수 = (마감 - 개장) // timeframe. 78 을 하드코딩하지 않고 여기서 유도한다.
    """
    return (_REGULAR_OPEN_MIN.get(market, _REGULAR_OPEN_MIN[MARKET_US]),
            _REGULAR_CLOSE_MIN.get(market, _REGULAR_CLOSE_MIN[MARKET_US]))


def in_eod_flatten_window(candle: "Candle", minutes_before_close: int) -> bool:
    """이 candle 이 그 market 의 마감 청산 창(마감 N분 전 ~ 마감)에 있는가 — market/tz 별 판단.

    - REGULAR: 각 시장 정규장 마감 기준(KR 15:30 KST / US 16:00 ET) N분 전 창.
    - US POST/OVERNIGHT: 기존 레버리지 EOD 정책 호환(20:00 ET N분 전 창, OVERNIGHT 상시).
    혼합 유니버스에서 symbol 별 candle 로 개별 판단하기 위한 순수 함수(현재시각/난수 미사용).
    """
    market = candle.market
    ts = candle.ts_exchange
    mod = ts.hour * 60 + ts.minute
    # 1) 정규장 마감 창 (KR/US 공통, 각 시장 close time 기준)
    if candle.session == SESSION_REGULAR:
        close_min = _REGULAR_CLOSE_MIN.get(market)
        # 조기폐장일 REGULAR 는 13:00 에 끝난다(classify_session 과 같은 기준) — 그 마감 기준 N분 전 창.
        if market == MARKET_US and _us_early_close(ts.date()):
            close_min = 13 * 60
        if close_min is not None and 0 <= (close_min - mod) <= minutes_before_close:
            return True
    # 2) US 애프터마켓/심야 (기존 레버리지 정책 호환)
    if market == MARKET_US:
        if candle.session == SESSION_OVERNIGHT:
            return True
        if candle.session == SESSION_POST and 0 <= (_US_EXT_CLOSE_MIN - mod) <= minutes_before_close:
            return True
    return False


@dataclass(frozen=True)
class Candle:
    symbol: str
    interval: str           # "1m" | "1d"
    ts_utc: datetime        # timezone-aware UTC (정렬/키 기준)
    ts_exchange: datetime   # timezone-aware 거래소 로컬 (US=ET, KR=KST)
    session: str            # PRE / REGULAR / POST / OVERNIGHT / CLOSED
    open: float
    high: float
    low: float
    close: float
    volume: float
    currency: str
    adjusted: bool
    market: str = MARKET_US

    @classmethod
    def from_moment(cls, symbol: str, interval: str, moment: datetime,
                    o: float, h: float, low: float, c: float, v: float,
                    currency: str, adjusted: bool, market: str | None = None) -> "Candle":
        """임의 tz 의 시각(moment)으로부터 canonical Candle 을 만든다.
        market 미지정 시 통화로 추정(KRW→KR, else US)."""
        if moment.tzinfo is None:
            raise ValueError("moment must be timezone-aware")
        if market is None:
            market = market_from_currency(currency)
        ts_utc = moment.astimezone(timezone.utc)
        ts_ex = moment.astimezone(exchange_tz(market))
        return cls(
            symbol=symbol, interval=interval,
            ts_utc=ts_utc, ts_exchange=ts_ex,
            session=classify_session(ts_ex, market),
            open=o, high=h, low=low, close=c, volume=v,
            currency=currency, adjusted=adjusted, market=market,
        )
