from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from src.models import ET, Candle
from src.research.box_mean_reversion.contract import (
    development_days,
    fold_boundaries,
    load_contract,
)
from src.research.box_mean_reversion.development import (
    Candidate,
    _completion_minute,
    _gate,
    close_candidate,
)
from src.research.box_mean_reversion.indicators import (
    ema,
    exact_median,
    wilder_atr,
    wilder_rsi,
)
from src.research.box_mean_reversion.loading import complete_regular_5m


def candle(moment: datetime, *, close: float = 100.0, high: float | None = None,
           low: float | None = None, volume: float = 100.0) -> Candle:
    return Candle.from_moment(
        "AAPL", "1m", moment,
        close, high if high is not None else close + 0.1,
        low if low is not None else close - 0.1, close, volume, "USD", True,
    )


def bar5(moment: datetime, *, open_: float, high: float, low: float, close: float) -> Candle:
    c = Candle.from_moment("AAPL", "1m", moment, open_, high, low, close, 100, "USD", True)
    return Candle(
        symbol=c.symbol, interval="5m", ts_utc=c.ts_utc, ts_exchange=c.ts_exchange,
        session=c.session, open=open_, high=high, low=low, close=close,
        volume=100, currency="USD", adjusted=True, market="US",
    )


def test_contract_is_frozen_and_oos_is_not_in_development():
    contract = load_contract()
    days = development_days(contract)
    assert days[0] == date(2023, 5, 18)
    assert days[-1] == date(2026, 1, 20)
    assert date(2026, 1, 21) not in days
    folds = fold_boundaries(contract)
    assert len(folds) == 10
    assert folds[-1][2] == days[-1]


def test_ema_seed_and_alpha():
    got = ema([10, 12, 11], 3)
    assert got == pytest.approx((10, 11, 11))


def test_exact_median_even_and_odd():
    assert exact_median([4, 1, 3, 2]) == 2.5
    assert exact_median([4, 1, 3]) == 3


def test_wilder_rsi_monotonic_gain_is_100():
    got = wilder_rsi([1, 2, 3, 4, 5, 6, 7], 5)
    assert got[4] is None
    assert got[5] == 100.0
    assert got[6] == 100.0


def test_wilder_atr_seed_is_first_period_mean():
    got = wilder_atr([2, 3, 4], [0, 1, 2], [1, 2, 3], 3)
    assert got == (None, None, 2.0)


def test_complete_regular_session_builds_78_complete_bars():
    day = date(2025, 6, 3)
    start = datetime(2025, 6, 3, 9, 30, tzinfo=ET)
    candles = tuple(candle(start + timedelta(minutes=i)) for i in range(390))
    bars, defects = complete_regular_5m(candles, day, "AAPL")
    assert defects == ()
    assert len(bars) == 78
    assert all(bar.interval == "5m" for bar in bars)


def test_signal_window_uses_completed_bar_time_for_start_labelled_bars():
    contract = load_contract()
    eligibility = contract["eligibility"]
    minimum = int(eligibility["minimum_signal_minute_et"])
    maximum = int(eligibility["maximum_signal_minute_et"])
    day = date(2025, 6, 3)

    first = bar5(
        datetime(2025, 6, 3, 10, 25, tzinfo=ET),
        open_=100,
        high=101,
        low=99,
        close=100,
    )
    last = bar5(
        datetime(2025, 6, 3, 15, 25, tzinfo=ET),
        open_=100,
        high=101,
        low=99,
        close=100,
    )
    after = bar5(
        datetime(2025, 6, 3, 15, 30, tzinfo=ET),
        open_=100,
        high=101,
        low=99,
        close=100,
    )

    assert first.ts_exchange.date() == day
    assert _completion_minute(first) == minimum
    assert _completion_minute(last) == maximum
    assert _completion_minute(after) > maximum


def test_missing_regular_minute_excludes_whole_symbol_day():
    day = date(2025, 6, 3)
    start = datetime(2025, 6, 3, 9, 30, tzinfo=ET)
    candles = tuple(candle(start + timedelta(minutes=i)) for i in range(390) if i != 17)
    bars, defects = complete_regular_5m(candles, day, "AAPL")
    assert bars == ()
    assert defects == ("incomplete_regular_session",)


def test_close_candidate_is_stop_first_on_collision():
    contract = load_contract()
    start = datetime(2025, 6, 3, 10, 35, tzinfo=ET)
    bars = [bar5(start, open_=100, high=102, low=98, close=101)]
    candidate = Candidate(
        symbol="AAPL", day=date(2025, 6, 3), signal_index=0, entry_index=0,
        signal_ts=bars[0].ts_utc, entry_ts=bars[0].ts_utc,
        entry_price=100, stop=99, target=101, atr=1, rsi=30, volume_multiple=2,
    )
    _, price, reason = close_candidate(candidate, bars, contract)
    assert price == 99
    assert reason == "stop_first"


def test_close_candidate_uses_twelfth_bar_close_for_time_exit():
    contract = load_contract()
    start = datetime(2025, 6, 3, 10, 35, tzinfo=ET)
    bars = [
        bar5(start + timedelta(minutes=5 * i), open_=100, high=100.4, low=99.6, close=100.1)
        for i in range(13)
    ]
    candidate = Candidate(
        symbol="AAPL", day=date(2025, 6, 3), signal_index=0, entry_index=0,
        signal_ts=bars[0].ts_utc, entry_ts=bars[0].ts_utc,
        entry_price=100, stop=98, target=102, atr=1, rsi=30, volume_multiple=2,
    )
    ts, price, reason = close_candidate(candidate, bars, contract)
    assert ts == bars[11].ts_utc
    assert price == pytest.approx(100.1)
    assert reason == "time_exit"


def test_infinite_profit_factor_passes_d4_but_zero_trades_still_fail_d1():
    contract = load_contract()
    stats = {
        "official": {
            "trade_count": 0, "trading_days_with_trade": 0,
            "net_expectancy": 0.0, "profit_factor": "Infinity", "mdd": 0.0,
            "positive_folds": 0, "cumulative_net_pnl": 0.0,
        },
        "stress": {"cumulative_net_pnl": 0.0},
    }
    defects = {
        "incomplete_symbol_day_ratio": 0.0, "new_conflicts": 0, "major_defects": 0,
    }
    gate = _gate(stats, defects, contract)
    assert gate["items"]["D4"] is True
    assert gate["items"]["D1"] is False
