from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

import src.research.orb.simulator as simulator_module
from src.research.orb.contract import load_contract, primary_spec
from src.research.orb.simulator import (
    FeaturePass,
    build_development_feature_pass,
    detect_data_defects,
    simulate_all_specs,
    simulate_day,
)
from src.research.orb.windows import development_data_days
from tests.orb_test_helpers import bar, features


def _flat_bars(day, *, symbol="AAPL", end=955):
    result = []
    for minute in range(585, end + 1, 5):
        if minute == 585:
            result.append(bar(day, minute, symbol=symbol, o=100, h=102, low=99, c=101))
        else:
            result.append(bar(day, minute, symbol=symbol, o=101, h=101.5, low=100, c=101))
    return result


def _no_raw_defects(*symbols):
    return {symbol: () for symbol in symbols}


def test_next_bar_fill_and_entry_bar_is_scanned_for_stop():
    day = date(2024, 3, 4)
    bars = [
        bar(day, 585, o=100, h=102, low=99, c=101),
        bar(day, 590, o=101, h=102, low=94, c=100),
    ]
    result = simulate_day(
        day=day, spec=primary_spec(load_contract()),
        decision_bars_by_symbol={"AAPL": bars},
        features_by_symbol={"AAPL": features(day, or_low=95)},
        raw_defects_by_symbol=_no_raw_defects("AAPL"),
        contract=load_contract())
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.position.entry_minute == 590
    assert trade.exit and trade.exit.exit_minute == 590
    assert trade.exit.exit_reason == "stop"
    assert result.open_positions_at_close == 0


def test_missing_immediate_next_bar_expires_signal_instead_of_late_fill():
    day = date(2024, 3, 4)
    result = simulate_day(
        day=day, spec=primary_spec(load_contract()),
        decision_bars_by_symbol={"AAPL": [
            bar(day, 585, c=101),
            bar(day, 595, o=120, h=121, low=119, c=120),
        ]},
        features_by_symbol={"AAPL": features(day)},
        raw_defects_by_symbol=_no_raw_defects("AAPL"),
        contract=load_contract(),
    )
    assert not result.trades
    assert [(entry.fill_minute, entry.reason) for entry in result.skipped] == [
        (590, "signal_expired")
    ]


def test_eod_normal_day_and_early_close_use_exchange_calendar():
    contract = load_contract()
    normal = date(2024, 3, 4)
    normal_result = simulate_day(
        day=normal, spec=primary_spec(contract),
        decision_bars_by_symbol={"AAPL": _flat_bars(normal)},
        features_by_symbol={"AAPL": features(normal)},
        raw_defects_by_symbol=_no_raw_defects("AAPL"), contract=contract)
    assert normal_result.trades[0].exit
    assert (normal_result.trades[0].exit.exit_minute,
            normal_result.trades[0].exit.exit_reason) == (955, "eod")

    early = date(2024, 7, 3)
    early_result = simulate_day(
        day=early, spec=primary_spec(contract),
        decision_bars_by_symbol={"AAPL": _flat_bars(early, end=775)},
        features_by_symbol={"AAPL": features(early)},
        raw_defects_by_symbol=_no_raw_defects("AAPL"), contract=contract)
    assert early_result.trades[0].exit
    assert (early_result.trades[0].exit.exit_minute,
            early_result.trades[0].exit.exit_reason) == (775, "eod")


def test_missing_eod_decision_bar_cannot_turn_fill_bar_into_normal_eod():
    day = date(2024, 3, 4)
    bars = [
        bar(day, minute, c=101)
        for minute in range(585, 950, 5)
    ]
    bars.append(bar(day, 955, o=110, h=111, low=109, c=110))

    result = simulate_day(
        day=day, spec=primary_spec(load_contract()),
        decision_bars_by_symbol={"AAPL": bars},
        features_by_symbol={"AAPL": features(day, or_low=50)},
        raw_defects_by_symbol=_no_raw_defects("AAPL"),
        contract=load_contract(),
    )

    trade = result.trades[0]
    assert trade.exit
    assert (
        trade.exit.exit_minute,
        trade.exit.exit_price,
        trade.exit.exit_reason,
        trade.operational_reason,
    ) == (955, 110, "eod_fallback", "eod_fallback")


