from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from src.models import ET, Candle
from src.research.gap_down_fade.contract import (
    evaluation_days,
    fold_boundaries,
    load_contract,
)
from src.research.gap_down_fade.development import (
    Candidate,
    GrossTrade,
    _allocate,
    _completion_minute,
    _gate,
    _net,
    _stats,
    close_candidate,
    detect_candidate,
)
from src.research.gap_down_fade.indicators import (
    cumulative_vwap,
    exact_median,
    wilder_atr,
    wilder_rsi,
)
from src.research.gap_down_fade.loading import complete_regular_5m


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
        low=low, close=close, volume=volume, currency="USD", adjusted=True, market="US",
    )


def recovery_bars() -> list[Candle]:
    start = datetime(2025, 6, 3, 9, 30, tzinfo=ET)
    bars: list[Candle] = []
    opening_closes = [99.7, 99.4, 99.1, 98.8, 98.6, 98.5]
    previous = 100.0
    for index, close in enumerate(opening_closes):
        bars.append(bar5(
            start + timedelta(minutes=5 * index), open_=previous,
            high=max(previous, close) + .05, low=close - .15, close=close, volume=200,
        ))
        previous = close
    for index in range(6, 20):
        close = 98.45 + (index % 2) * .03
        bars.append(bar5(
            start + timedelta(minutes=5 * index), open_=previous,
            high=max(previous, close) + .05, low=98.35 + (index % 3) * .02,
            close=close, volume=100,
        ))
        previous = close
    bars.append(bar5(
        start + timedelta(minutes=100), open_=98.5, high=99.5, low=98.7,
        close=99.4, volume=180,
    ))
    for index in range(21, 78):
        bars.append(bar5(
            start + timedelta(minutes=5 * index), open_=99.4, high=99.7,
            low=99.2, close=99.5, volume=100,
        ))
    return bars


def test_contract_is_frozen_and_blind_oos_is_excluded():
    contract = load_contract()
    days = evaluation_days(contract)
    assert days[0] == date(2023, 5, 18)
    assert days[-1] == date(2026, 1, 20)
    assert date(2026, 1, 21) not in days
    assert len(fold_boundaries(contract)) == 10
    assert contract["discipline"]["external_provider_used"] is False


def test_indicator_conventions_are_deterministic():
    assert exact_median([4, 1, 3, 2]) == 2.5
    assert cumulative_vwap([2, 4], [0, 2], [1, 3], [1, 3]) == pytest.approx((1, 2.5))
    assert wilder_atr([2, 3, 4], [0, 1, 2], [1, 2, 3], 3) == (None, None, 2.0)
    rsi = wilder_rsi([6, 5, 4, 3, 2, 1, 2], 5)
    assert rsi[5] == 0.0
    assert rsi[6] == pytest.approx(20.0)


def test_complete_regular_session_resamples_to_78_bars():
    day = date(2025, 6, 3)
    start = datetime(2025, 6, 3, 9, 30, tzinfo=ET)
    candles = tuple(candle(start + timedelta(minutes=index)) for index in range(390))
    bars, defects = complete_regular_5m(candles, day, "AAPL")
    assert defects == ()
    assert len(bars) == 78


def test_missing_minute_excludes_symbol_day():
    day = date(2025, 6, 3)
    start = datetime(2025, 6, 3, 9, 30, tzinfo=ET)
    candles = tuple(candle(start + timedelta(minutes=index)) for index in range(390) if index != 10)
    bars, defects = complete_regular_5m(candles, day, "AAPL")
    assert bars == ()
    assert defects == ("incomplete_regular_session",)


def test_primary_signal_uses_previous_daily_close_and_next_bar_open():
    bars = recovery_bars()
    candidate, reasons = detect_candidate(
        "AAPL", date(2025, 6, 3), bars, previous_close=102.0,
        baseline_opening_volume=600.0, contract=load_contract(),
    )
    assert reasons == {}
    assert candidate is not None
    assert candidate.entry_index == 21
    assert candidate.entry_price == bars[21].open
    assert candidate.gap_return == pytest.approx(100 / 102 - 1)


