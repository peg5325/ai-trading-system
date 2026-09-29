from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "research" / "PREREG_PREMARKET_FREEZE_LONG_PAPER_V1.json"
DOC_PATH = ROOT / "docs" / "research" / "PREREG_PREMARKET_FREEZE_LONG_PAPER_V1.md"


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_contract_is_prospective_and_non_executable():
    contract = _contract()
    scope = contract["implementation_scope"]
    assert contract["schema"] == "premarket-freeze-long-paper/contract/v1"
    assert contract["issue"] == 163
    assert contract["strategy_template_id"] == "premarket_freeze_long_intraday_v1"
    assert contract["status"] == "PAPER_CONTRACT_LOCKED_PENDING_IMPLEMENTATION_REVIEW"
    assert scope["paper_strategy_code_in_this_change"] is False
    assert scope["runtime_config_activation_in_this_change"] is False
    assert scope["orders_created_in_this_change"] is False
    assert scope["live_broker_access_in_this_change"] is False
    assert scope["blind_oos_opened_in_this_change"] is False


def test_prior_observations_are_disclosed_without_claiming_clean_room():
    contract = _contract()
    disclosure = contract["prior_access_disclosure"]
    assert disclosure["premarket_shadow_days_examined"] == ["2026-09-21", "2026-09-28"]
    assert disclosure["postclose_outcomes_examined"] == ["2026-09-21", "2026-09-28"]
    assert disclosure["parameter_tuning_from_those_outcomes"] is False
    assert "not clean-room" in disclosure["evidence_label"]


def test_long_only_scope_matches_current_accounting_boundary():
    contract = _contract()
    direction = contract["direction_scope"]
    assert direction["long_watch"] == "PAPER_ELIGIBLE"
    assert direction["short_watch"] == "OBSERVATION_ONLY"
    assert "negative-position accounting" in direction["short_successor_requires"]
    assert contract["decision_input"]["required_regime"] == "RISK_ON"


def test_portfolio_and_cost_math_leave_daily_risk_room():
    contract = _contract()
    portfolio = contract["portfolio"]
    costs = contract["execution_costs"]
    exits = contract["exits"]
    max_positions = portfolio["max_positions"]
    position_pct = portfolio["position_notional_pct_of_entry_equity"]
    max_exposure = max_positions * position_pct
    worst_stop_drag = max_exposure * exits["stop_loss_pct_from_fill"]
    stress_cost_drag = max_exposure * costs["stress_round_trip_total_pct_points"] / 100.0
    assert max_exposure == pytest.approx(portfolio["max_total_effective_exposure_pct"])
    assert max_exposure == pytest.approx(0.60)
    assert worst_stop_drag + stress_cost_drag < contract["risk"]["daily_loss_halt_pct_of_prior_close_equity"]
    assert costs["official_and_stress_reporting"].startswith("subtract the per-side incremental")
    assert costs["official_incremental_pct_points_per_side"] == pytest.approx(0.10)
    assert costs["stress_incremental_pct_points_per_side"] == pytest.approx(0.27)


def test_entry_exit_and_evaluation_gates_are_fully_fixed():
    contract = _contract()
    assert contract["entry"] == {
        "session": "REGULAR",
        "decision_already_frozen_before_open": True,
        "entry_time_et": "09:35",
        "fill": "first available REGULAR 1m bar open at or after 09:35 ET",
        "missing_entry_bar": "NO_TRADE_SYMBOL",
        "order_type": "PAPER_MARKET_WITH_REFERENCE_PRICE",
        "allocation_notional": "entry-time marked account equity * 0.20",
        "anticipated_entry_fill": "reference_open * (1 + 0.0005 entry slippage)",
        "cash_per_share_for_sizing": "anticipated_entry_fill * (1 + 0.0002 entry fee)",
        "quantity": "floor(min(allocation_notional, available_cash) / cash_per_share_for_sizing)",
        "quantity_zero": "NO_TRADE_SYMBOL",
        "cash_check_includes_entry_slippage_and_fee": True,
        "max_entries_per_symbol_per_day": 1,
        "reentry_same_day": False,
        "averaging_down": False,
        "scale_in": False,
    }
    assert contract["exits"]["stop_loss_pct_from_fill"] == 0.0075
    assert contract["exits"]["take_profit_pct_from_fill"] == 0.015
    assert contract["exits"]["stop_trigger"] == "completed REGULAR 1m bar low <= stop_price"
    assert contract["exits"]["target_trigger"] == "completed REGULAR 1m bar high >= target_price"
    assert contract["exits"]["same_bar_stop_and_target"] == "STOP_FIRST_CONSERVATIVE"
    assert contract["exits"]["time_exit_minutes"] == 60
    assert contract["exits"]["overnight"] is False
    assert contract["evaluation"]["scheduled_trading_sessions"] == 10
    assert contract["evaluation"]["automatic_live_promotion"] is False
    assert contract["evaluation"]["minimum_completed_trades"] == 1
    assert contract["risk"]["mark_frequency"] == "every completed REGULAR 1m bar and every fill"
    assert contract["risk"]["breach_liquidation_fill"].startswith("next available REGULAR 1m bar open")


def test_markdown_preserves_contract_boundaries():
    doc = DOC_PATH.read_text(encoding="utf-8")
    for text in (
        "clean-room 연구가 아니다",
        "SHORT_WATCH",
        "USD 2,000",
        "−0.75%",
        "+1.5%",
        "미래 10거래일",
        "실주문 승인이 아니며 자동 승격은 없다",
    ):
        assert text in doc
