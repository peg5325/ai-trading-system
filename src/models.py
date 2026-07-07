"""Canonical 데이터 모델. 설계 13_Data_Contract 를 따른다.

Provider 무관하게 내부는 항상 이 Candle 형식으로 다룬다.
지표 계산(IndicatorEngine)과 백테스트/실거래는 모두 이 형식을 공유한다.

market/exchange 인식: 미국(US, ET)과 국내(KR, KST)의 세션/타임존이 충돌하지 않도록
candle 마다 market 을 두고, 세션 분류를 market 별로 수행한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
    # US: PRE 04:00–09:30, REGULAR 09:30–16:00, POST 16:00–20:00, 그 외 OVERNIGHT
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return SESSION_PRE
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return SESSION_REGULAR
    if 16 * 60 <= minutes < 20 * 60:
        return SESSION_POST
    return SESSION_OVERNIGHT


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
