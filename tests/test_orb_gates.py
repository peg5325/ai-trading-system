from __future__ import annotations

from datetime import date

import pytest

from src.research.orb.contract import load_contract, universe
from src.research.orb.execution import unclosable_trade
from src.research.orb.gates import (
    DefectMetrics,
    SnapshotDefectEvidence,
    build_defect_metrics,
    disqualified_non_primary_specs,
    evaluate_development,
    evaluate_oos,
    oos_months,
)
from src.research.orb.identity import IdentityCheck, IdentityResult
from src.research.orb.portfolio import Position, SkippedEntry
from src.research.orb.simulator import DaySimulationResult
from src.research.orb.stats import PerformanceStats
from tests.orb_test_helpers import bar


def _stats(**overrides):
    values = {
        "cost_level": "official", "trade_count": 536, "entered_trade_count": 536,
        "trading_days_with_trade": 322, "cumulative_net_pnl": 100,
        "net_expectancy": 1, "net_expectancy_per_r": .1, "profit_factor": 1.2,
        "mdd": .1, "equity_curve": (100_000, 100_100),
        "fold_pnl": {f"F{i:02d}": (1 if i <= 7 else 0) for i in range(1, 11)},
        "positive_folds": 7, "family_profit_share": {"AAPL": .2},
        "day_profit_share": {"2024-01-01": .1},
    }
    values.update(overrides)
    return PerformanceStats(**values)


def _defects(**overrides):
    values = {
        "data_excluded_symbol_days": 100, "total_symbol_days": 20_100,
        "operational_failure_trades": 10, "entered_trades": 536,
        "major_defect_count": 0, "conflict_total": 28,
    }
    values.update(overrides)
    return DefectMetrics(**values)


def _identity(*, status="match"):
    contract = load_contract()
    expected = contract["data_identity"]
    checks = tuple(
        IdentityCheck(name, expected[key], expected[key], status)
        for name, key in (
            ("snapshot_file", "snapshot_file_sha256"),
            ("snapshot_content", "snapshot_content_sha256"),
            ("manifest_data", "manifest_data_sha256"),
            ("raw_content", "raw_content_sha256"),
        )
    )
    return IdentityResult(
        checks=checks,
        ok=status == "match",
        verdict=None if status != "not_run" else "NOT_RUN",
    )


def _snapshot_evidence(*, conflict=28, identity=None):
    contract = load_contract()
    symbols = universe(contract).all_symbols
    return SnapshotDefectEvidence.from_sources(
        manifest={"symbols": [
            {"symbol": symbol, "conflict": conflict if index == 0 else 0}
            for index, symbol in enumerate(symbols)
        ]},
        identity=identity or _identity(),
        contract=contract,
    )


def test_all_development_boundaries_pass_and_open_only_next_stage():
    decision = evaluate_development(
        primary_official=_stats(),
        primary_stress=_stats(cost_level="stress", cumulative_net_pnl=0),
        neighbor_official=[_stats(net_expectancy=1) for _ in range(6)],
        defects=_defects(), contract=load_contract())
    assert decision.development_gate == "PASS"
    assert decision.verdict is None
    assert decision.next_stage == "blind_oos"
    assert not decision.failed


def test_each_core_boundary_flips_when_below_or_above():
    cases = (
        ("D1", {"trade_count": 535}),
        ("D2", {"trading_days_with_trade": 321}),
        ("D4", {"profit_factor": 1.19}),
        ("D5", {"mdd": .101}),
        ("D6", {"positive_folds": 6}),
    )
    for expected, change in cases:
        decision = evaluate_development(
            primary_official=_stats(**change),
            primary_stress=_stats(cost_level="stress", cumulative_net_pnl=0),
            neighbor_official=[_stats() for _ in range(6)],
            defects=_defects(), contract=load_contract())
        assert expected in decision.failed
        assert decision.verdict == "NO_SELECTION"


def test_expectancy_and_stress_boundaries_flip():
    for expected, official, stress in (
        ("D3", _stats(net_expectancy=0), _stats(cost_level="stress")),
        ("D7", _stats(), _stats(cost_level="stress", cumulative_net_pnl=-.01)),
    ):
        decision = evaluate_development(
            primary_official=official,
            primary_stress=stress,
            neighbor_official=[_stats() for _ in range(6)],
            defects=_defects(), contract=load_contract())
        assert expected in decision.failed


def test_zero_trade_neighbor_is_not_opposite_one_passes_and_two_are_cliff():
    one_opposite = [
        _stats(trade_count=0, net_expectancy=-1),
        _stats(net_expectancy=-1),
        *[_stats(net_expectancy=1) for _ in range(4)],
    ]
    passing = evaluate_development(
        primary_official=_stats(),
        primary_stress=_stats(cost_level="stress", cumulative_net_pnl=0),
        neighbor_official=one_opposite, defects=_defects(),
        contract=load_contract())
    assert "D8" not in passing.failed

    neighbors = [
        _stats(trade_count=0, net_expectancy=-1),
        _stats(net_expectancy=-1),
        _stats(net_expectancy=-1),
        *[_stats(net_expectancy=1) for _ in range(3)],
    ]
    decision = evaluate_development(
        primary_official=_stats(),
        primary_stress=_stats(cost_level="stress", cumulative_net_pnl=0),
        neighbor_official=neighbors, defects=_defects(), contract=load_contract())
    assert "D8" in decision.failed