def test_three_missing_expected_bars_force_stale_exit_at_next_open():
    day = date(2024, 3, 4)
    bars = [
        bar(day, 585, c=101),
        bar(day, 590, o=101, h=101.5, low=100, c=101),
        bar(day, 610, o=99, h=100, low=98, c=99),
    ]
    result = simulate_day(
        day=day, spec=primary_spec(load_contract()),
        decision_bars_by_symbol={"AAPL": bars},
        features_by_symbol={"AAPL": features(day)},
        raw_defects_by_symbol=_no_raw_defects("AAPL"), contract=load_contract())
    trade = result.trades[0]
    assert trade.exit and (trade.exit.exit_minute, trade.exit.exit_price) == (610, 99)
    assert trade.exit.exit_reason == "stale_exit"
    assert trade.operational_reason == "stale_exit"


def test_missing_eod_fill_uses_last_post_entry_close_or_unclosable():
    day = date(2024, 3, 4)
    fallback = simulate_day(
        day=day, spec=primary_spec(load_contract()),
        decision_bars_by_symbol={"AAPL": [
            bar(day, 585, c=101),
            bar(day, 590, o=101, h=101.5, low=100, c=101),
            bar(day, 595, o=101, h=101.5, low=100, c=100.5),
        ]},
        features_by_symbol={"AAPL": features(day)},
        raw_defects_by_symbol=_no_raw_defects("AAPL"), contract=load_contract())
    assert fallback.trades[0].exit
    assert (fallback.trades[0].exit.exit_reason,
            fallback.trades[0].exit.exit_price) == ("eod_fallback", 100.5)

    unclosable = simulate_day(
        day=day, spec=primary_spec(load_contract()),
        decision_bars_by_symbol={"AAPL": [
            bar(day, 585, c=101),
            bar(day, 590, o=101, h=101.5, low=100, c=101),
        ]},
        features_by_symbol={"AAPL": features(day)},
        raw_defects_by_symbol=_no_raw_defects("AAPL"), contract=load_contract())
    assert unclosable.trades[0].exit is None
    assert unclosable.trades[0].operational_reason == "unclosable"


def test_identical_duplicates_and_input_shuffle_are_invariant():
    day = date(2024, 3, 4)
    bars = [
        bar(day, 585, c=101),
        bar(day, 590, o=101, h=102, low=94, c=100),
    ]
    kwargs = {
        "day": day, "spec": primary_spec(load_contract()),
        "features_by_symbol": {"AAPL": features(day)},
        "raw_defects_by_symbol": _no_raw_defects("AAPL"),
        "contract": load_contract(),
    }
    a = simulate_day(decision_bars_by_symbol={"AAPL": bars}, **kwargs)
    b = simulate_day(decision_bars_by_symbol={"AAPL": [bars[1], bars[0], bars[0]]}, **kwargs)
    assert a == b


def test_conflicting_duplicate_decision_bar_fails_closed():
    day = date(2024, 3, 4)
    with pytest.raises(ValueError, match="결정봉 값 충돌"):
        simulate_day(
            day=day, spec=primary_spec(load_contract()),
            decision_bars_by_symbol={"AAPL": [
                bar(day, 585, c=101),
                bar(day, 585, c=102),
            ]},
            features_by_symbol={"AAPL": features(day)},
            raw_defects_by_symbol=_no_raw_defects("AAPL"),
            contract=load_contract(),
        )


def test_no_reentry_after_entry_bar_exit():
    day = date(2024, 3, 4)
    bars = [
        bar(day, 585, c=101),
        bar(day, 590, o=101, h=102, low=94, c=101),
        bar(day, 595, o=101, h=102, low=100, c=101),
        bar(day, 600, o=101, h=102, low=94, c=101),
    ]
    result = simulate_day(
        day=day, spec=primary_spec(load_contract()),
        decision_bars_by_symbol={"AAPL": bars},
        features_by_symbol={"AAPL": features(day)},
        raw_defects_by_symbol=_no_raw_defects("AAPL"), contract=load_contract())
    assert len(result.trades) == 1


