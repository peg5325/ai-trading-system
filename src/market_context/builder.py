"""build_benchmark_context — 벤치마크 국면 v1 스냅샷을 만드는 순수 함수 (계약서 §2-3, §3-6).

원칙:
- 순수·결정론적: 같은 입력 → 같은 출력(now()/난수 미사용). Paper·Backtest 가 동일 결과.
- 완료봉만: `ts_utc ≤ decision_time` 인 봉만 사용(look-ahead 금지). source_bar_end ≤ decision_time,
  context_asof ≤ decision_time.
- 관측 전용: 어떤 결정에도 되먹임되지 않는다. warm-up/결측은 None(매매를 막지 않음).
- 지표(EMA)는 live/backtest 와 동일한 `src.engines.indicators.ema` 재사용.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.engines.indicators import ema
from src.market_context.models import (
    MARKET_CONTEXT_VERSION,
    TREND_DOWN,
    TREND_NEUTRAL,
    TREND_UP,
    BenchmarkContext,
)


def iso_z(dt: datetime) -> str:
    """aware datetime → 'YYYY-MM-DDTHH:MM:SSZ' (UTC)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tf_minutes(timeframe: str) -> int:
    """'5m' → 5. 파싱 실패 시 5(기본 5분봉)."""
    try:
        s = str(timeframe).strip().lower()
        if s.endswith("m"):
            return int(s[:-1])
        return int(s)
    except (ValueError, TypeError):
        return 5


def _opt(v) -> float | None:
    if v is None or pd.isna(v):
        return None
    return float(v)


def _trailing_return(closes: list[float], n_bars: int) -> float | None:
    """n_bars 전 완료봉 대비 trailing return. 데이터 부족/0가격이면 None."""
    if n_bars <= 0 or len(closes) <= n_bars:
        return None
    prev = closes[-1 - n_bars]
    if not prev:
        return None
    return (closes[-1] - prev) / prev


def _infer_tf_minutes(candles, fallback: str) -> int:
    """완료봉 ts_utc 간격(중앙값, 분)으로 실제 timeframe 추론. trailing 윈도가 config 오설정에
    흔들리지 않도록 데이터에서 직접 유도한다. 2봉 미만이면 라벨(fallback) 파싱."""
    if len(candles) >= 2:
        deltas = sorted((candles[i].ts_utc - candles[i - 1].ts_utc).total_seconds() / 60.0
                        for i in range(1, len(candles)))
        med = deltas[len(deltas) // 2]
        if med > 0:
            return max(1, int(round(med)))
    return _tf_minutes(fallback)


def _trend(close: float | None, ema20: float | None, slope: float | None) -> str | None:
    """UP/DOWN/NEUTRAL. 종가·EMA·slope 셋 다 필요 — 하나라도 결측이면 None(불확실)."""
    if close is None or ema20 is None or slope is None:
        return None
    above = close > ema20
    if above and slope > 0:
        return TREND_UP
    if (not above) and slope < 0:
        return TREND_DOWN
    return TREND_NEUTRAL


def build_benchmark_context(candles, *, decision_time: datetime, symbol: str,
                            timeframe: str = "5m", ema_period: int = 20,
                            trailing_min: tuple[int, ...] = (30, 60)
                            ) -> BenchmarkContext | None:
    """벤치마크 candle + 결정 시각으로 v1 국면 스냅샷을 만든다.

    candles: 벤치마크(예: QQQ)의 Candle 리스트(정렬 무관). decision_time: 결정 시각(aware UTC).
    완료봉(ts_utc ≤ decision_time)만 사용한다. 완료봉이 없으면 None(관측 없음).
    """
    completed = sorted((c for c in candles if c.ts_utc <= decision_time),
                       key=lambda c: c.ts_utc)
    if not completed:
        return None

    closes = [float(c.close) for c in completed]
    source = completed[-1]
    close = closes[-1]

    # EMA(period) — live/backtest 와 동일 함수. warm-up 은 NaN → None.
    ema_series = ema(pd.Series(closes, dtype=float), ema_period)
    ema_last = _opt(ema_series.iloc[-1])
    ema_prev = _opt(ema_series.iloc[-2]) if len(ema_series) >= 2 else None

    vs_ema = ((close - ema_last) / ema_last) if (ema_last not in (None, 0)) else None
    slope = ((ema_last - ema_prev) / ema_prev) if (ema_last is not None
                                                   and ema_prev not in (None, 0)) else None

    # trailing 윈도는 실제 봉 간격에서 유도(config timeframe 오설정에 흔들리지 않게).
    tf_min = _infer_tf_minutes(completed, timeframe)
    r30 = _trailing_return(closes, round(30 / tf_min))
    r60 = _trailing_return(closes, round(60 / tf_min))

    return BenchmarkContext(
        benchmark_symbol=symbol,
        benchmark_close=close,
        benchmark_ema20=ema_last,
        benchmark_ema_period=int(ema_period),   # 필드명(ema20) 과 무관하게 실제 기간 기록
        benchmark_vs_ema20=vs_ema,
        benchmark_ema20_slope=slope,
        benchmark_return_trailing_30m=r30,
        benchmark_return_trailing_60m=r60,
        benchmark_context_asof=iso_z(decision_time),
        # source_bar_end: 마지막 완료봉의 canonical ts(= left-label, 봉 시작).
        #   결정 그리드(cycle_id/candidate decision)와 동일 앵커라 source_bar_end ≤ decision_time
        #   불변식이 성립한다("end" 는 '마지막 완료봉'의 의미 — 봉 종료시각이 아님).
        benchmark_source_bar_end=iso_z(source.ts_utc),
        benchmark_timeframe=f"{tf_min}m",
        benchmark_trend=_trend(close, ema_last, slope),
        market_context_version=MARKET_CONTEXT_VERSION,
    )
