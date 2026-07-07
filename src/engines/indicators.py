"""순수 지표 계산 함수 (pandas 기반).

- Provider 에 접근하지 않는다. 입력은 pandas Series/DataFrame 뿐.
- backtest 와 live 가 동일하게 사용하는 결정론적 계산.
- warm-up 구간은 명시적으로 NaN (min_periods 로 통일).

Warm-up NaN 정책 (index 0 기준):
  ema(n)          : 첫 n-1 개 NaN, index n-1 부터 유효
  rsi(n)          : 첫 n-1 개 NaN, index n-1 부터 유효 (첫 봉은 변화량 0으로 취급)
  bollinger(n)    : 첫 n-1 개 NaN, index n-1 부터 유효
  stoch_rsi       : rsi warm-up + stoch window + smoothing 만큼 NaN
  vwap            : ET 날짜(세션)별로 리셋, 각 세션 첫 봉부터 유효 (warm-up 없음)
                    누적 거래량 0 인 경우에만 NaN
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(close: pd.Series, period: int) -> pd.Series:
    """지수이동평균. adjust=False 재귀식, 첫 period-1 개는 NaN."""
    out = close.ewm(span=period, adjust=False, min_periods=period).mean()
    return out.rename(f"ema{period}")


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI. 0~100. 첫 봉 변화량은 0 으로 채운다(첫 period-1 개 NaN)."""
    delta = close.diff()
    gain = delta.clip(lower=0).fillna(0.0)
    loss = (-delta).clip(lower=0).fillna(0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100.0 - 100.0 / (1.0 + rs)
    # avg_loss == 0 (하락 없음) -> RSI 100
    out = out.where(avg_loss != 0, 100.0)
    # warm-up 구간(avg_gain NaN)은 NaN 유지
    out = out.where(avg_gain.notna(), np.nan)
    return out.rename(f"rsi{period}")


def vwap(df: pd.DataFrame) -> pd.Series:
    """세션(ET 날짜) 앵커 VWAP. 각 날짜마다 리셋되는 누적 VWAP.

    df 는 high/low/close/volume 과 et_date 컬럼을 가져야 한다.
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    tpv = tp * df["volume"]
    grp = df["et_date"]
    cum_tpv = tpv.groupby(grp).cumsum()
    cum_v = df["volume"].groupby(grp).cumsum()
    out = cum_tpv / cum_v.replace(0, np.nan)
    return out.rename("vwap")


def bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0
              ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """볼린저밴드. (mid=SMA, upper, lower). 모집단 표준편차(ddof=0)."""
    mid = close.rolling(period, min_periods=period).mean()
    sd = close.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + num_std * sd
    lower = mid - num_std * sd
    return mid.rename("bb_mid"), upper.rename("bb_upper"), lower.rename("bb_lower")


def stoch_rsi(close: pd.Series, rsi_period: int = 14, stoch_period: int = 14,
              smooth_k: int = 3, smooth_d: int = 3) -> tuple[pd.Series, pd.Series]:
    """Stochastic RSI. %K, %D 를 0~100 으로 반환.

    RSI 가 stoch window 내에서 평탄(max==min)하면 해당 지점은 NaN.
    """
    r = rsi(close, rsi_period)
    lo = r.rolling(stoch_period, min_periods=stoch_period).min()
    hi = r.rolling(stoch_period, min_periods=stoch_period).max()
    denom = (hi - lo)
    raw = (r - lo) / denom.where(denom != 0, np.nan)
    k = (raw.rolling(smooth_k, min_periods=smooth_k).mean() * 100.0)
    d = k.rolling(smooth_d, min_periods=smooth_d).mean()
    return k.rename("stochrsi_k"), d.rename("stochrsi_d")
