from datetime import date

import pytest

from src.research.orb.contract import load_contract
from src.research.orb.execution import close_trade, evaluate_exit
from src.research.orb.portfolio import Position
from tests.orb_test_helpers import bar


def _position(day: date, *, entry=101.0, stop=95.0, target=113.0):
    entry_bar = bar(day, 590, o=entry, h=entry, low=entry, c=entry)
    return Position(
        symbol="AAPL", day=day, spec_name="primary",
        entry_bar_ts_utc=entry_bar.ts_utc, entry_minute=590,
        entry_price=entry, qty=10, leverage=1, stop=stop, target=target,
        one_r=entry-stop, rvol=2, gap=.01, rank=1, binding_cap="risk")


def test_gap_through_stop_and_target_fill_at_open():
    day = date(2024, 3, 4)
    p = _position(day)
    stop = evaluate_exit(p, bar(day, 595, o=94, h=96, low=93, c=95), is_eod_fill=False)
    target = evaluate_exit(p, bar(day, 595, o=114, h=115, low=113, c=114), is_eod_fill=False)
    assert stop and (stop.exit_price, stop.exit_reason) == (94, "stop")
    assert target and (target.exit_price, target.exit_reason) == (114, "target")


def test_same_bar_stop_beats_target():
    day = date(2024, 3, 4)
    fill = evaluate_exit(
        _position(day), bar(day, 595, o=100, h=114, low=94, c=101),
        is_eod_fill=False)
    assert fill and (fill.exit_price, fill.exit_reason) == (95, "stop")


def test_eod_stop_beats_eod_and_target_never_beats_eod():
    day = date(2024, 3, 4)
    p = _position(day)
    stopped = evaluate_exit(
        p, bar(day, 955, o=100, h=114, low=94, c=110), is_eod_fill=True)
    eod = evaluate_exit(
        p, bar(day, 955, o=114, h=115, low=100, c=114), is_eod_fill=True)
    assert stopped and stopped.exit_reason == "stop"
    assert eod and (eod.exit_price, eod.exit_reason) == (114, "eod")


def test_costs_are_three_levels_and_not_double_charged():
    day = date(2024, 3, 4)
    p = _position(day)
    fill = evaluate_exit(
        p, bar(day, 595, o=114, h=115, low=113, c=114), is_eod_fill=False)
    assert fill
    trade = close_trade(load_contract(), p, fill)
    assert set(trade.costs) == {"low", "official", "stress"}
    official = trade.costs["official"]
    assert official.entry_cost == pytest.approx(101 * 10 * .0034 / 2)
    assert official.exit_cost == pytest.approx(114 * 10 * .0034 / 2)
    expected = (114 - 101) * 10 - (101 + 114) * 10 * .0034 / 2
    assert official.net_pnl == pytest.approx(expected)
