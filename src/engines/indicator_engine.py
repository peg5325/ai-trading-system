"""IndicatorEngine — Canonical 캔들에 지표를 계산하는 순수 모듈.

- 입력: list[Candle] 또는 pandas DataFrame (storage 에서 조회한 것).
- Provider 에 직접 접근하지 않는다.
- backtest 와 live 가 동일하게 사용.
- 결과는 ts_utc 인덱스로 candle 과 정렬된 DataFrame.
- 진행 중인(미완결) 캔들은 사용하지 않는다: resample 시 마지막 부분 버킷을 버린다.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.models import ET, Candle, classify_session
from src.engines.indicators import bollinger, ema, rsi, stoch_rsi, vwap

# 결과 DataFrame 컬럼 순서
OUTPUT_COLUMNS = [
    "open", "high", "low", "close", "volume",
    "ema7", "ema20", "rsi14", "vwap",
    "bb_mid", "bb_upper", "bb_lower",
    "stochrsi_k", "stochrsi_d",
]
_FRAME_COLUMNS = ["open", "high", "low", "close", "volume",
                  "currency", "session", "ts_exchange", "et_date"]


def _empty_frame(columns: list[str]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([], tz="UTC", name="ts_utc")
    return pd.DataFrame({c: pd.Series(dtype="object") for c in columns}, index=idx)


def candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    """Canonical 캔들 리스트를 ts_utc 인덱스 DataFrame 으로 변환 (오름차순)."""
    if not candles:
        return _empty_frame(_FRAME_COLUMNS)
    rows = []
    for c in candles:
        rows.append({
            "ts_utc": c.ts_utc,
            "open": float(c.open), "high": float(c.high),
            "low": float(c.low), "close": float(c.close),
            "volume": float(c.volume),
            "currency": c.currency, "session": c.session,
            "ts_exchange": c.ts_exchange, "et_date": c.ts_exchange.date(),
        })
    df = pd.DataFrame(rows).set_index("ts_utc").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.index = pd.DatetimeIndex(df.index, name="ts_utc")
    return df


def _add_meta(out: pd.DataFrame, currency: str) -> pd.DataFrame:
    out = out.copy()
    if len(out):
        et = out.index.tz_convert(ET)
        out["currency"] = currency
        out["session"] = [classify_session(t) for t in et]
        out["ts_exchange"] = list(et)
        out["et_date"] = [t.date() for t in et]
    return out


def resample(df: pd.DataFrame, rule: str = "5min", drop_partial: bool = True) -> pd.DataFrame:
    """OHLCV 리샘플링 (예: 1분 -> 5분).

    label/closed = left: 버킷 [t, t+rule) 를 시작시각 t 로 라벨링.
    drop_partial=True: 마지막(진행 중일 수 있는) 버킷을 제거한다.
    거래가 없는 빈 버킷은 제거한다.
    """
    if df.empty:
        return _empty_frame(_FRAME_COLUMNS)
    agg = {"open": "first", "high": "max", "low": "min",
           "close": "last", "volume": "sum"}
    out = df.resample(rule, label="left", closed="left").agg(agg)
    out = out.dropna(subset=["open"])
    currency = df["currency"].iloc[0] if "currency" in df.columns and len(df) else ""
    out = _add_meta(out, currency)
    if drop_partial and len(out):
        out = out.iloc[:-1]
    return out


def _ensure_et_date(df: pd.DataFrame) -> pd.DataFrame:
    if "et_date" in df.columns:
        return df
    df = df.copy()
    if "ts_exchange" in df.columns:
        et = pd.DatetimeIndex(pd.to_datetime(df["ts_exchange"]))
    else:
        et = df.index.tz_convert(ET)
    df["et_date"] = [t.date() for t in et]
    return df


@dataclass(frozen=True)
class IndicatorParams:
    ema_fast: int = 7
    ema_slow: int = 20
    rsi_period: int = 14
    bb_period: int = 20
    bb_std: float = 2.0
    stoch_rsi_period: int = 14
    stoch_period: int = 14
    stoch_k: int = 3
    stoch_d: int = 3


class IndicatorEngine:
    def __init__(self, params: IndicatorParams | None = None):
        self.p = params or IndicatorParams()

    def compute(self, data: list[Candle] | pd.DataFrame) -> pd.DataFrame:
        df = data if isinstance(data, pd.DataFrame) else candles_to_frame(data)
        if df.empty:
            return _empty_frame(OUTPUT_COLUMNS)
        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]

        close = df["close"].astype(float)
        res = pd.DataFrame(index=df.index)
        res.index.name = "ts_utc"
        for col in ["open", "high", "low", "close", "volume"]:
            res[col] = df[col].astype(float)

        res["ema7"] = ema(close, self.p.ema_fast)
        res["ema20"] = ema(close, self.p.ema_slow)
        res["rsi14"] = rsi(close, self.p.rsi_period)
        res["vwap"] = vwap(_ensure_et_date(df))
        mid, upper, lower = bollinger(close, self.p.bb_period, self.p.bb_std)
        res["bb_mid"], res["bb_upper"], res["bb_lower"] = mid, upper, lower
        k, d = stoch_rsi(close, self.p.stoch_rsi_period, self.p.stoch_period,
                         self.p.stoch_k, self.p.stoch_d)
        res["stochrsi_k"], res["stochrsi_d"] = k, d

        return res[OUTPUT_COLUMNS]
