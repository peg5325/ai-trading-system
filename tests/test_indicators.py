"""순수 지표 함수 테스트: 기본 계산, warm-up NaN, 세션 VWAP 리셋."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.engines.indicators import bollinger, ema, rsi, stoch_rsi, vwap


# ---- EMA ----
def test_ema_constant_series():
    s = pd.Series([5.0] * 10)
    out = ema(s, 3)
    assert out.iloc[:2].isna().all()        # 첫 period-1 NaN
    assert out.iloc[2:].eq(5.0).all()       # 상수열의 EMA 는 상수


def test_ema_warmup_length():
    s = pd.Series([float(i) for i in range(1, 31)])
    out = ema(s, 7)
    assert out.iloc[:6].isna().all()
    assert out.iloc[6:].notna().all()


# ---- RSI ----
def test_rsi_all_up_is_100():
    s = pd.Series([float(i) for i in range(1, 31)])
    out = rsi(s, 14)
    assert out.iloc[:13].isna().all()
    assert out.iloc[13:].notna().all()
    assert out.iloc[13:].round(6).eq(100.0).all()


def test_rsi_all_down_is_zero():
    s = pd.Series([float(i) for i in range(30, 0, -1)])
    out = rsi(s, 14)
    assert out.iloc[13:].round(6).eq(0.0).all()


def test_rsi_bounded_0_100():
    s = pd.Series([100 + (i % 7) - (i % 5) * 0.5 for i in range(60)])
    out = rsi(s, 14).dropna()
    assert ((out >= 0) & (out <= 100)).all()


# ---- Bollinger ----
def test_bollinger_constant_zero_width():
    s = pd.Series([50.0] * 25)
    mid, up, lo = bollinger(s, 20, 2.0)
    assert mid.iloc[:19].isna().all()
    assert mid.iloc[19:].eq(50.0).all()
    assert up.iloc[19:].eq(50.0).all()      # 표준편차 0 -> 밴드 폭 0
    assert lo.iloc[19:].eq(50.0).all()


def test_bollinger_band_ordering():
    s = pd.Series([100 + (i % 9) for i in range(40)], dtype=float)
    mid, up, lo = bollinger(s, 20, 2.0)
    valid = mid.notna()
    assert (up[valid] >= mid[valid]).all()
    assert (mid[valid] >= lo[valid]).all()


# ---- Stochastic RSI ----
def test_stoch_rsi_range_and_warmup():
    s = pd.Series([100 + np.sin(i / 3.0) * 5 for i in range(80)])
    k, d = stoch_rsi(s, 14, 14, 3, 3)
    assert k.iloc[:14].isna().all()         # 최소한 RSI warm-up 만큼은 NaN
    kv, dv = k.dropna(), d.dropna()
    assert len(kv) > 0 and len(dv) > 0
    assert ((kv >= 0) & (kv <= 100)).all()
    assert ((dv >= 0) & (dv <= 100)).all()


# ---- VWAP (세션 리셋) ----
def test_vwap_resets_per_et_date():
    d1 = dt.date(2026, 7, 6)
    d2 = dt.date(2026, 7, 7)
    df = pd.DataFrame({
        "high": [10.0, 20.0, 30.0],
        "low": [10.0, 20.0, 30.0],
        "close": [10.0, 20.0, 30.0],
        "volume": [100.0, 100.0, 50.0],
        "et_date": [d1, d1, d2],
    })
    out = vwap(df)
    # d1: 10, then (10*100+20*100)/200 = 15 ; d2 리셋 -> 30
    assert out.tolist() == pytest.approx([10.0, 15.0, 30.0])
