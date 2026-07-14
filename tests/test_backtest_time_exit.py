"""BacktestConfig time-exit 배선(PR 3-B) — 설계 §6 승인 항목.

기본값(None)은 백테스트 동작 무변경(TIME_EXIT off), 설정 시 엔진 청산 경로에 도달.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.backtest.engine import BacktestEngine, _exit_config_from_backtest
from src.backtest.execution import ExecutionModel
from src.backtest.models import BacktestConfig
from src.models import ET, Candle

REG_OPEN = 10  # 10:00 ET 정규장


def _candle(day, minute_offset, o, h, l, c, v=10000.0):
    moment = datetime(2026, 6, day, REG_OPEN, 0, tzinfo=ET) + timedelta(minutes=minute_offset)
    return Candle.from_moment("SOXL", "5m", moment, o, h, l, c, v, "USD", True)


def _flat_day(day, n, price=100.0):
    """가격 평탄한 하루치 n봉(SL/TP/trend 미발화)."""
    return [_candle(day, k, price, price + 0.1, price - 0.1, price) for k in range(n)]


class _Stub:
    def __init__(self, indices):
        self.idx = set(indices)

    def signal(self, symbol, indicators, t, leverage_factor):
        return ["FORCE"] if t in self.idx else None


_NO_RULE = dict(stop_loss_pct=0.0, take_profit_pct=None, exit_on_trend_break=False,
                execution=ExecutionModel(slippage_bps=0, fee_bps=0), min_trades=1)


def test_exit_config_maps_time_exit_fields():
    # 기본값 → None (백테스트 동작 무변경)
    d = _exit_config_from_backtest(BacktestConfig())
    assert d.time_exit_minutes is None and d.time_exit_bars is None
    # 설정값 → 매핑 통과
    cfg = BacktestConfig(time_exit_minutes=60.0, time_exit_bars=12)
    m = _exit_config_from_backtest(cfg)
    assert m.time_exit_minutes == 60.0 and m.time_exit_bars == 12


def test_time_exit_bars_fires_in_backtest():
    """time_exit_bars=1 → 진입 다음다음 봉에서 TIME_EXIT 청산(세션말 EOD 이전)."""
    candles = _flat_day(1, 5)  # 5봉, 같은 ET 날짜
    cfg = BacktestConfig(time_exit_bars=1, **_NO_RULE)
    rep = BacktestEngine(cfg).run(candles, "SOXL", leverage_factor=3,
                                  entry_strategy=_Stub([0]))
    assert len(rep.trades) == 1
    assert rep.trades[0].exit_reason == ["TIME_EXIT"]


def test_default_no_time_exit_holds_to_eod():
    """기본(time_exit=None) → TIME_EXIT 미발화, 세션말 EOD_FLATTEN 로 청산(기존 동작)."""
    candles = _flat_day(1, 5)
    cfg = BacktestConfig(**_NO_RULE)  # time_exit 미설정
    rep = BacktestEngine(cfg).run(candles, "SOXL", leverage_factor=3,
                                  entry_strategy=_Stub([0]))
    assert len(rep.trades) == 1
    assert rep.trades[0].exit_reason == ["EOD_FLATTEN"]