def test_same_bar_exit_releases_capacity_before_new_entry():
    with open(
        "docs/research/PREREG_P0_US30_ORB_V1.json", encoding="utf-8"
    ) as handle:
        contract = json.load(handle)
    contract["portfolio"]["max_positions"] = 1
    day = date(2024, 3, 4)
    result = simulate_day(
        day=day, spec=primary_spec(contract),
        decision_bars_by_symbol={
            "AAPL": [
                bar(day, 585, symbol="AAPL", c=101),
                bar(day, 590, symbol="AAPL", o=101, h=101.5, low=100, c=101),
                bar(day, 595, symbol="AAPL", o=113, h=114, low=112, c=113),
            ],
            "MSFT": [
                bar(day, 585, symbol="MSFT", low=98, c=99),
                bar(day, 590, symbol="MSFT", o=100, h=101, low=99, c=101),
                bar(day, 595, symbol="MSFT", o=101, h=101.5, low=100, c=101),
                bar(day, 600, symbol="MSFT", o=101, h=101.5, low=100, c=101),
            ],
        },
        features_by_symbol={
            "AAPL": features(day, symbol="AAPL"),
            "MSFT": features(day, symbol="MSFT"),
        },
        raw_defects_by_symbol=_no_raw_defects("AAPL", "MSFT"),
        contract=contract,
    )
    assert [trade.position.symbol for trade in result.trades] == ["AAPL", "MSFT"]
    assert all(entry.reason != "slot_cap" for entry in result.skipped)
    assert result.fill_order == ("exits", "entries")


def test_feature_pass_calls_every_declared_day_symbol_and_never_oos():
    with open("docs/research/PREREG_P0_US30_ORB_V1.json", encoding="utf-8") as handle:
        contract = json.load(handle)
    contract["universe"]["market_etf"] = ["SPY"]
    contract["universe"]["sector_etf"] = []
    contract["universe"]["large_cap"] = ["AAPL"]
    contract["universe"]["leveraged_inverse"] = []
    calls = []

    def spy(_db, symbol, day):
        calls.append((symbol, day))
        return ()

    result = build_development_feature_pass(
        contract=contract, db_path="unused.sqlite", loader=spy)
    # 690 declared days x 2 symbols + one previous-day warmup load per symbol.
    assert len(calls) == 690 * 2 + 2
    declared = set(result.days)
    for symbol in ("AAPL", "SPY"):
        loaded_declared = {day for sym, day in calls if sym == symbol and day in declared}
        assert loaded_declared == declared
    assert max(day for _, day in calls) <= date(2026, 1, 20)


def test_feature_pass_uses_post_as_frontier_then_discards_raw_candles():
    with open(
        "docs/research/PREREG_P0_US30_ORB_V1.json", encoding="utf-8"
    ) as handle:
        contract = json.load(handle)
    contract["universe"]["market_etf"] = ["SPY"]
    contract["universe"]["sector_etf"] = []
    contract["universe"]["large_cap"] = []
    contract["universe"]["leveraged_inverse"] = []
    first_day = development_data_days(contract)[0]

    def spy(_db, symbol, day):
        if day == first_day:
            return (
                bar(day, 955, symbol=symbol, interval="1m"),
                bar(day, 960, symbol=symbol, interval="1m"),
            )
        return ()

    result = build_development_feature_pass(
        contract=contract, db_path="unused.sqlite", loader=spy)

    cached = result.decision_bars_by_day[first_day]["SPY"]
    assert cached[-1].ts_exchange.hour == 15
    assert cached[-1].ts_exchange.minute == 55
    assert not hasattr(result, "day_candles_by_day")


