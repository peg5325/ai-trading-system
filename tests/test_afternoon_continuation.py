from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from src.models import ET, Candle
from src.research.afternoon_continuation.contract import evaluation_days, fold_boundaries, load_contract
from src.research.afternoon_continuation.development import (
    Candidate,
    _gate,
    close_candidate,
    detect_candidate,
)
from src.research.afternoon_continuation.indicators import cumulative_vwap, ema, exact_median, wilder_atr
from src.research.afternoon_continuation.loading import complete_regular_5m


def candle(moment: datetime, close: float = 100.0, volume: float = 100.0) -> Candle:
    return Candle.from_moment(
        "AAPL", "1m", moment, close, close + 0.1, close - 0.1, close,
        volume, "USD", True,
    )


def bar5(moment: datetime, *, open_: float, high: float, low: float, close: float,
         volume: float = 100.0) -> Candle:
    base = Candle.from_moment(
        "AAPL", "1m", moment, open_, high, low, close, volume, "USD", True)
    return Candle(
        symbol=base.symbol, interval="5m", ts_utc=base.ts_utc,
        ts_exchange=base.ts_exchange, session=base.session, open=open_, high=high,
        low=low, close=close, volume=volume, currency="USD", adjusted=True,
        market="US",
    )


def continuation_bars() -> list[Candle]:
    start = datetime(2025, 6, 3, 9, 30, tzinfo=ET)
    bars: list[Candle] = []
    for i in range(78):
        moment = start + timedelta(minutes=5 * i)
        if i < 12:
            close = 100 + (i + 1) / 12
            open_ = 100 if i == 0 else 100 + i / 12
            bars.append(bar5(
                moment, open_=open_, high=close + .08, low=open_ - .08,
                close=close, volume=200,
            ))
        elif i < 20:
            close = 100.95 - (i - 11) * .06
            bars.append(bar5(
                moment, open_=close + .03, high=close + .11, low=close - .08,
                close=close,
            ))
        elif i == 20:
            bars.append(bar5(
                moment, open_=100.5, high=101.05, low=100.4, close=101.0,
                volume=300,
            ))
        else:
            bars.append(bar5(
                moment, open_=101, high=101.1, low=100.9, close=101,
            ))
    return bars


def test_contract_is_frozen_and_blind_oos_is_excluded():
    contract = load_contract()
    days = evaluation_days(contract)
    assert days[0] == date(2023, 5, 18)
    assert days[-1] == date(2026, 1, 20)
    assert date(2026, 1, 21) not in days
    assert len(fold_boundaries(contract)) == 10


def test_indicator_conventions_are_deterministic():
    assert ema([10, 12, 11], 3) == pytest.approx((10, 11, 11))
    assert exact_median([4, 1, 3, 2]) == 2.5
    assert wilder_atr([2, 3, 4], [0, 1, 2], [1, 2, 3], 3) == (None, None, 2.0)
    assert cumulative_vwap([2, 4], [0, 2], [1, 3], [1, 3]) == pytest.approx((1, 2.5))


def test_complete_regular_session_resamples_to_78_bars():
    day = date(2025, 6, 3)
    start = datetime(2025, 6, 3, 9, 30, tzinfo=ET)
    candles = tuple(candle(start + timedelta(minutes=i)) for i in range(390))
    bars, defects = complete_regular_5m(candles, day, "AAPL")
    assert defects == ()
    assert len(bars) == 78


def test_missing_minute_excludes_symbol_day():
    day = date(2025, 6, 3)
    start = datetime(2025, 6, 3, 9, 30, tzinfo=ET)
    candles = tuple(candle(start + timedelta(minutes=i)) for i in range(390) if i != 10)
    bars, defects = complete_regular_5m(candles, day, "AAPL")
    assert bars == ()
    assert defects == ("incomplete_regular_session",)


def test_close_candidate_stop_first_on_collision():
    start = datetime(2025, 6, 3, 13, 0, tzinfo=ET)
    bars = [bar5(start, open_=100, high=102, low=98, close=101)]
    candidate = Candidate(
        symbol="AAPL", day=date(2025, 6, 3), entry_index=0,
        signal_ts=bars[0].ts_utc, entry_ts=bars[0].ts_utc, entry_price=100,
        stop=99, target=101, morning_return=.01, morning_volume_multiple=1.5,
    )
    _, price, reason = close_candidate(candidate, bars)
    assert price == 99
    assert reason == "stop_first"


def test_primary_signal_uses_next_bar_open_and_ignores_later_bars():
    contract = load_contract()
    bars = continuation_bars()
    candidate, reasons = detect_candidate(
        "AAPL", date(2025, 6, 3), bars, baseline_volume=1200, contract=contract)
    assert reasons == {}
    assert candidate is not None
    assert candidate.entry_index == 21
    assert candidate.entry_price == bars[21].open
    changed = list(bars)
    changed[40] = bar5(
        changed[40].ts_exchange, open_=50, high=51, low=49, close=50,
        volume=999999,
    )
    candidate_after, _ = detect_candidate(
        "AAPL", date(2025, 6, 3), changed, baseline_volume=1200, contract=contract)
    assert candidate_after == candidate


def test_close_candidate_uses_last_regular_close():
    start = datetime(2025, 6, 3, 13, 0, tzinfo=ET)
    bars = [
        bar5(start + timedelta(minutes=5 * i), open_=100, high=100.2, low=99.8, close=100.1)
        for i in range(4)
    ]
    candidate = Candidate(
        symbol="AAPL", day=date(2025, 6, 3), entry_index=0,
        signal_ts=bars[0].ts_utc, entry_ts=bars[0].ts_utc, entry_price=100,
        stop=98, target=103, morning_return=.01, morning_volume_multiple=1.5,
    )
    ts, price, reason = close_candidate(candidate, bars)
    assert ts == bars[-1].ts_utc
    assert price == pytest.approx(100.1)
    assert reason == "eod"


def test_gate_rejects_bad_economics_even_when_data_passes():
    contract = load_contract()
    stats = {
        "official": {"trade_count": 400, "trading_days_with_trade": 250,
                     "net_expectancy": -1, "profit_factor": .8, "mdd": .05,
                     "positive_folds": 4, "cumulative_net_pnl": -400},
        "stress": {"cumulative_net_pnl": -800},
    }
    defects = {"incomplete_symbol_day_ratio": .04, "new_conflicts": 0,
               "major_defects": 0}
    gate = _gate(stats, defects, contract)
    assert gate["verdict"] == "NO_SELECTION"
    assert gate["items"]["D9"] is True


def test_gate_marks_data_failure_inconclusive():
    contract = load_contract()
    stats = {
        "official": {"trade_count": 400, "trading_days_with_trade": 250,
                     "net_expectancy": 1, "profit_factor": 1.5, "mdd": .05,
                     "positive_folds": 8, "cumulative_net_pnl": 400},
        "stress": {"cumulative_net_pnl": 100},
    }
    defects = {"incomplete_symbol_day_ratio": .051, "new_conflicts": 0,
               "major_defects": 0}
    assert _gate(stats, defects, contract)["verdict"] == "INCONCLUSIVE_DATA"
