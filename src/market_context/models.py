"""US Minimal Market Context 모델 (v2.1 PR 2-B, 계약서 §2-3).

- 순수 데이터 컨테이너 + config. 매매/판단 로직 없음(관측 전용).
- `BenchmarkContext` 는 벤치마크(기본 QQQ) 국면의 v1 스냅샷. trailing·as-of 명시,
  완료봉만 사용(look-ahead 금지). 어떤 결정에도 되먹임되지 않는다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

# 스키마 버전(필드 구성 변경 시 bump).
MARKET_CONTEXT_VERSION = "1.0"

# benchmark_trend 라벨(종가 vs EMA + slope 로 결정).
TREND_UP = "UP"
TREND_DOWN = "DOWN"
TREND_NEUTRAL = "NEUTRAL"


@dataclass(frozen=True)
class MarketContextConfig:
    """market_context 블록. 기본 비활성(opt-in) — 켜져도 매매를 바꾸지 않는다.

    benchmark 는 이미 universe 에서 가져온 candle 을 재사용한다(추가 API 0). universe 에
    없으면 관측을 생략한다(별도 fetch 금지 → KRX benchmark 는 자연히 비활성).
    """
    enabled: bool = False
    benchmark_symbol: str = "QQQ"
    timeframe: str = "5m"
    ema_period: int = 20


@dataclass(frozen=True)
class BenchmarkContext:
    """벤치마크 국면 v1 스냅샷. 모든 시각은 ISO-8601 UTC('...Z') 문자열.

    불변식(builder 가 보장): source_bar_end ≤ context_asof ≤ decision_time(완료봉만).
    warm-up/결측 지표는 None(관측이 매매를 막지 않음).
    """
    benchmark_symbol: str
    benchmark_close: float | None
    benchmark_ema20: float | None
    benchmark_vs_ema20: float | None          # (close - ema20) / ema20
    benchmark_ema20_slope: float | None       # (ema20_t - ema20_{t-1}) / ema20_{t-1}
    benchmark_return_trailing_30m: float | None
    benchmark_return_trailing_60m: float | None
    benchmark_context_asof: str | None        # 결정 시점(as-of)
    benchmark_source_bar_end: str | None      # 마지막 완료봉의 canonical ts(left-label=봉 시작)
    #   ↑ 봉 종료시각이 아니라 '마지막 완료봉' 타임스탬프. 결정 그리드와 동일 앵커라
    #     source_bar_end ≤ decision_time(=candidate decision) 불변식이 성립한다.
    benchmark_timeframe: str
    benchmark_trend: str | None               # UP | DOWN | NEUTRAL | None
    market_context_version: str = MARKET_CONTEXT_VERSION

    def to_dict(self) -> dict:
        return asdict(self)
