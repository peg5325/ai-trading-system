"""IndicatorEngine — Canonical 캔들에 지표를 계산하는 순수 모듈 + **리샘플 adapter**.

- 입력: list[Candle] 또는 pandas DataFrame (storage 에서 조회한 것).
- Provider 에 직접 접근하지 않는다.
- backtest 와 live 가 동일하게 사용.
- 결과는 ts_utc 인덱스로 candle 과 정렬된 DataFrame.
- 진행 중인(미완결) 캔들은 사용하지 않는다: resample 시 닫히지 않은 버킷을 버린다.

**리샘플 단일 소스화(이슈 #76)**: `resample`/`frame_to_candles` 는 더 이상 자체 집계를 하지
않는다. 유일한 집계 권위는 `src/bars/resample.py`(pure leaf·market-aware·순서독립·세션 버킷키)이고
이 모듈은 **DataFrame ↔ Candle thin adapter** 다. 따라서 연구(`candidate_stream`)·
라이브(`paper_loop`)·`replay` 가 동일 1분봉에서 **동일 직렬화 결과**를 낸다
(계약 §2.5 / AMENDMENT A2, `docs/research/RESAMPLER_PARITY_CONTRACT.md`).

DataFrame 경로 계약(라이브/replay 가 쓰는 형태):
- 인덱스는 ts_utc 하나뿐이라 **단일 symbol 프레임**만 다룬다(복수 symbol 은 Candle API 사용).
- 입력 프레임의 행은 **1분봉**으로 해석한다(`interval` 컬럼이 있으면 검증, 없으면 1m 가정).
  1m 이 아니면 fail-closed — 파생봉을 다시 리샘플하는 오용을 막는다.
- 세션/거래소시각/market 은 **원본에서 상속**하고 재계산하지 않는다(US 하드코딩 제거).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from src.models import (MARKET_US, Candle, classify_session, exchange_tz,
                        market_from_currency)
from src.engines.indicators import bollinger, ema, rsi, stoch_rsi, vwap
from src.bars.resample import bucket_minutes_from_rule, resample_1m

# 결과 DataFrame 컬럼 순서
OUTPUT_COLUMNS = [
    "open", "high", "low", "close", "volume",
    "ema7", "ema20", "rsi14", "vwap",
    "bb_mid", "bb_upper", "bb_lower",
    "stochrsi_k", "stochrsi_d",
]
# OHLCV 순서(결정론 dedup tie-break 키 — 연구 core 의 최대 튜플 규칙과 동일)
_VALUE_COLUMNS = ["open", "high", "low", "close", "volume"]
# `et_date` 는 **거래소 로컬 날짜**다(US=ET, KR=KST). 이름은 하위호환으로 유지한다.
_FRAME_COLUMNS = ["open", "high", "low", "close", "volume",
                  "currency", "session", "ts_exchange", "et_date",
                  "market", "adjusted", "interval"]
# 단일 symbol 프레임 어댑터 내부에서만 쓰는 자리표시자(집계 그룹핑 키로만 사용).
_ADAPTER_SYMBOL = "_FRAME_"


def _empty_frame(columns: list[str]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([], tz="UTC", name="ts_utc")
    return pd.DataFrame({c: pd.Series(dtype="object") for c in columns}, index=idx)


def _dedup_deterministic(df: pd.DataFrame) -> pd.DataFrame:
    """ts 중복 제거 — **입력 순서 무관**((o,h,l,c,v) 최대 튜플, 연구 core 와 동일 규칙).

    기존 `keep="last"` 는 입력 순서에 의존했다(계약 §2.5 (b) 차이). 값이 같은 정상 중복에서는
    결과가 동일하고, 값이 충돌하는 중복에서만 규칙이 확정된다.
    """
    if not df.index.has_duplicates:
        return df
    if all(c in df.columns for c in _VALUE_COLUMNS):
        ordered = df.sort_values(_VALUE_COLUMNS, kind="mergesort")
        ordered = ordered.sort_index(kind="mergesort")     # stable → 값 순서 보존
        return ordered[~ordered.index.duplicated(keep="last")]
    return df[~df.index.duplicated(keep="last")]


def candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    """Canonical 캔들 리스트를 ts_utc 인덱스 DataFrame 으로 변환 (오름차순).

    session/ts_exchange/market/adjusted/interval 을 **원본 candle 에서 그대로 싣는다** —
    adapter 가 뒤에서 재계산하지 않도록(§2.5 (c) US 하드코딩 제거).
    """
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
            "market": c.market, "adjusted": c.adjusted, "interval": c.interval,
        })
    df = pd.DataFrame(rows).set_index("ts_utc").sort_index(kind="mergesort")
    df = _dedup_deterministic(df)
    df.index = pd.DatetimeIndex(df.index, name="ts_utc")
    return df


def _row_market(row, currency: str) -> str:
    market = row.get("market")
    if isinstance(market, str) and market:
        return market
    return market_from_currency(currency)


def _row_ts_exchange(row, ts, market: str) -> datetime:
    tsx = row.get("ts_exchange")
    if isinstance(tsx, datetime) and tsx.tzinfo is not None:
        return tsx
    if isinstance(tsx, pd.Timestamp) and tsx.tzinfo is not None:
        return tsx.to_pydatetime()
    return ts.astimezone(exchange_tz(market))


def frame_to_candles(df: pd.DataFrame, symbol: str, *,
                     interval: str | None = None) -> list[Candle]:
    """프레임 → Candle 리스트 — live/research/replay **공용 단일 구현**(#76).

    - interval: 인자 > `interval` 컬럼 > `"5m"`(기본 결정봉).
    - market/session/ts_exchange/adjusted 는 컬럼이 있으면 **상속**, 없으면 통화로 market 을
      추정한 뒤 그 거래소 tz/세션 규칙으로 유도한다(ET 하드코딩 없음).
    """
    out: list[Candle] = []
    if df is None or df.empty:
        return out
    for ts, row in df.iterrows():
        ts_utc = ts.to_pydatetime() if isinstance(ts, pd.Timestamp) else ts
        currency = row.get("currency") or "USD"
        market = _row_market(row, currency)
        ts_exchange = _row_ts_exchange(row, ts_utc, market)
        session = row.get("session")
        if not isinstance(session, str) or not session:
            session = classify_session(ts_exchange, market)
        label = interval or row.get("interval") or "5m"
        adjusted = row.get("adjusted")
        out.append(Candle(
            symbol=symbol, interval=str(label), ts_utc=ts_utc,
            ts_exchange=ts_exchange, session=session,
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=float(row["volume"]),
            currency=str(currency),
            adjusted=True if adjusted is None else bool(adjusted),
            market=market))
    return out


def _frame_to_1m_candles(df: pd.DataFrame, symbol: str) -> list[Candle]:
    """리샘플 입력 프레임 → 1분봉 Candle. interval 컬럼이 1m 이 아니면 fail-closed.

    `interval` 컬럼이 없는 프레임은 계약대로 1분봉으로 해석한다(라벨을 지어내지 않고 명시 지정).
    """
    has_label = "interval" in df.columns
    candles = frame_to_candles(df, symbol, interval=None if has_label else "1m")
    for c in candles:
        if c.interval != "1m":
            raise ValueError(
                f"리샘플 입력은 1분봉이어야 한다(interval={c.interval!r}) — 파생봉 재리샘플 금지.")
    return candles


def resample(df: pd.DataFrame, rule: str = "5min", drop_partial: bool = True, *,
             evaluation_time: datetime | None = None) -> pd.DataFrame:
    """OHLCV 리샘플링 (1분 → 5분/15분) — canonical core 위임 adapter(#76).

    - 버킷 [t, t+rule) 는 시작시각 t 로 라벨링하고 거래가 없는 빈 버킷은 만들지 않는다(기존과 동일).
    - `drop_partial=True`(기본): **닫힌 버킷만** 남긴다. 닫힘 판정은 `iloc[:-1]`(마지막 행 제거)이
      아니라 명시적 완료 경계다 — 버킷 종료 <= frontier(= `evaluation_time` 또는 입력 최대 ts).
      마지막 1분봉이 형성 중일 수 있으므로 frontier 에 +1분 하지 않는다(보수적) → 기존 동작과 동치.
    - `rule` 은 5min/15min(및 5m/15m)만 허용하고 그 외는 **fail-closed** ValueError.
    - 세션/거래소시각/market 은 원본에서 상속된다(US 하드코딩 제거 — KR 프레임도 정확).
    """
    minutes = bucket_minutes_from_rule(rule)
    if df is None or df.empty:
        return _empty_frame(_FRAME_COLUMNS)
    candles = _frame_to_1m_candles(df, _ADAPTER_SYMBOL)
    bars = resample_1m(candles, minutes, closed_only=drop_partial,
                       evaluation_time=evaluation_time)
    if not bars:
        return _empty_frame(_FRAME_COLUMNS)
    return candles_to_frame(bars)


def _ensure_et_date(df: pd.DataFrame) -> pd.DataFrame:
    """VWAP 일일 리셋 키(**거래소 로컬 날짜**)를 보장한다.

    `ts_exchange`/`market` 이 있으면 그대로 쓰고, 없을 때만 통화로 market 을 추정해 그 거래소
    tz 로 변환한다(기존 ET 하드코딩 제거 — KR 프레임의 VWAP 이 ET 날짜로 잘리지 않게).
    """
    if "et_date" in df.columns:
        return df
    df = df.copy()
    if "ts_exchange" in df.columns:
        local = pd.DatetimeIndex(pd.to_datetime(df["ts_exchange"]))
        df["et_date"] = [t.date() for t in local]
        return df
    if "market" in df.columns:
        markets = [str(m) for m in df["market"]]
    elif "currency" in df.columns:
        markets = [market_from_currency(c) for c in df["currency"]]
    else:
        markets = [MARKET_US] * len(df)
    df["et_date"] = [ts.astimezone(exchange_tz(m)).date()
                     for ts, m in zip(df.index, markets)]
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
        df = df.sort_index(kind="mergesort")
        df = _dedup_deterministic(df)           # 순서 무관 dedup(#76 (b) 통일)

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
