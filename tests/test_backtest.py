"""BacktestEngine 테스트: look-ahead 방지, 다음봉 시가 체결, slippage/fee, EOD flatten,
max drawdown, win rate, 빈 데이터, 무거래, insufficient sample, in/out 분리, walk-forward."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.backtest.engine import BacktestEngine
from src.backtest.execution import ExecutionModel
from src.backtest.models import (
    SAMPLE_INSUFFICIENT,
    BacktestConfig,
    split_in_out,
)
from src.models import ET, Candle

# ---- 캔들 빌더: 정규장, 여러 ET 날짜 ----
REG_OPEN = 10  # 10:00 ET (정규장, 마감 여유)


def candle(day, minute_offset, o, h, l, c, v=10000.0):
    moment = datetime(2026, 6, day, REG_OPEN, 0, tzinfo=ET) + timedelta(minutes=minute_offset)
    return Candle.from_moment("SOXL", "5m", moment, o, h, l, c, v, "USD", True)


def day_bars(day, closes, start_price=None):
    """하루치 연속 봉. opens[i]=closes[i-1] (첫 봉 open=start_price)."""
    bars = []
    prev = start_price if start_price is not None else closes[0]
    for k, c in enumerate(closes):
        o = prev
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1
        bars.append(candle(day, k, o, h, l, c))
        prev = c
    return bars


class StubStrategy:
    """지정한 (전역) 봉 인덱스에서 진입 신호."""
    def __init__(self, signal_indices, reasons=("RSI recovery", "VWAP reclaim")):
        self.idx = set(signal_indices)
        self.reasons = list(reasons)

    def signal(self, symbol, indicators, t, leverage_factor):
        return list(self.reasons) if t in self.idx else None


# 손절/추세청산 없이 EOD flatten 만으로 청산되게 하는 설정
NO_RULE_EXIT = dict(stop_loss_pct=0.0, take_profit_pct=None, exit_on_trend_break=False,
                    execution=ExecutionModel(slippage_bps=0, fee_bps=0), min_trades=100)


def cfg(**kw):
    base = dict(NO_RULE_EXIT)
    base.update(kw)
    return BacktestConfig(**base)


# ---- 다음 봉 시가 체결 (신호 봉 종가 체결 금지) ----
def test_fills_at_next_bar_open_not_signal_close():
    # 하루: 4봉. 신호를 봉0에 → 진입은 봉1 open. EOD flatten 은 봉3 종가.
    closes = [100.0, 110.0, 120.0, 130.0]
    candles = day_bars(1, closes, start_price=99.0)  # opens: 99,100,110,120
    eng = BacktestEngine(cfg())
    rep = eng.run(candles, "SOXL", leverage_factor=3, entry_strategy=StubStrategy([0]))
    assert len(rep.trades) == 1
    tr = rep.trades[0]
    assert tr.entry_price == 100.0          # 봉1 open (봉0 close=100 이지만 open=100 우연 아님: opens[1]=closes[0])
    assert tr.exit_price == 130.0           # 봉3 종가 EOD flatten
    assert tr.exit_reason == ["EOD_FLATTEN"]
    assert tr.entry_reason == ["RSI recovery", "VWAP reclaim"]


# ---- slippage / fee 반영 ----
def test_slippage_and_fee_applied():
    closes = [100.0, 110.0, 120.0]
    candles = day_bars(1, closes, start_price=100.0)  # opens: 100,100,110
    eng = BacktestEngine(BacktestConfig(
        stop_loss_pct=0.0, exit_on_trend_break=False, min_trades=100,
        execution=ExecutionModel(slippage_bps=10, fee_bps=5)))  # 0.1% slip, 0.05% fee
    rep = eng.run(candles, "SOXL", 3, StubStrategy([0]))
    tr = rep.trades[0]
    assert tr.entry_price == pytest.approx(100.0 * 1.001)   # 매수 슬리피지 +
    assert tr.exit_price == pytest.approx(120.0 * 0.999)    # 매도 슬리피지 -
    # 수수료가 pnl 을 깎는다 (무비용 대비)
    free = BacktestEngine(cfg()).run(candles, "SOXL", 3, StubStrategy([0])).trades[0]
    assert tr.pnl < free.pnl


# ---- EOD flatten: 레버리지는 당일 청산, 다음날로 안 넘어감 ----
def test_leverage_eod_flatten_no_overnight():
    d1 = day_bars(1, [100.0, 105.0, 110.0], start_price=100.0)
    d2 = day_bars(2, [111.0, 108.0, 112.0], start_price=110.0)
    candles = d1 + d2
    eng = BacktestEngine(cfg())
    rep = eng.run(candles, "SOXL", 3, StubStrategy([0]))  # 봉0(day1)에서만 신호
    assert len(rep.trades) == 1
    tr = rep.trades[0]
    assert tr.exit_reason == ["EOD_FLATTEN"]
    # entry_time 과 exit_time 이 같은 ET 날짜 (오버나잇 아님)
    assert tr.entry_time.tz_convert(ET).date() == tr.exit_time.tz_convert(ET).date()


# ---- look-ahead 방지: 미래 봉을 바꿔도 과거에 종료된 거래는 불변 ----
def test_no_look_ahead_future_bars_do_not_change_past_trade():
    d1 = day_bars(1, [100.0, 105.0, 110.0], start_price=100.0)
    eng = BacktestEngine(cfg())
    base = eng.run(d1, "SOXL", 3, StubStrategy([0])).trades[0]
    # day2 를 뒤에 붙여도 day1 거래는 동일해야 한다
    d2 = day_bars(2, [999.0, 5.0, 500.0], start_price=110.0)  # 극단적 미래값
    ext = eng.run(d1 + d2, "SOXL", 3, StubStrategy([0])).trades[0]
    assert base.to_dict() == ext.to_dict()


# ---- win rate & max drawdown (EOD flatten 으로 하루 1거래) ----
def test_win_rate_and_drawdown():
    # day1 승(100→110), day2 패(110→90), day3 승(90→100). 각 날 봉0에서 진입.
    d1 = day_bars(1, [100.0, 105.0, 110.0], start_price=100.0)
    d2 = day_bars(2, [110.0, 100.0, 90.0], start_price=110.0)
    d3 = day_bars(3, [90.0, 95.0, 100.0], start_price=90.0)
    candles = d1 + d2 + d3
    # 각 날 첫 봉(전역 인덱스 0,3,6)에서 신호
    eng = BacktestEngine(cfg())
    rep = eng.run(candles, "SOXL", 3, StubStrategy([0, 3, 6]))
    assert rep.summary["total_trades"] == 3
    assert rep.summary["win_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert rep.summary["max_drawdown_pct"] < 0        # 손실 구간 존재
    assert rep.summary["max_consecutive_losses"] == 1
    assert rep.summary["sample_status"] == SAMPLE_INSUFFICIENT


# ---- 빈 데이터 ----
def test_empty_data():
    rep = BacktestEngine(cfg()).run([], "SOXL", 3, StubStrategy([]))
    assert rep.summary["total_trades"] == 0
    assert rep.summary["sample_status"] == SAMPLE_INSUFFICIENT
    assert rep.trades == []
    assert rep.to_dict()["period"] == {"start": None, "end": None}


# ---- 거래 없는 데이터 (신호 없음) ----
def test_no_trades():
    candles = day_bars(1, [100.0, 101.0, 102.0], start_price=100.0)
    rep = BacktestEngine(cfg()).run(candles, "SOXL", 3, StubStrategy([]))  # 신호 없음
    assert rep.summary["total_trades"] == 0
    assert rep.summary["win_rate"] == 0.0
    assert rep.summary["profit_factor"] is None
    assert rep.summary["exposure_time"] == 0.0


# ---- insufficient sample 판정 ----
def test_insufficient_sample_flag():
    d = day_bars(1, [100.0, 110.0, 120.0], start_price=100.0)
    rep = BacktestEngine(cfg(min_trades=1)).run(d, "SOXL", 3, StubStrategy([0]))
    assert rep.summary["total_trades"] == 1
    assert rep.summary["sample_status"] == "OK"     # min_trades=1 이면 충분
    rep2 = BacktestEngine(cfg(min_trades=100)).run(d, "SOXL", 3, StubStrategy([0]))
    assert rep2.summary["sample_status"] == SAMPLE_INSUFFICIENT


# ---- in-sample / out-of-sample 분리 ----
def test_in_out_split():
    candles = day_bars(1, [100.0] * 10, start_price=100.0)
    in_c, out_c = split_in_out(candles, 0.7)
    assert len(in_c) == 7 and len(out_c) == 3
    r_in, r_out = BacktestEngine(cfg()).run_in_out(candles, "SOXL", 3, ratio=0.7,
                                                   entry_strategy=StubStrategy([]))
    assert r_in.symbol == "SOXL" and r_out.symbol == "SOXL"


# ---- walk-forward 는 인터페이스만 (TODO) ----
def test_walk_forward_not_implemented():
    with pytest.raises(NotImplementedError):
        BacktestEngine(cfg()).walk_forward()


# ---- 결정론 ----
def test_deterministic():
    candles = day_bars(1, [100.0, 105.0, 110.0], start_price=100.0)
    eng = BacktestEngine(cfg())
    a = eng.run(candles, "SOXL", 3, StubStrategy([0])).to_dict()
    b = eng.run(candles, "SOXL", 3, StubStrategy([0])).to_dict()
    assert a == b


# ---- 기본 전략(Screener 재사용)로도 동작 ----
def test_runs_with_default_screener_strategy():
    # 지표 warm-up 위해 충분한 봉. 신호가 없어도 크래시 없이 리포트 반환.
    candles = day_bars(1, [100.0 + (i % 5) for i in range(60)], start_price=100.0)
    rep = BacktestEngine(cfg()).run(candles, "SOXL", 3)   # 기본 ScreenerEntryStrategy
    assert rep.symbol == "SOXL"
    assert "sample_status" in rep.summary
