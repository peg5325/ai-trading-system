from datetime import date

from src.research.orb.contract import load_contract, neighbor_specs, primary_spec
from src.research.orb.portfolio import (
    SKIPPED_REASONS,
    allocate_entries,
    size_position,
)
from src.research.orb.signals import detect_breakout
from tests.orb_test_helpers import bar, features


def _signal(day, symbol, *, rvol=2, gap=.01, or_low=95):
    return detect_breakout(
        features(day, symbol=symbol, rvol=rvol, gap=gap, or_low=or_low),
        bar(day, 585, symbol=symbol, c=101),
        primary_spec(load_contract()),
        decision_bar_minutes=5,
    )


def test_skip_vocabulary_is_exactly_five():
    assert SKIPPED_REASONS == (
        "one_r_not_positive", "qty_lt_1", "family_cap", "slot_cap",
        "signal_expired")


def test_sizing_caps_instead_of_blocking_and_reports_binding_cap():
    sizing = size_position(
        entry_price=100, stop=99, symbol="AAPL", current_effective=49_900,
        contract=load_contract())
    assert sizing is not None
    assert sizing.binding_cap == "portfolio"
    assert sizing.qty == 1


def test_qty_below_one_is_the_only_capacity_skip():
    day = date(2024, 3, 4)
    sig = _signal(day, "AAPL", or_low=1)
    assert sig
    accepted, skipped = allocate_entries(
        signals=[sig],
        bars_by_symbol={"AAPL": bar(day, 590, o=1_000_000)},
        positions={},
        traded_symbols=set(),
        contract=load_contract(),
        fill_minute=590,
        close_minute=960,
    )
    assert not accepted
    assert skipped[0].reason == "qty_lt_1"
    assert skipped[0].binding_cap in {"risk", "notional", "effective", "portfolio"}


def test_same_bar_exposure_includes_already_accepted_and_never_exceeds_fifty_pct():
    day = date(2024, 3, 4)
    signals = [_signal(day, "AAPL"), _signal(day, "MSFT")]
    assert all(signals)
    bars = {
        symbol: bar(day, 590, symbol=symbol, o=100, h=101, low=99, c=100)
        for symbol in ("AAPL", "MSFT")
    }
    accepted, skipped = allocate_entries(
        signals=[s for s in signals if s],
        bars_by_symbol=bars,
        positions={},
        traded_symbols=set(),
        contract=load_contract(),
        fill_minute=590,
        close_minute=960,
    )
    assert not skipped
    assert len(accepted) == 2
    assert sum(p.effective_exposure for p in accepted) <= 50_000


def test_family_cap_includes_same_bar_accepted_candidate():
    day = date(2024, 3, 4)
    signals = [_signal(day, "TQQQ", rvol=3), _signal(day, "QQQ", rvol=2)]
    bars = {
        symbol: bar(day, 590, symbol=symbol, o=100, h=101, low=99, c=100)
        for symbol in ("TQQQ", "QQQ")
    }
    accepted, skipped = allocate_entries(
        signals=[s for s in signals if s], bars_by_symbol=bars, positions={},
        traded_symbols=set(), contract=load_contract(), fill_minute=590,
        close_minute=960)
    assert [p.symbol for p in accepted] == ["TQQQ"]
    assert [(s.symbol, s.reason) for s in skipped] == [("QQQ", "family_cap")]
    assert accepted[0].leverage == 3


def test_max_entry_boundary_is_inclusive_then_expires():
    day = date(2024, 3, 4)
    sig = _signal(day, "AAPL")
    assert sig
    # synthesize the pending signal's expected fill minute at each boundary
    at = sig.__class__(**{**sig.__dict__, "fill_minute": 870})
    after = sig.__class__(**{**sig.__dict__, "fill_minute": 875})
    bars = {"AAPL": bar(day, 870, o=100)}
    accepted, skipped = allocate_entries(
        signals=[at], bars_by_symbol=bars, positions={}, traded_symbols=set(),
        contract=load_contract(), fill_minute=870, close_minute=960)
    assert accepted and not skipped
    accepted2, skipped2 = allocate_entries(
        signals=[after], bars_by_symbol={"AAPL": bar(day, 875, o=100)},
        positions={}, traded_symbols=set(), contract=load_contract(),
        fill_minute=875, close_minute=960)
    assert not accepted2 and skipped2[0].reason == "signal_expired"


def test_early_close_max_entry_boundary_is_inclusive_then_expires():
    day = date(2024, 7, 3)
    sig = _signal(day, "AAPL")
    assert sig
    at = sig.__class__(**{**sig.__dict__, "fill_minute": 690})
    after = sig.__class__(**{**sig.__dict__, "fill_minute": 695})
    accepted, skipped = allocate_entries(
        signals=[at], bars_by_symbol={"AAPL": bar(day, 690, o=100)},
        positions={}, traded_symbols=set(), contract=load_contract(),
        fill_minute=690, close_minute=780)
    assert accepted and not skipped
    accepted2, skipped2 = allocate_entries(
        signals=[after], bars_by_symbol={"AAPL": bar(day, 695, o=100)},
        positions={}, traded_symbols=set(), contract=load_contract(),
        fill_minute=695, close_minute=780)
    assert not accepted2 and skipped2[0].reason == "signal_expired"


def test_three_same_bar_candidates_fill_two_slots_in_rank_order():
    day = date(2024, 3, 4)
    signals = [
        _signal(day, "MSFT", rvol=2.0, gap=.01),
        _signal(day, "NVDA", rvol=3.0, gap=.01),
        _signal(day, "AAPL", rvol=2.5, gap=.01),
    ]
    bars = {
        symbol: bar(day, 590, symbol=symbol, o=100, h=101, low=99, c=100)
        for symbol in ("AAPL", "MSFT", "NVDA")
    }
    accepted, skipped = allocate_entries(
        signals=[signal for signal in reversed(signals) if signal],
        bars_by_symbol=bars, positions={}, traded_symbols=set(),
        contract=load_contract(), fill_minute=590, close_minute=960)
    assert [position.symbol for position in accepted] == ["NVDA", "AAPL"]
    assert [(entry.symbol, entry.reason) for entry in skipped] == [
        ("MSFT", "slot_cap")
    ]


def test_target_neighbor_multiplier_is_carried_into_position():
    day = date(2024, 3, 4)
    spec = next(spec for spec in neighbor_specs(load_contract())
                if spec.name == "target_1.5")
    signal = detect_breakout(
        features(day), bar(day, 585, c=101), spec, decision_bar_minutes=5)
    assert signal
    accepted, _ = allocate_entries(
        signals=[signal], bars_by_symbol={"AAPL": bar(day, 590, o=101)},
        positions={}, traded_symbols=set(), contract=load_contract(),
        fill_minute=590, close_minute=960)
    assert accepted[0].target == 101 + 1.5 * (101 - 95)


def test_inverse_etf_remains_a_long_position_with_three_x_exposure():
    day = date(2024, 3, 4)
    signal = _signal(day, "SQQQ")
    assert signal
    accepted, skipped = allocate_entries(
        signals=[signal],
        bars_by_symbol={"SQQQ": bar(day, 590, symbol="SQQQ", o=100)},
        positions={}, traded_symbols=set(), contract=load_contract(),
        fill_minute=590, close_minute=960)
    assert not skipped
    assert accepted[0].symbol == "SQQQ"
    assert accepted[0].qty > 0
    assert accepted[0].leverage == 3
