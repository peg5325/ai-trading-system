from __future__ import annotations

from datetime import date

from src.research.gap_rvol.contract import load_contract
from src.research.gap_rvol.execution import evaluate_exit
from src.research.gap_rvol.portfolio import Position, allocate_entries
from src.research.gap_rvol.signals import GapRvolSignal
from tests.gap_rvol_test_helpers import bar


def _signal(day: date, symbol: str, *, rvol: float, gap: float) -> GapRvolSignal:
    return GapRvolSignal(
        symbol=symbol,
        day=day,
        spec_name="primary",
        signal_minute=590,
        fill_minute=595,
        pullback_low=99,
        rvol=rvol,
        gap=gap,
        target_r_multiple=2,
    )


def test_simultaneous_candidates_rank_rvol_then_gap_then_symbol_and_family_cap():
    day = date(2024, 3, 4)
    signals = (
        _signal(day, "TQQQ", rvol=3, gap=.01),
        _signal(day, "QQQ", rvol=2, gap=.02),
        _signal(day, "AAPL", rvol=2, gap=.01),
    )
    bars = {
        symbol: bar(day, 595, symbol=symbol, o=100)
        for symbol in ("TQQQ", "QQQ", "AAPL")
    }
    accepted, skipped = allocate_entries(
        signals=signals,
        bars_by_symbol=bars,
        positions={},
        traded_symbols=set(),
        contract=load_contract(),
        fill_minute=595,
    )
    assert [(item.symbol, item.rank) for item in accepted] == [
        ("TQQQ", 1), ("AAPL", 3)
    ]
    assert [(item.symbol, item.reason) for item in skipped] == [("QQQ", "family_cap")]


def test_pullback_low_stop_and_two_r_target_are_used():
    day = date(2024, 3, 4)
    accepted, _ = allocate_entries(
        signals=(_signal(day, "AAPL", rvol=2, gap=.01),),
        bars_by_symbol={"AAPL": bar(day, 595, o=101)},
        positions={},
        traded_symbols=set(),
        contract=load_contract(),
        fill_minute=595,
    )
    position = accepted[0]
    assert (position.stop, position.one_r, position.target) == (99, 2, 105)


def test_exit_priority_is_gap_stop_then_gap_target_then_stale_eod_time():
    day = date(2024, 3, 4)
    position = Position(
        "AAPL", day, "primary", bar(day, 595).ts_utc, 595,
        100, 10, 1, 99, 102, 1, 2, .01, 1, "risk")
    assert evaluate_exit(
        position, bar(day, 655, o=98, h=103, low=97, c=100),
        stale=True, eod=True, time_due=True).exit_reason == "stop"
    assert evaluate_exit(
        position, bar(day, 655, o=103, h=104, low=98, c=100),
        stale=True, eod=True, time_due=True).exit_reason == "target"
    assert evaluate_exit(
        position, bar(day, 655, o=100, h=101, low=99.5, c=100),
        stale=True, eod=True, time_due=True).exit_reason == "stale_exit"
    assert evaluate_exit(
        position, bar(day, 655, o=100, h=101, low=99.5, c=100),
        eod=True, time_due=True).exit_reason == "eod"
    assert evaluate_exit(
        position, bar(day, 655, o=100, h=101, low=99.5, c=100),
        time_due=True).exit_reason == "time"