def test_all_eight_specs_are_simulated_as_independent_result_streams(monkeypatch):
    contract = load_contract()
    day = date(2024, 3, 4)
    candles = tuple(
        bar(day, minute, interval="1m")
        for minute in range(570, 961)
    )
    feature = features(day, or_low=50, gap=.01, rvol=2.0)
    feature_pass = FeaturePass(
        days=(day,),
        symbols=("AAPL",),
        by_opening_range_minutes={
            5: {day: {"AAPL": feature}},
            15: {day: {"AAPL": feature}},
        },
        decision_bars_by_day={
            day: {
                "AAPL": simulator_module.decision_bars(
                    candles,
                    day,
                    spec=simulator_module.decision_bar_spec(contract),
                )
            }
        },
        defects_by_day={day: {"AAPL": ()}},
    )
    monkeypatch.setattr(
        simulator_module, "development_eval_days", lambda _contract: (day,))

    output = simulate_all_specs(contract=contract, feature_pass=feature_pass)

    assert len(output) == 8
    assert set(output) == {
        "primary",
        "rvol_1.3",
        "rvol_1.7",
        "gap_0.002",
        "gap_0.004",
        "target_1.5",
        "target_2.5",
        "sensitivity",
    }
    assert len({id(results) for results in output.values()}) == 8
    assert all(results[0].spec_name == name for name, results in output.items())
    primary_trade = output["primary"][0].trades[0]
    assert primary_trade.exit
    assert (
        primary_trade.exit.exit_minute,
        primary_trade.exit.exit_reason,
    ) == (955, "eod")

    defective = replace(
        feature_pass,
        defects_by_day={
            day: {"AAPL": ("unexplained_1m_price_jump_gt_50pct",)}
        },
    )
    defective_output = simulate_all_specs(
        contract=contract, feature_pass=defective)
    assert all(not results[0].trades for results in defective_output.values())
    assert defective_output["primary"][0].major_defects == (
        ("AAPL", "unexplained_1m_price_jump_gt_50pct"),
    )


def test_raw_bar_defect_scan_stays_within_same_regular_day():
    day = date(2024, 3, 4)
    candles = [
        bar(day, 570, interval="1m", o=100, h=101, low=99, c=100),
        bar(day, 571, interval="1m", o=170, h=171, low=169, c=170),
    ]
    assert detect_data_defects(candles) == ("unexplained_1m_price_jump_gt_50pct",)


def test_post_or_after_early_close_decision_bar_is_rejected():
    day = date(2024, 7, 3)
    with pytest.raises(ValueError, match="REGULAR 마감 전"):
        simulate_day(
            day=day, spec=primary_spec(load_contract()),
            decision_bars_by_symbol={
                "AAPL": [bar(day, 1190, interval="5m", c=101)]
            },
            features_by_symbol={"AAPL": features(day)},
            raw_defects_by_symbol=_no_raw_defects("AAPL"),
            contract=load_contract(),
        )


def test_feature_exclusion_reasons_are_preserved_for_defect_aggregation():
    day = date(2024, 3, 4)
    result = simulate_day(
        day=day, spec=primary_spec(load_contract()),
        decision_bars_by_symbol={"AAPL": [bar(day, 585, c=101)]},
        features_by_symbol={
            "AAPL": features(
                day,
                excluded=(
                    "incomplete_opening_range",
                    "insufficient_rvol_lookback",
                ),
            )
        },
        raw_defects_by_symbol=_no_raw_defects("AAPL"),
        contract=load_contract(),
    )
    assert result.excluded == (
        ("AAPL", "incomplete_opening_range"),
        ("AAPL", "insufficient_rvol_lookback"),
    )
    assert not result.trades


def test_unknown_session_is_a_major_data_defect():
    day = date(2024, 3, 4)
    invalid = replace(bar(day, 585, interval="1m"), session="UNKNOWN")
    assert detect_data_defects([invalid]) == ("session_label_violation",)


@pytest.mark.parametrize(
    "reason",
    [
        "ohlc_invariant_violation",
        "session_label_violation",
        "unexplained_1m_price_jump_gt_50pct",
    ],
)
def test_raw_major_defect_excludes_symbol_day_and_survives_in_result(reason):
    day = date(2024, 3, 4)
    result = simulate_day(
        day=day,
        spec=primary_spec(load_contract()),
        decision_bars_by_symbol={"AAPL": _flat_bars(day)},
        features_by_symbol={"AAPL": features(day)},
        raw_defects_by_symbol={"AAPL": (reason,)},
        contract=load_contract(),
    )
    assert not result.trades
    assert result.major_defects == (("AAPL", reason),)
    assert ("AAPL", reason) in result.excluded


def test_raw_defect_evidence_cannot_omit_a_feature_symbol():
    day = date(2024, 3, 4)
    with pytest.raises(ValueError, match="정확히 같아야"):
        simulate_day(
            day=day,
            spec=primary_spec(load_contract()),
            decision_bars_by_symbol={"AAPL": _flat_bars(day)},
            features_by_symbol={"AAPL": features(day)},
            raw_defects_by_symbol={},
            contract=load_contract(),
        )
