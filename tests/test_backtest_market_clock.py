"""BacktestEngine market clock 계약(#88).

US 결과는 기존 ET 구현과 byte-for-byte 같은 report dict를 유지하고, KR은 저장된
session/KST 거래일을 사용한다. 단일 run에 시장이 섞이면 fail-closed 한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.backtest import engine as backtest_engine_module
from src.backtest.engine import BacktestEngine
from src.backtest.execution import ExecutionModel
from src.backtest.models import BacktestConfig
from src.engines.indicator_engine import candles_to_frame
from src.models import ET, KST, Candle


class _StubStrategy:
    def __init__(self, indices):
        self._indices = set(indices)

    def signal(self, symbol, indicators, t, leverage_factor):
        return ["US_BASELINE"] if t in self._indices else None


def _config():
    return BacktestConfig(
        stop_loss_pct=0.0,
        take_profit_pct=None,
        exit_on_trend_break=False,
        execution=ExecutionModel(slippage_bps=0, fee_bps=0),
        min_trades=1,
    )


def _candle(symbol, moment, close, previous, *, market, currency):
    return Candle.from_moment(
        symbol, "5m", moment,
        previous, max(previous, close) + 0.1, min(previous, close) - 0.1,
        close, 10_000, currency, True, market,
    )


def _us_baseline():
    candles = []
    previous = 99.0
    for day, closes in ((2, [100.0, 105.0, 110.0]),
                        (5, [111.0, 108.0, 112.0])):
        for offset, close in enumerate(closes):
            moment = datetime(2026, 1, day, 10, 0, tzinfo=ET) + \
                timedelta(minutes=5 * offset)
            candles.append(_candle(
                "SOXL", moment, close, previous, market="US", currency="USD"))
            previous = close
    return candles


def test_us_report_is_bar_for_bar_identical_to_pre_88_baseline():
    """#88 수정 전 main에서 캡처한 report 전체 직렬화 — US 동작 변경 금지."""
    actual = BacktestEngine(_config()).run(
        _us_baseline(), "SOXL", 3, _StubStrategy({0, 3})).to_dict()

    assert actual == {
        "symbol": "SOXL",
        "period": {
            "start": "2026-01-02T15:00:00Z",
            "end": "2026-01-05T15:10:00Z",
        },
        "summary": {
            "initial_capital": 100000.0,
            "final_equity": 100545.27027,
            "total_return_pct": 0.5453,
            "max_drawdown_pct": -0.1351,
            "win_rate": 1.0,
            "profit_factor": None,
            "expectancy": 272.635135,
            "avg_win": 272.635135,
            "avg_loss": 0.0,
            "max_consecutive_losses": 0,
            "sharpe_ratio": 0.7147,
            "total_trades": 2,
            "exposure_time": 0.6667,
            "sample_status": "OK",
        },
        "trades": [
            {
                "entry_time": "2026-01-02T15:05:00Z",
                "exit_time": "2026-01-02T15:10:00Z",
                "entry_price": 100.0,
                "exit_price": 110.0,
                "quantity": 50.0,
                "pnl": 500.0,
                "pnl_pct": 10.0,
                "entry_reason": ["US_BASELINE"],
                "exit_reason": ["EOD_FLATTEN"],
                "risk_decision": {
                    "allowed": True,
                    "adjusted_quantity": 50.0,
                    "reason_codes": [],
                    "risk_flags": ["LEVERAGED_ETF"],
                },
            },
            {
                "entry_time": "2026-01-05T15:05:00Z",
                "exit_time": "2026-01-05T15:10:00Z",
                "entry_price": 111.0,
                "exit_price": 112.0,
                "quantity": 45.27027,
                "pnl": 45.27027,
                "pnl_pct": 0.9009,
                "entry_reason": ["US_BASELINE"],
                "exit_reason": ["EOD_FLATTEN"],
                "risk_decision": {
                    "allowed": True,
                    "adjusted_quantity": 45.27027,
                    "reason_codes": [],
                    "risk_flags": ["LEVERAGED_ETF"],
                },
            },
        ],
    }


