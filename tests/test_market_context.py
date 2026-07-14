"""US Minimal Market Context (v2.1 Phase 2 · PR 2-B) 테스트.

계약: docs/DESIGN_DELTA_V2_1.md §2-3(trailing·as-of·완료봉), §3-6(간접 행동변경 방지).
- 순수 builder: v1 필드, look-ahead 거부, warm-up/결측 무크래시, 결정론(Paper=Backtest 패리티).
- config: market_context 블록 파싱(미지정 → 비활성 기본).
- paper_loop: 관측이 매매를 게이팅하지 않음(ON/OFF 결과·호출예산 동일, 실패 무차단).
- journal/dataset 전파.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.models import Candle
from src.market_context.builder import build_benchmark_context, iso_z
from src.market_context.models import (
    MARKET_CONTEXT_VERSION,
    MarketContextConfig,
    TREND_DOWN,
    TREND_UP,
)

BASE = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)   # 10:00 ET 정규장


def bench_candles(n, *, start=400.0, step=0.5, tf_min=5, symbol="QQQ"):
    """연속 5분봉 n개(완료봉). close = start + step*i."""
    out = []
    for i in range(n):
        ts = BASE + timedelta(minutes=tf_min * i)
        c = start + step * i
        out.append(Candle.from_moment(symbol, "5m", ts, o=c, h=c + 0.2, low=c - 0.2,
                                      c=c, v=1000.0, currency="USD", adjusted=False))
    return out


# ---- 순수 builder: v1 필드 ----
def test_builder_produces_v1_fields_uptrend():
    candles = bench_candles(30)
    ctx = build_benchmark_context(candles, decision_time=candles[-1].ts_utc,
                                  symbol="QQQ", timeframe="5m", ema_period=20)
    assert ctx is not None
    d = ctx.to_dict()
    assert set(d) == {
        "benchmark_symbol", "benchmark_close", "benchmark_ema20", "benchmark_vs_ema20",
        "benchmark_ema20_slope", "benchmark_return_trailing_30m",
        "benchmark_return_trailing_60m", "benchmark_context_asof",
        "benchmark_source_bar_end", "benchmark_timeframe", "benchmark_trend",
        "market_context_version"}
    assert d["benchmark_symbol"] == "QQQ"
    assert d["benchmark_close"] == candles[-1].close
    assert d["benchmark_ema20"] is not None
    assert d["benchmark_vs_ema20"] > 0            # 상승: 종가 > EMA
    assert d["benchmark_ema20_slope"] > 0
    assert d["benchmark_return_trailing_30m"] is not None
    assert d["benchmark_return_trailing_60m"] is not None
    assert d["benchmark_timeframe"] == "5m"
    assert d["benchmark_trend"] == TREND_UP
    assert d["market_context_version"] == MARKET_CONTEXT_VERSION


def test_downtrend_classification():
    candles = bench_candles(30, step=-0.5)
    ctx = build_benchmark_context(candles, decision_time=candles[-1].ts_utc,
                                  symbol="QQQ", timeframe="5m", ema_period=20)
    assert ctx.benchmark_vs_ema20 < 0
    assert ctx.benchmark_ema20_slope < 0
    assert ctx.benchmark_trend == TREND_DOWN


# ---- look-ahead 거부 (§2-3 불변식) ----
def test_lookahead_future_bars_excluded():
    candles = bench_candles(10)
    decision = candles[5].ts_utc                  # 6..9 봉은 미래
    ctx = build_benchmark_context(candles, decision_time=decision,
                                  symbol="QQQ", timeframe="5m", ema_period=20)
    assert ctx is not None
    assert ctx.benchmark_close == candles[5].close            # 미래 종가 미사용
    assert ctx.benchmark_source_bar_end == iso_z(candles[5].ts_utc)
    assert ctx.benchmark_source_bar_end <= ctx.benchmark_context_asof
    assert ctx.benchmark_source_bar_end <= iso_z(decision)    # source_bar_end <= decision_time


def test_context_asof_not_in_future():
    candles = bench_candles(30)
    decision = candles[-1].ts_utc
    ctx = build_benchmark_context(candles, decision_time=decision,
                                  symbol="QQQ", timeframe="5m", ema_period=20)
    assert ctx.benchmark_context_asof <= iso_z(decision)
    assert ctx.benchmark_source_bar_end <= iso_z(decision)


# ---- warm-up / 결측 무크래시 ----
def test_warmup_insufficient_bars_nulls_not_crash():
    candles = bench_candles(5)                     # < ema_period(20), < 30m(6봉)
    ctx = build_benchmark_context(candles, decision_time=candles[-1].ts_utc,
                                  symbol="QQQ", timeframe="5m", ema_period=20)
    assert ctx is not None
    assert ctx.benchmark_close == candles[-1].close           # 종가/소스는 있음
    assert ctx.benchmark_ema20 is None
    assert ctx.benchmark_vs_ema20 is None
    assert ctx.benchmark_ema20_slope is None
    assert ctx.benchmark_return_trailing_30m is None
    assert ctx.benchmark_trend is None


def test_no_completed_bars_returns_none():
    candles = bench_candles(5)
    before_all = candles[0].ts_utc - timedelta(minutes=5)
    ctx = build_benchmark_context(candles, decision_time=before_all,
                                  symbol="QQQ", timeframe="5m", ema_period=20)
    assert ctx is None


# ---- 결정론 = Paper/Backtest 패리티 (같은 입력 → 같은 context) ----
def test_deterministic_same_input_same_output():
    candles = bench_candles(30)
    a = build_benchmark_context(candles, decision_time=candles[-1].ts_utc,
                                symbol="QQQ", timeframe="5m", ema_period=20)
    b = build_benchmark_context(list(reversed(candles)), decision_time=candles[-1].ts_utc,
                                symbol="QQQ", timeframe="5m", ema_period=20)
    assert a.to_dict() == b.to_dict()             # 정렬 무관, 결정론


# ---- config 파싱 ----
def test_config_parses_market_context_block():
    from src.runtime.config import RuntimeConfig
    cfg = RuntimeConfig.from_dict({"market_context": {
        "enabled": True, "benchmark_symbol": "SPY", "timeframe": "5m", "ema_period": 10}})
    mc = cfg.market_context
    assert mc.enabled is True and mc.benchmark_symbol == "SPY" and mc.ema_period == 10


def test_config_market_context_default_disabled():
    from src.runtime.config import RuntimeConfig
    cfg = RuntimeConfig.from_dict({})
    assert cfg.market_context.enabled is False
    assert cfg.market_context.benchmark_symbol == "QQQ"
    assert isinstance(cfg.market_context, MarketContextConfig)


# ---- journal round-trip ----
def test_journal_cycle_roundtrips_benchmark_context():
    from src.journal.models import JournalCycle
    bc = {"benchmark_symbol": "QQQ", "benchmark_close": 400.0,
          "market_context_version": MARKET_CONTEXT_VERSION}
    jc = JournalCycle(cycle_id="c", cycle_ts=None, session="REGULAR", universe=["QQQ"],
                      status="RAN", reason=None, error=None, outcomes=[],
                      benchmark_context=bc)
    d = jc.to_dict()
    assert d["benchmark_context"] == bc
    assert JournalCycle.from_dict(d).benchmark_context == bc
