from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from src.models import ET, Candle
from src.research.spy_intraday_momentum.contract import (
    evaluation_days,
    fold_boundaries,
    load_contract,
)
from src.research.spy_intraday_momentum.development import (
    GrossTrade,
    _gate,
    gross_pnl,
    net_pnl,
    signal_direction,
    trade_for_day,
)
from src.research.spy_intraday_momentum.loading import complete_regular_1m


def candle(
    moment: datetime,
    *,
    symbol: str = "SPY",
    open_: float = 100.0,
    close: float = 100.0,
) -> Candle:
    return Candle.from_moment(
        symbol,
        "1m",
        moment,
        open_,
        max(open_, close) + 0.1,
        min(open_, close) - 0.1,
        close,
        1000.0,
        "USD",
        True,
    )


def full_day(day: date, close_minute: int = 960) -> tuple[Candle, ...]:
    start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET)
    return tuple(candle(start + timedelta(minutes=index))
                 for index in range(close_minute - 570))


def replace_minute(
    bars: tuple[Candle, ...], minute: int, *, open_: float, close: float
) -> tuple[Candle, ...]:
    values = list(bars)
    values[minute - 570] = candle(
        values[minute - 570].ts_exchange, open_=open_, close=close
    )
    return tuple(values)


def test_contract_is_frozen_and_blind_oos_is_excluded():
    contract = load_contract()
    days = evaluation_days(contract)
    assert days[0] == date(2023, 5, 18)
    assert days[-1] == date(2026, 1, 20)
    assert date(2026, 1, 21) not in days
    assert len(fold_boundaries(contract)) == 10
    assert contract["discipline"]["sensitivity_specs"] == 0


def test_complete_regular_session_has_exact_390_minute_grid():
    day = date(2025, 6, 3)
    bars, defects = complete_regular_1m(full_day(day), day, "SPY")
    assert defects == ()
    assert len(bars) == 390
    assert bars[0].ts_exchange.strftime("%H:%M") == "09:30"
    assert bars[-1].ts_exchange.strftime("%H:%M") == "15:59"


def test_missing_minute_excludes_day():
    day = date(2025, 6, 3)
    source = full_day(day)
    bars, defects = complete_regular_1m(source[:10] + source[11:], day, "SPY")
    assert bars == ()
    assert defects == ("incomplete_regular_session",)


def test_signal_direction_is_only_sign():
    assert signal_direction(0.001) == "LONG"
    assert signal_direction(-0.001) == "SHORT"
    assert signal_direction(0.0) is None


def test_positive_signal_enters_1530_open_and_exits_1559_close():
    day = date(2025, 6, 3)
    bars = replace_minute(full_day(day), 599, open_=101.0, close=101.0)
    bars = replace_minute(bars, 930, open_=102.0, close=102.1)
    bars = replace_minute(bars, 959, open_=103.0, close=104.0)
    trade = trade_for_day(day, bars, previous_close=100.0, contract=load_contract())
    assert trade is not None
    assert trade.direction == "LONG"
    assert trade.entry_price == 102.0
    assert trade.exit_price == 104.0
    assert trade.entry_ts.endswith("-04:00") is False  # stored execution timestamp is UTC
    assert trade.qty == 980


def test_negative_signal_creates_short_and_reverses_gross_move():
    day = date(2025, 6, 3)
    bars = replace_minute(full_day(day), 599, open_=99.0, close=99.0)
    bars = replace_minute(bars, 930, open_=100.0, close=100.0)
    bars = replace_minute(bars, 959, open_=98.0, close=98.0)
    trade = trade_for_day(day, bars, previous_close=100.0, contract=load_contract())
    assert trade is not None
    assert trade.direction == "SHORT"
    assert gross_pnl(trade) == 2000.0


def test_early_close_uses_last_half_hour_dynamically():
    day = date(2025, 7, 3)
    bars, defects = complete_regular_1m(full_day(day, close_minute=780), day, "SPY")
    assert defects == ()
    bars = replace_minute(bars, 599, open_=101.0, close=101.0)
    bars = replace_minute(bars, 750, open_=102.0, close=102.0)
    bars = replace_minute(bars, 779, open_=103.0, close=103.0)
    trade = trade_for_day(day, bars, previous_close=100.0, contract=load_contract())
    assert trade is not None
    assert trade.entry_price == 102.0
    assert trade.exit_price == 103.0


def test_cost_is_applied_to_both_entry_and_exit_notional():
    trade = GrossTrade(
        day=date(2025, 6, 3), direction="LONG", signal_return=0.01,
        entry_ts="entry", exit_ts="exit", entry_price=100.0, exit_price=101.0, qty=100,
    )
    assert gross_pnl(trade) == 100.0
    assert net_pnl(trade, 0.34) == pytest.approx(65.83)


def test_gate_rejects_bad_economics_when_data_passes():
    stats = {
        "official": {"trade_count": 400, "trading_days_with_trade": 400,
                     "net_expectancy": -1, "profit_factor": .8, "mdd": .05,
                     "positive_folds": 4, "cumulative_net_pnl": -400},
        "stress": {"cumulative_net_pnl": -800},
    }
    defects = {"incomplete_spy_day_ratio": .04, "missing_previous_close_ratio": 0,
               "new_conflicts": 0, "major_defects": 0}
    gate = _gate(stats, defects, load_contract())
    assert gate["verdict"] == "NO_SELECTION"
    assert gate["items"]["D9"] is True


def test_gate_marks_excess_incomplete_days_inconclusive():
    stats = {
        "official": {"trade_count": 600, "trading_days_with_trade": 600,
                     "net_expectancy": 1, "profit_factor": 1.5, "mdd": .05,
                     "positive_folds": 8, "cumulative_net_pnl": 600},
        "stress": {"cumulative_net_pnl": 100},
    }
    defects = {"incomplete_spy_day_ratio": .051, "missing_previous_close_ratio": 0,
               "new_conflicts": 0, "major_defects": 0}
    assert _gate(stats, defects, load_contract())["verdict"] == "INCONCLUSIVE_DATA"