def test_signal_window_uses_completed_bar_time():
    eligibility = load_contract()["eligibility"]
    first = bar5(
        datetime(2025, 6, 3, 9, 55, tzinfo=ET),
        open_=100,
        high=101,
        low=99,
        close=100,
    )
    last = bar5(
        datetime(2025, 6, 3, 12, 55, tzinfo=ET),
        open_=100,
        high=101,
        low=99,
        close=100,
    )
    after = bar5(
        datetime(2025, 6, 3, 13, 0, tzinfo=ET),
        open_=100,
        high=101,
        low=99,
        close=100,
    )

    assert _completion_minute(first) == int(eligibility["signal_start_minute_et"])
    assert _completion_minute(last) == int(eligibility["signal_end_minute_et"])
    assert _completion_minute(after) > int(eligibility["signal_end_minute_et"])


def test_gap_threshold_rejects_small_gap():
    candidate, reasons = detect_candidate(
        "AAPL", date(2025, 6, 3), recovery_bars(), previous_close=100.5,
        baseline_opening_volume=600.0, contract=load_contract(),
    )
    assert candidate is None
    assert reasons == {"gap_too_small": 1}


def test_close_candidate_is_stop_first_on_collision():
    start = datetime(2025, 6, 3, 13, 0, tzinfo=ET)
    bars = [bar5(start, open_=100, high=102, low=98, close=101)]
    candidate = Candidate(
        symbol="AAPL", day=date(2025, 6, 3), entry_index=0,
        signal_ts=bars[0].ts_utc, entry_ts=bars[0].ts_utc, entry_price=100,
        stop=99, target=101, gap_return=-.02, opening_volume_multiple=1.5,
    )
    exit_ts, price, reason = close_candidate(candidate, bars)
    assert exit_ts == bars[0].ts_utc + timedelta(minutes=5)
    assert price == 99
    assert reason == "stop_first"


def test_close_candidate_gap_through_exits_at_bar_open():
    start = datetime(2025, 6, 3, 13, 0, tzinfo=ET)
    bars = [bar5(start, open_=98, high=100, low=97, close=99)]
    candidate = Candidate(
        symbol="AAPL",
        day=date(2025, 6, 3),
        entry_index=0,
        signal_ts=bars[0].ts_utc - timedelta(minutes=5),
        entry_ts=bars[0].ts_utc,
        entry_price=100,
        stop=99,
        target=102,
        gap_return=-0.02,
        opening_volume_multiple=1.5,
    )
    exit_ts, price, reason = close_candidate(candidate, bars)
    assert exit_ts == bars[0].ts_utc
    assert price == 98
    assert reason == "stop_gap"


def test_gate_rejects_bad_economics_even_when_data_passes():
    stats = {
        "official": {"trade_count": 400, "trading_days_with_trade": 250,
                     "net_expectancy": -1, "profit_factor": .8, "mdd": .05,
                     "positive_folds": 4, "cumulative_net_pnl": -400},
        "stress": {"cumulative_net_pnl": -800},
    }
    defects = {"incomplete_symbol_day_ratio": .04, "missing_previous_close_ratio": 0,
               "new_conflicts": 0, "major_defects": 0}
    gate = _gate(stats, defects, load_contract())
    assert gate["verdict"] == "NO_SELECTION"
    assert gate["items"]["D9"] is True


def test_gate_marks_missing_previous_close_inconclusive():
    stats = {
        "official": {"trade_count": 400, "trading_days_with_trade": 250,
                     "net_expectancy": 1, "profit_factor": 1.5, "mdd": .05,
                     "positive_folds": 8, "cumulative_net_pnl": 400},
        "stress": {"cumulative_net_pnl": 100},
    }
    defects = {"incomplete_symbol_day_ratio": .04, "missing_previous_close_ratio": .0001,
               "new_conflicts": 0, "major_defects": 0}
    assert _gate(stats, defects, load_contract())["verdict"] == "INCONCLUSIVE_DATA"


