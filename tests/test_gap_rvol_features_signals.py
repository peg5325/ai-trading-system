from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.history.exchange_calendar import trading_days
from src.research.gap_rvol.contract import load_contract, primary_spec
from src.research.gap_rvol.features import (
    causal_vwap_by_bar,
    compute_day_features,
)
from src.research.gap_rvol.signals import detect_signal
from src.research.orb.features import RvolWindow
from tests.gap_rvol_test_helpers import bar, features, minute_bars


def _seeded_window(day: date) -> RvolWindow:
    prior = trading_days(day - timedelta(days=45), day - timedelta(days=1))[-20:]
    window = RvolWindow(20, 15, 15)
    for prior_day in prior:
        window.push(prior_day, 1500.0)
    return window


def test_gap_rvol_retention_and_vwap_are_hand_calculated():
    day = date(2024, 3, 4)
    previous = date(2024, 3, 1)
    today = minute_bars(day, price=102.0, volume=100.0)
    prev = minute_bars(previous, price=100.0, volume=100.0)
    decision = tuple(
        bar(day, minute, o=102, h=103, low=101, c=102, v=500)
        for minute in range(570, 960, 5)
    )
    result = compute_day_features(
        symbol="AAPL",
        day=day,
        previous_day=previous,
        previous_regular_1m=prev,
        today_regular_1m=today,
        decision_bars=decision,
        rvol_window=_seeded_window(day),
        spec=primary_spec(load_contract()),
        rvol_minutes=15,
        decision_minutes=5,
    )
    assert result.excluded_reasons == ()
    assert result.gap == pytest.approx(0.02)
    assert result.rvol == pytest.approx(1.0)
    assert result.retention_floor == pytest.approx(101.0)
    assert result.vwap_by_minute[570] == pytest.approx((103 + 101 + 102) / 3)


def test_causal_vwap_does_not_see_future_minutes():
    day = date(2024, 3, 4)
    minutes = list(minute_bars(day, count=10, price=100.0, volume=1.0))
    first_decision = (bar(day, 570),)
    before, reasons = causal_vwap_by_bar(
        tuple(minutes), first_decision,
        symbol="AAPL", day=day, bar_minutes=5)
    minutes[9] = bar(
        day, 579, interval="1m", o=10000, h=10000, low=10000, c=10000, v=10000)
    after, reasons_after = causal_vwap_by_bar(
        tuple(minutes), first_decision,
        symbol="AAPL", day=day, bar_minutes=5)
    assert reasons == reasons_after == ()
    assert before == after


def test_pullback_arms_then_next_bar_strictly_reclaims_and_fills_next_bar():
    day = date(2024, 3, 4)
    bars = (
        bar(day, 570, o=100, h=101, low=99, c=100),
        bar(day, 575, o=100, h=102, low=99, c=101),
        bar(day, 580, o=101, h=103, low=100, c=102),
        bar(day, 585, o=102, h=103, low=99, c=101),  # arm
        bar(day, 590, o=101, h=105, low=100, c=104),  # trigger
    )
    scan = detect_signal(
        features=features(day, floor=99.0, vwap=100.0),
        bars=bars,
        spec=primary_spec(load_contract()),
        decision_minutes=5,
        observation_start_minute=585,
        latest_signal_minute=625,
        latest_fill_minute=630,
    )
    assert scan.signal is not None
    assert (scan.signal.signal_minute, scan.signal.fill_minute) == (590, 595)
    assert scan.signal.pullback_low == 99


def test_armed_bar_cannot_trigger_and_reclaim_is_strict():
    day = date(2024, 3, 4)
    same_bar = (
        bar(day, 580, o=100, h=103, low=99, c=102),
        bar(day, 585, o=100, h=104, low=98, c=101),
    )
    scan = detect_signal(
        features=features(day, floor=98, vwap=99),
        bars=same_bar,
        spec=primary_spec(load_contract()),
        decision_minutes=5,
        observation_start_minute=585,
        latest_signal_minute=625,
        latest_fill_minute=630,
    )
    assert scan.signal is None
    equality = same_bar + (
        bar(day, 590, o=100, h=103, low=99, c=103),  # == previous high
    )
    scan = detect_signal(
        features=features(day, floor=98, vwap=99),
        bars=equality,
        spec=primary_spec(load_contract()),
        decision_minutes=5,
        observation_start_minute=585,
        latest_signal_minute=625,
        latest_fill_minute=630,
    )
    assert scan.signal is None


def test_retention_equality_holds_but_break_invalidates_day():
    day = date(2024, 3, 4)
    equal_then_trigger = (
        bar(day, 580, c=101, h=102),
        bar(day, 585, o=100, h=101, low=99, c=100),
        bar(day, 590, o=100, h=103, low=99, c=102),
    )
    assert detect_signal(
        features=features(day, floor=100, vwap=99),
        bars=equal_then_trigger,
        spec=primary_spec(load_contract()),
        decision_minutes=5,
        observation_start_minute=585,
        latest_signal_minute=625,
        latest_fill_minute=630,
    ).signal is not None
    broken = list(equal_then_trigger)
    broken[1] = bar(day, 585, o=100, h=101, low=98, c=99.99)
    scan = detect_signal(
        features=features(day, floor=100, vwap=99),
        bars=broken,
        spec=primary_spec(load_contract()),
        decision_minutes=5,
        observation_start_minute=585,
        latest_signal_minute=625,
        latest_fill_minute=630,
    )
    assert scan.filter_reason == "retention_broken"