def test_dataframe_input_keeps_same_canonical_market_clock():
    candles = _us_baseline()
    expected = BacktestEngine(_config()).run(
        candles, "SOXL", 3, _StubStrategy({0, 3})).to_dict()
    actual = BacktestEngine(_config()).run(
        candles_to_frame(candles), "SOXL", 3, _StubStrategy({0, 3})).to_dict()

    assert actual == expected


def test_dataframe_unsupported_currency_does_not_fall_back_to_us():
    frame = candles_to_frame(_us_baseline()).drop(columns=["market"])
    frame["currency"] = "EUR"

    with pytest.raises(ValueError, match="currency 는 USD"):
        BacktestEngine(_config()).run(
            frame, "SOXL", 1, _StubStrategy(set()))


def test_kr_uses_stored_regular_session_and_kst_trading_day():
    """13:00 KST에 ET 날짜가 바뀌어도 같은 KRX 거래일이므로 조기청산하지 않는다."""
    closes = [100.0, 101.0, 102.0, 103.0]
    candles = []
    previous = 99.0
    for day in (8, 9):
        for offset, close in enumerate(closes):
            moment = datetime(2026, 7, day, 12, 50, tzinfo=KST) + \
                timedelta(minutes=5 * offset)
            candles.append(_candle(
                "005930", moment, close, previous, market="KR", currency="KRW"))
            previous = close

    report = BacktestEngine(_config()).run(
        candles, "005930", 3, _StubStrategy({0, 4}))

    assert len(report.trades) == 2
    for trade, first, last in zip(
            report.trades, (candles[1], candles[5]), (candles[3], candles[7])):
        assert trade.entry_time == first.ts_utc
        assert trade.exit_time == last.ts_utc
        assert trade.exit_reason == ["EOD_FLATTEN"]
        assert trade.entry_time.astimezone(KST).date() == \
            trade.exit_time.astimezone(KST).date()
        # 기존 ET 고정 코드는 전 봉을 OVERNIGHT로 분류해 진입 자체를 차단했다.
        assert trade.entry_time.astimezone(ET).date() != \
            trade.exit_time.astimezone(ET).date()


def test_default_screener_receives_detected_kr_market(monkeypatch):
    captured = []

    class _SpyScreener:
        def screen(self, inputs):
            captured.extend(inputs)
            return []

    monkeypatch.setattr(
        backtest_engine_module, "UniverseScreener", lambda: _SpyScreener())
    candles = [
        _candle(
            "005930",
            datetime(2026, 7, 8, 10, 0, tzinfo=KST) + timedelta(minutes=5 * i),
            100.0 + i, 99.0 + i, market="KR", currency="KRW",
        )
        for i in range(3)
    ]

    BacktestEngine(_config()).run(candles, "005930", 1)

    assert captured
    assert {symbol_data.market for symbol_data in captured} == {"KR"}


def test_mixed_market_run_fails_closed_even_at_same_timestamp():
    moment = datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)
    us = _candle("X", moment, 100.0, 99.0, market="US", currency="USD")
    kr = _candle("X", moment, 100.0, 99.0, market="KR", currency="KRW")

    with pytest.raises(ValueError, match="단일 market"):
        BacktestEngine(_config()).run([us, kr], "X", 1, _StubStrategy(set()))


def test_unsupported_market_fails_closed():
    candle = Candle(
        symbol="X", interval="5m",
        ts_utc=datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc),
        ts_exchange=datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc),
        session="REGULAR",
        open=100.0, high=101.0, low=99.0, close=100.0, volume=1_000,
        currency="EUR", adjusted=True, market="EU",
    )

    with pytest.raises(ValueError, match="지원하지 않는 market"):
        BacktestEngine(_config()).run([candle], "X", 1, _StubStrategy(set()))


def test_timestamp_instant_mismatch_fails_closed():
    candle = Candle(
        symbol="005930", interval="5m",
        ts_utc=datetime(2026, 7, 8, 1, 0, tzinfo=timezone.utc),
        ts_exchange=datetime(2026, 7, 8, 11, 0, tzinfo=KST),
        session="REGULAR",
        open=100.0, high=101.0, low=99.0, close=100.0, volume=1_000,
        currency="KRW", adjusted=True, market="KR",
    )

    with pytest.raises(ValueError, match="같은 순간"):
        BacktestEngine(_config()).run([candle], "005930", 1, _StubStrategy(set()))
