"""market_context — US 최소 시장 국면 관측(v2.1 PR 2-B).

벤치마크(기본 QQQ)의 완료봉으로 국면 v1 스냅샷을 만든다. 관측 전용이며 매매 행동을
바꾸지 않는다(계약서 §2-3, §3-6). 자세한 계약은 README.md 참조.
"""
from src.market_context.builder import build_benchmark_context, iso_z
from src.market_context.models import (
    MARKET_CONTEXT_VERSION,
    TREND_DOWN,
    TREND_NEUTRAL,
    TREND_UP,
    BenchmarkContext,
    MarketContextConfig,
)

__all__ = [
    "build_benchmark_context",
    "iso_z",
    "BenchmarkContext",
    "MarketContextConfig",
    "MARKET_CONTEXT_VERSION",
    "TREND_UP",
    "TREND_DOWN",
    "TREND_NEUTRAL",
]
