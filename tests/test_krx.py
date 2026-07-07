"""KRX 종목 feasibility: market/exchange 인식, KR 세션 분류, US/KR 타임존 비충돌,
CandleStore market 복원, Screener 가 KR 정규장을 연장시간으로 오분류하지 않음."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from src.engines.indicator_engine import IndicatorEngine
from src.models import (
    KST,
    MARKET_KR,
    MARKET_US,
    Candle,
    classify_session,
    market_from_currency,
)
from src.storage.candle_store import CandleStore
from src.universe.screener import SymbolData, UniverseScreener


# ---- market_from_currency ----
def test_market_from_currency():
    assert market_from_currency("KRW") == MARKET_KR
    assert market_from_currency("USD") == MARKET_US


# ---- KR 세션 경계 ----
def test_kr_session_boundaries():
    def sess(h, m):
        return classify_session(datetime(2026, 7, 7, h, m, tzinfo=KST), MARKET_KR)
    assert sess(8, 30) == "PRE"
    assert sess(9, 0) == "REGULAR"
    assert sess(15, 29) == "REGULAR"
    assert sess(15, 45) == "POST"
    assert sess(20, 0) == "CLOSED"
    assert sess(3, 0) == "CLOSED"


# ---- KR candle: 통화→market, KST 거래소시각, 정규장 세션 ----
def test_kr_candle_market_session_tz():
    m = datetime.fromisoformat("2026-07-07T10:00:00+09:00")   # 10:00 KST
    c = Candle.from_moment("005930", "1m", m, 70000, 70100, 69900, 70050, 1000, "KRW", True)
    assert c.market == MARKET_KR
    assert c.session == "REGULAR"
    assert c.ts_exchange.hour == 10
    assert c.ts_exchange.utcoffset().total_seconds() == 9 * 3600


# ---- US 종목은 영향 없음 (기존 동작 유지) ----
def test_us_candle_unaffected():
    m = datetime.fromisoformat("2026-07-06T10:00:00-04:00")   # 10:00 ET
    c = Candle.from_moment("SOXL", "1m", m, 190, 191, 189, 190, 1000, "USD", True)
    assert c.market == MARKET_US and c.session == "REGULAR"


# ---- CandleStore 가 통화로 market 복원 ----
def test_candle_store_preserves_market(tmp_path):
    store = CandleStore(tmp_path / "m.sqlite")
    m = datetime.fromisoformat("2026-07-07T10:00:00+09:00")
    store.upsert_many([Candle.from_moment("005930", "1m", m, 70000, 70000, 70000, 70000, 1000, "KRW", True)])
    got = store.query("005930", "1m")[0]
    assert got.market == MARKET_KR and got.session == "REGULAR"
    assert got.ts_exchange.utcoffset().total_seconds() == 9 * 3600
    store.close()


# ---- 핵심: KR 정규장을 연장시간으로 오분류하지 않음 (US 로 보면 OVERNIGHT) ----
def _frame(n=30, close=70000.0, ts="2026-07-07T01:00:00Z"):  # 01:00 UTC = 10:00 KST
    idx = pd.DatetimeIndex(
        [pd.Timestamp(ts) - pd.Timedelta(minutes=5 * (n - 1 - i)) for i in range(n)], name="ts_utc")
    bb_mid, half = close, 0.05 * close / 2
    return pd.DataFrame({
        "open": close, "high": close, "low": close, "close": close, "volume": 100000.0,
        "ema7": close * 0.999, "ema20": close * 0.995, "rsi14": 58.0, "vwap": close * 0.999,
        "bb_mid": bb_mid, "bb_upper": bb_mid + half, "bb_lower": bb_mid - half,
        "stochrsi_k": 70.0, "stochrsi_d": 60.0}, index=idx)


def test_kr_regular_not_flagged_extended_hours():
    scr = UniverseScreener()
    kr = scr.screen([SymbolData("005930", _frame(), 1, market=MARKET_KR)])
    assert kr and "EXTENDED_HOURS_SPREAD" not in kr[0].risk_flags   # KR 정규장 → 연장시간 아님
    # 동일 프레임을 US 로 보면 01:00 UTC 는 미국 심야 → 연장시간 플래그 (타임존/세션 분리 확인)
    us = scr.screen_all([SymbolData("005930", _frame(), 1, market=MARKET_US)])
    assert "EXTENDED_HOURS_SPREAD" in us[0].risk_flags


# ---- 체인: KR 캔들 → IndicatorEngine → Screener 정상 동작 ----
def test_kr_chain_indicator_and_screener():
    base = datetime(2026, 7, 7, 9, 0, tzinfo=KST)             # 09:00 KST 부터 정규장
    candles, prev = [], 70000.0
    for i in range(40):
        mmt = base + timedelta(minutes=5 * i)
        close = 70000.0 + i * 20
        candles.append(Candle.from_moment("005930", "5m", mmt, prev,
                                          max(prev, close) + 10, min(prev, close) - 10,
                                          close, 100000, "KRW", True))
        prev = close
    ind = IndicatorEngine().compute(candles)
    assert len(ind) == 40 and ind["vwap"].notna().any()       # VWAP 계산됨(크래시 없음)
    cands = UniverseScreener().screen([SymbolData("005930", ind, 1, market=MARKET_KR)])
    for c in cands:
        assert "EXTENDED_HOURS_SPREAD" not in c.risk_flags     # 정규장 → 연장시간 아님