def _trade(symbol: str, day: date, *, entry: float, exit_: float, qty: int = 10) -> GrossTrade:
    entry_ts = datetime(day.year, day.month, day.day, 13, 0, tzinfo=ET)
    return GrossTrade(
        symbol=symbol,
        day=day,
        signal_ts=entry_ts - timedelta(minutes=5),
        entry_ts=entry_ts,
        exit_ts=entry_ts + timedelta(minutes=5),
        entry_price=entry,
        exit_price=exit_,
        qty=qty,
        exit_reason="target" if exit_ > entry else "stop",
        gap_return=-0.02,
        opening_volume_multiple=1.5,
    )


def test_net_cost_and_fold_attribution_are_locked():
    day1 = date(2025, 6, 2)
    day2 = date(2025, 6, 3)
    win = _trade("AAPL", day1, entry=100, exit_=101)
    loss = _trade("MSFT", day2, entry=100, exit_=99)
    assert _net(win, 0.34) == pytest.approx(10 - (100 + 101) * 10 * 0.0034 / 2)
    stats = _stats(
        (win, loss),
        (day1, day2),
        (("F01", day1, day1), ("F02", day2, day2)),
        0.34,
        100_000,
    )
    assert stats["fold_pnl"]["F01"] > 0
    assert stats["fold_pnl"]["F02"] < 0
    assert stats["positive_folds"] == 1


def test_allocate_enforces_exposure_family_at_same_entry_time():
    contract = load_contract()
    day = date(2025, 6, 3)
    start = datetime(2025, 6, 3, 13, 0, tzinfo=ET)
    bars = [
        bar5(
            start + timedelta(minutes=5 * index),
            open_=100,
            high=100.5,
            low=99.5,
            close=100,
        )
        for index in range(2)
    ]

    def candidate(symbol: str, volume_multiple: float) -> Candidate:
        return Candidate(
            symbol=symbol,
            day=day,
            entry_index=0,
            signal_ts=bars[0].ts_utc - timedelta(minutes=5),
            entry_ts=bars[0].ts_utc,
            entry_price=100,
            stop=98,
            target=103,
            gap_return=-0.02,
            opening_volume_multiple=volume_multiple,
        )

    trades, skipped = _allocate(
        ((candidate("QQQ", 2.0), bars), (candidate("TQQQ", 1.5), bars)),
        contract,
    )
    assert [trade.symbol for trade in trades] == ["QQQ"]
    assert skipped["family_cap"] == 1


def test_allocate_keeps_intrabar_exit_open_for_same_bar_entry():
    contract = load_contract()
    day = date(2025, 6, 3)
    start = datetime(2025, 6, 3, 13, 0, tzinfo=ET)
    first_bars = [
        bar5(start, open_=100, high=100.5, low=99.5, close=100),
        bar5(
            start + timedelta(minutes=5),
            open_=100,
            high=100.5,
            low=97.5,
            close=98,
        ),
    ]
    second_bars = [
        bar5(
            start + timedelta(minutes=5),
            open_=100,
            high=100.5,
            low=99.5,
            close=100,
        )
    ]
    first = Candidate(
        symbol="QQQ",
        day=day,
        entry_index=0,
        signal_ts=first_bars[0].ts_utc - timedelta(minutes=5),
        entry_ts=first_bars[0].ts_utc,
        entry_price=100,
        stop=98,
        target=103,
        gap_return=-0.03,
        opening_volume_multiple=2.0,
    )
    second = Candidate(
        symbol="TQQQ",
        day=day,
        entry_index=0,
        signal_ts=second_bars[0].ts_utc - timedelta(minutes=5),
        entry_ts=second_bars[0].ts_utc,
        entry_price=100,
        stop=98,
        target=103,
        gap_return=-0.02,
        opening_volume_multiple=1.5,
    )
    trades, skipped = _allocate(((first, first_bars), (second, second_bars)), contract)
    assert [trade.symbol for trade in trades] == ["QQQ"]
    assert trades[0].exit_ts == start + timedelta(minutes=10)
    assert skipped["family_cap"] == 1
