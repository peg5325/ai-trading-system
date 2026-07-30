from __future__ import annotations

import dataclasses
import json
import random
from datetime import date
from pathlib import Path

import pytest

from src.research.gap_rvol.contract import load_contract, primary_spec
from src.research.gap_rvol.gates import DefectMetrics, evaluate_development
from src.research.gap_rvol.simulator import detect_data_defects, simulate_day
from src.research.gap_rvol.windows import regular_close_minute_checked
from src.research.orb.stats import PerformanceStats
from tests.gap_rvol_test_helpers import bar, features


def _signal_bars(day: date):
    bars = [
        bar(day, 570, c=100),
        bar(day, 575, h=102, c=101),
        bar(day, 580, h=103, c=102),
        bar(day, 585, o=102, h=103, low=99, c=101),
        bar(day, 590, o=101, h=105, low=100, c=104),
        bar(day, 595, o=103, h=104, low=102, c=103),
    ]
    bars.extend(
        bar(day, minute, o=103, h=104, low=102, c=103)
        for minute in range(600, 661, 5)
    )
    return bars


def test_next_bar_fill_entry_bar_scan_and_scheduled_60_minute_stop():
    day = date(2024, 3, 4)
    result = simulate_day(
        day=day,
        spec=primary_spec(load_contract()),
        decision_bars_by_symbol={"AAPL": _signal_bars(day)},
        features_by_symbol={"AAPL": features(
            day,
            vwap=100,
            minutes=tuple(range(570, 661, 5)),
        )},
        raw_defects_by_symbol={"AAPL": ()},
        contract=load_contract(),
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.position.entry_minute == 595
    assert trade.exit is not None
    assert (trade.exit.exit_minute, trade.exit.exit_reason) == (655, "time")


def test_identical_duplicates_and_input_shuffle_are_invariant():
    day = date(2024, 3, 4)
    bars = _signal_bars(day)
    kwargs = {
        "day": day,
        "spec": primary_spec(load_contract()),
        "features_by_symbol": {"AAPL": features(
            day, minutes=tuple(range(570, 661, 5)))},
        "raw_defects_by_symbol": {"AAPL": ()},
        "contract": load_contract(),
    }
    baseline = simulate_day(
        decision_bars_by_symbol={"AAPL": bars}, **kwargs)
    shuffled = bars + [bars[0]]
    random.Random(7).shuffle(shuffled)
    assert simulate_day(
        decision_bars_by_symbol={"AAPL": shuffled}, **kwargs) == baseline


def test_missing_immediate_fill_expires_instead_of_filling_late():
    day = date(2024, 3, 4)
    bars = [
        item for item in _signal_bars(day)
        if not (item.ts_exchange.hour == 9 and item.ts_exchange.minute == 55)
    ]
    result = simulate_day(
        day=day,
        spec=primary_spec(load_contract()),
        decision_bars_by_symbol={"AAPL": bars},
        features_by_symbol={"AAPL": features(
            day, minutes=tuple(range(570, 661, 5)))},
        raw_defects_by_symbol={"AAPL": ()},
        contract=load_contract(),
    )
    assert not result.trades
    assert [(item.fill_minute, item.reason) for item in result.skipped] == [
        (595, "signal_expired")
    ]


def test_missing_exact_time_bar_exits_at_first_later_scheduled_bar_without_extension():
    day = date(2024, 3, 4)
    bars = [
        item for item in _signal_bars(day)
        if not (item.ts_exchange.hour == 10 and item.ts_exchange.minute == 55)
    ]
    bars.append(bar(day, 660, o=103, h=104, low=102, c=103))
    result = simulate_day(
        day=day,
        spec=primary_spec(load_contract()),
        decision_bars_by_symbol={"AAPL": bars},
        features_by_symbol={"AAPL": features(
            day, minutes=tuple(range(570, 666, 5)))},
        raw_defects_by_symbol={"AAPL": ()},
        contract=load_contract(),
    )
    trade = result.trades[0]
    assert trade.exit is not None
    assert (trade.exit.exit_minute, trade.exit.exit_reason) == (660, "time")
    assert trade.operational_reason is None


def test_early_close_calendar_boundary_is_exchange_derived():
    assert regular_close_minute_checked(date(2024, 7, 3)) == 13 * 60


def test_unknown_major_defect_reason_is_rejected():
    day = date(2024, 3, 4)
    with pytest.raises(ValueError, match="unknown major-defect"):
        simulate_day(
            day=day,
            spec=primary_spec(load_contract()),
            decision_bars_by_symbol={"AAPL": _signal_bars(day)},
            features_by_symbol={"AAPL": features(
                day, minutes=tuple(range(570, 661, 5)))},
            raw_defects_by_symbol={"AAPL": ("unregistered_heuristic",)},
            contract=load_contract(),
        )


def test_major_defects_use_only_frozen_invalidation_classes():
    day = date(2024, 3, 4)
    bad_ohlc = dataclasses.replace(bar(day, 570), low=102, high=101)
    assert detect_data_defects((bad_ohlc,)) == ("data_contract_violation",)
    # A large but structurally valid price jump is not an unstated D9 heuristic.
    jump = (
        bar(day, 570, o=100, h=100, low=100, c=100),
        bar(day, 575, o=200, h=200, low=200, c=200),
    )
    assert detect_data_defects(jump) == ()


def test_same_bar_exit_releases_capacity_before_new_entry():
    day = date(2024, 3, 4)
    aapl = _signal_bars(day)
    aapl = [
        dataclasses.replace(item, low=98)
        if item.ts_exchange.hour == 10 and item.ts_exchange.minute == 0
        else item
        for item in aapl
    ]
    msft = [
        bar(day, 570, symbol="MSFT", c=100),
        bar(day, 575, symbol="MSFT", h=102, c=101),
        bar(day, 580, symbol="MSFT", h=103, c=102),
        bar(day, 585, symbol="MSFT", h=104, c=103),
        bar(day, 590, symbol="MSFT", o=102, h=103, low=99, c=101),
        bar(day, 595, symbol="MSFT", o=101, h=105, low=100, c=104),
        bar(day, 600, symbol="MSFT", o=103, h=104, low=102, c=103),
    ]
    contract = json.loads(Path(
        "docs/research/PREREG_P0_US30_GAP_RVOL_V1.json"
    ).read_text(encoding="utf-8"))
    contract["portfolio"]["max_positions"] = 1
    result = simulate_day(
        day=day,
        spec=primary_spec(contract),
        decision_bars_by_symbol={"AAPL": aapl, "MSFT": msft},
        features_by_symbol={
            "AAPL": features(day, minutes=tuple(range(570, 661, 5))),
            "MSFT": features(
                day, symbol="MSFT", minutes=tuple(range(570, 606, 5))),
        },
        raw_defects_by_symbol={"AAPL": (), "MSFT": ()},
        contract=contract,
    )
    assert [trade.position.symbol for trade in result.trades] == ["AAPL", "MSFT"]
    assert result.trades[0].exit is not None
    assert result.trades[0].exit.exit_minute == 600


def _stats(**changes) -> PerformanceStats:
    base = PerformanceStats(
        cost_level="official",
        trade_count=600,
        entered_trade_count=600,
        trading_days_with_trade=400,
        cumulative_net_pnl=1000,
        net_expectancy=2,
        net_expectancy_per_r=.1,
        profit_factor=1.4,
        mdd=.05,
        equity_curve=(100000, 101000),
        fold_pnl={f"F{i:02}": 1 for i in range(1, 11)},
        positive_folds=10,
        family_profit_share={},
        day_profit_share={},
    )
    return dataclasses.replace(base, **changes)


def test_d1_to_d8_failures_are_no_selection_but_d9_is_inconclusive():
    contract = load_contract()
    clean = DefectMetrics(0, 20100, 0, 600, 0)
    economic = evaluate_development(
        primary_official=_stats(profit_factor=1.1),
        primary_stress=_stats(),
        neighbor_official=(),
        defects=clean,
        contract=contract,
    )
    assert economic.verdict == "NO_SELECTION"
    assert economic.failed == ("D4",)

    defective = evaluate_development(
        primary_official=_stats(),
        primary_stress=_stats(),
        neighbor_official=(),
        defects=DefectMetrics(1006, 20100, 0, 600, 0),
        contract=contract,
    )
    assert defective.verdict == "INCONCLUSIVE_DATA"
    assert defective.failed == ("D9",)


def test_unmet_preconditions_are_not_run():
    decision = evaluate_development(
        primary_official=_stats(),
        primary_stress=_stats(),
        neighbor_official=(),
        defects=DefectMetrics(0, 1, 0, 1, 0),
        contract=load_contract(),
        preconditions_ok=False,
    )
    assert (decision.verdict, decision.failed) == ("NOT_RUN", ("P1_P5",))