def test_conflict_baseline_twenty_eight_passes_and_twenty_nine_is_inconclusive():
    passing = evaluate_development(
        primary_official=_stats(),
        primary_stress=_stats(cost_level="stress", cumulative_net_pnl=0),
        neighbor_official=[_stats() for _ in range(6)],
        defects=_defects(conflict_total=28), contract=load_contract())
    failing = evaluate_development(
        primary_official=_stats(),
        primary_stress=_stats(cost_level="stress", cumulative_net_pnl=0),
        neighbor_official=[_stats() for _ in range(6)],
        defects=_defects(conflict_total=29), contract=load_contract())
    assert "D9" not in passing.failed
    assert failing.verdict == "INCONCLUSIVE_DATA"
    assert "D9" in failing.failed


def test_defect_ratios_have_distinct_denominators_and_route_inconclusive():
    defects = _defects(
        data_excluded_symbol_days=1006,
        operational_failure_trades=108,
        entered_trades=536)
    assert defects.data_ratio != defects.operational_ratio
    decision = evaluate_development(
        primary_official=_stats(),
        primary_stress=_stats(cost_level="stress", cumulative_net_pnl=0),
        neighbor_official=[_stats() for _ in range(6)],
        defects=defects, contract=load_contract())
    assert decision.verdict == "INCONCLUSIVE_DATA"
    assert "D9" in decision.failed


def test_defect_aggregation_deduplicates_data_reasons_and_ignores_strategy_skip():
    day = date(2024, 3, 4)
    entry_bar = bar(day, 590, o=101, h=101, low=101, c=101)
    position = Position(
        symbol="AAPL", day=day, spec_name="primary",
        entry_bar_ts_utc=entry_bar.ts_utc, entry_minute=590,
        entry_price=101, qty=1, leverage=1, stop=95, target=113,
        one_r=6, rvol=2, gap=.01, rank=1, binding_cap="risk")
    result = DaySimulationResult(
        day=day,
        spec_name="primary",
        trades=(unclosable_trade(position),),
        skipped=(
            SkippedEntry("MSFT", day, 585, 590, "slot_cap"),
        ),
        excluded=(
            ("AAPL", "incomplete_opening_range"),
            ("AAPL", "insufficient_rvol_lookback"),
            ("MSFT", "no_regular_data"),
        ),
        major_defects=(),
        open_positions_at_close=0,
    )

    defects = build_defect_metrics(
        day_results=[result], total_symbol_days=30,
        snapshot_evidence=_snapshot_evidence())

    assert defects.data_excluded_symbol_days == 1
    assert defects.operational_failure_trades == 1
    assert defects.entered_trades == 1
    assert defects.data_ratio == 1 / 30
    assert defects.operational_ratio == 1


def test_precondition_failure_is_not_strategy_failure():
    decision = evaluate_development(
        primary_official=_stats(), primary_stress=_stats(),
        neighbor_official=[], defects=_defects(), contract=load_contract(),
        preconditions_ok=False)
    assert decision.development_gate == "NOT_RUN"
    assert decision.verdict == "NOT_RUN"


def test_oos_gate_is_pure_and_does_not_load_data():
    items = evaluate_oos(
        primary_official=_stats(trade_count=100, trading_days_with_trade=60),
        primary_stress=_stats(cost_level="stress", cumulative_net_pnl=0),
        neighbor_official=[_stats() for _ in range(6)],
        positive_months=4, defects=_defects(), contract=load_contract())
    assert len(items) == 11
    assert {item.name for item in items} == {f"O{i}" for i in range(1, 12)}


def test_detected_raw_defect_and_identity_failure_cannot_be_dropped_from_d9():
    day = date(2024, 3, 4)
    result = DaySimulationResult(
        day=day,
        spec_name="primary",
        trades=(),
        skipped=(),
        excluded=(("AAPL", "ohlc_invariant_violation"),),
        major_defects=(("AAPL", "ohlc_invariant_violation"),),
        open_positions_at_close=0,
    )
    defects = build_defect_metrics(
        day_results=[result],
        total_symbol_days=30,
        snapshot_evidence=_snapshot_evidence(
            identity=_identity(status="mismatch")),
    )
    assert defects.major_defect_count == 5
    decision = evaluate_development(
        primary_official=_stats(),
        primary_stress=_stats(cost_level="stress", cumulative_net_pnl=0),
        neighbor_official=[_stats() for _ in range(6)],
        defects=defects,
        contract=load_contract(),
    )
    assert decision.verdict == "INCONCLUSIVE_DATA"
    assert "D9" in decision.failed


@pytest.mark.parametrize(
    "defects",
    [
        _defects(conflict_total=29),
        _defects(operational_failure_trades=108, entered_trades=536),
        _defects(major_defect_count=1),
    ],
)
def test_o11_uses_the_complete_common_defect_policy(defects):
    items = evaluate_oos(
        primary_official=_stats(trade_count=100, trading_days_with_trade=60),
        primary_stress=_stats(cost_level="stress", cumulative_net_pnl=0),
        neighbor_official=[_stats() for _ in range(6)],
        positive_months=4,
        defects=defects,
        contract=load_contract(),
    )
    assert not next(item for item in items if item.name == "O11").passed


def test_oos_months_use_frozen_anchor_boundaries_and_non_primary_are_disqualified():
    months = oos_months(load_contract())
    assert months[0][0] == "M1"
    assert months[0][1].isoformat() == "2026-01-21"
    assert months[-1][2].isoformat() == "2026-07-21"
    assert disqualified_non_primary_specs(
        ["primary", "sensitivity", "rvol_1.3"]) == ("rvol_1.3", "sensitivity")
