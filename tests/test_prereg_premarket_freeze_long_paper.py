from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "research" / "PREREG_PREMARKET_FREEZE_LONG_PAPER_V1.json"
DOC_PATH = ROOT / "docs" / "research" / "PREREG_PREMARKET_FREEZE_LONG_PAPER_V1.md"


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _cash_per_share(contract: dict, reference_open: float) -> float:
    costs = contract["execution_costs"]
    fill = reference_open * (1 + costs["paper_slippage_bps_per_side"] / 10_000)
    return fill * (1 + costs["paper_commission_bps_per_side"] / 10_000)


def _size(contract: dict, equity: float, reference_opens: list[float]) -> list[int]:
    """Reference reading of portfolio.sizing_procedure for invariant checks."""
    cap = contract["portfolio"]["max_total_effective_exposure_pct"] * equity
    used = 0.0
    quantities = []
    for index, reference_open in enumerate(reference_opens):
        per_share = _cash_per_share(contract, reference_open)
        room = cap - used
        qty = math.floor((room / (len(reference_opens) - index)) / per_share)
        if qty == 0 and per_share <= room:
            qty = 1
        used += qty * per_share
        quantities.append(qty)
    return quantities


def test_contract_is_prospective_and_non_executable():
    contract = _contract()
    scope = contract["implementation_scope"]
    assert contract["schema"] == "premarket-freeze-long-paper/contract/v1"
    assert contract["version"] == "1.1"
    assert contract["issue"] == 163
    assert contract["strategy_template_id"] == "premarket_freeze_long_intraday_v1"
    assert contract["status"] == "PAPER_CONTRACT_LOCKED_PENDING_IMPLEMENTATION_REVIEW"
    assert contract["revision_history"][-1]["applied_before_any_paper_session"] is True
    assert scope["paper_strategy_code_in_this_change"] is False
    assert scope["runtime_config_activation_in_this_change"] is False
    assert scope["orders_created_in_this_change"] is False
    assert scope["live_broker_access_in_this_change"] is False
    assert scope["blind_oos_opened_in_this_change"] is False


def test_prior_observations_are_disclosed_without_claiming_clean_room():
    disclosure = _contract()["prior_access_disclosure"]
    assert disclosure["premarket_shadow_days_examined"] == [
        "2026-09-15", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-28", "2026-09-29",
    ]
    assert disclosure["postclose_outcomes_examined"] == ["2026-09-21", "2026-09-28", "2026-09-29"]
    assert disclosure["parameter_tuning_from_those_outcomes"] is False
    assert "META" in _contract()["revision_history"][-1]["sizing_change_trigger_disclosure"]
    assert "not clean-room" in disclosure["evidence_label"]


def test_long_only_scope_and_decision_gates_name_artifact_paths():
    contract = _contract()
    direction = contract["direction_scope"]
    assert direction["long_watch"] == "PAPER_ELIGIBLE"
    assert direction["short_watch"] == "OBSERVATION_ONLY"
    assert "negative-position accounting" in direction["short_successor_requires"]
    gates = {gate["path"]: gate["rule"] for gate in contract["decision_input"]["gates"]}
    assert gates["snapshot.payload.input.market_regime.label"] == "== RISK_ON"
    assert gates["snapshot.payload.input.provenance.run_mode"] == "== LIVE_WINDOW"
    assert gates["snapshot.payload.quality_defects"] == "== []"
    assert gates["brief.status"] == "== SHADOW_READY"
    assert gates["brief.watchlist[i].strategy_template_id"] == "== premarket_freeze_long_intraday_v1"


def test_single_position_book_drives_sizing_and_risk():
    contract = _contract()
    assert contract["account"]["position_book"] == "RECORDED_PAPER_LEDGER"
    assert contract["risk"]["measured_on"] == "RECORDED_PAPER_LEDGER"
    assert contract["account"]["initial_equity"] == 2000.0


def test_exposure_cap_leaves_daily_risk_room_under_stress_cost():
    contract = _contract()
    portfolio = contract["portfolio"]
    exposure = portfolio["max_total_effective_exposure_pct"]
    stress = contract["execution_costs"]["stress_round_trip_total_pct_points"] / 100.0
    stop = contract["exits"]["stop_loss_pct_from_fill"]
    daily = contract["risk"]["daily_loss_halt_pct_of_prior_close_equity"]
    assert portfolio["per_symbol_weight_cap"] is None
    assert exposure == pytest.approx(0.75)
    assert portfolio["minimum_cash_reserve_pct"] == pytest.approx(1 - exposure)
    assert exposure * (stop + stress) < daily
    assert (exposure + 0.05) * (stop + stress) >= daily


def test_costs_follow_toss_fees_and_increment_to_official_and_stress():
    costs = _contract()["execution_costs"]
    paper = 2 * (costs["paper_slippage_bps_per_side"] + costs["paper_commission_bps_per_side"]) / 100
    assert costs["paper_commission_bps_per_side"] == 10.0
    assert costs["paper_sec_fee_pct_of_sell_notional"] == pytest.approx(0.00206)
    assert paper == pytest.approx(costs["paper_round_trip_total_pct_points"])
    assert costs["official_round_trip_total_pct_points"] == pytest.approx(0.34)
    assert costs["stress_round_trip_total_pct_points"] == pytest.approx(0.51)
    assert costs["official_incremental_pct_points_per_side"] == pytest.approx(
        (costs["official_round_trip_total_pct_points"] - paper) / 2)
    assert costs["stress_incremental_pct_points_per_side"] == pytest.approx(
        (costs["stress_round_trip_total_pct_points"] - paper) / 2)


def test_sizing_procedure_never_exceeds_exposure_cap():
    contract = _contract()
    cap_pct = contract["portfolio"]["max_total_effective_exposure_pct"]
    rng = random.Random(163)
    for _ in range(2000):
        equity = rng.uniform(1500, 2500)
        opens = [rng.uniform(5, 2500) for _ in range(rng.randint(1, 3))]
        quantities = _size(contract, equity, opens)
        used = sum(q * _cash_per_share(contract, o) for q, o in zip(quantities, opens))
        assert used <= cap_pct * equity + 1e-9


def test_sizing_example_from_observed_prices_buys_high_priced_names():
    # 2026-09-29 first regular-bar opens of META, MU, AVGO.
    assert _size(_contract(), 2000.0, [725.93, 1075.0, 356.99]) == [1, 0, 2]


def test_entry_exit_and_evaluation_gates_are_fully_fixed():
    contract = _contract()
    entry, exits, evaluation = contract["entry"], contract["exits"], contract["evaluation"]
    assert entry["fill"].startswith("open of the REGULAR 1m bar that starts exactly at 09:35:00 ET")
    assert entry["missing_entry_bar"] == "NO_TRADE_SYMBOL"
    assert entry["max_entries_per_symbol_per_day"] == 1
    assert entry["reentry_same_day"] is False
    assert exits["stop_loss_pct_from_fill"] == 0.0075
    assert exits["take_profit_pct_from_fill"] == 0.015
    assert exits["same_bar_stop_and_target"] == "STOP_FIRST_CONSERVATIVE"
    assert exits["time_exit_minutes"] == 60
    assert exits["time_exit_anchor"] == "start of the entry fill bar"
    assert "minus 10 minutes" in exits["eod_flatten_time"]
    assert exits["overnight"] is False
    assert evaluation["scheduled_trading_sessions"] == 10
    assert evaluation["automatic_live_promotion"] is False
    assert evaluation["minimum_completed_trades"] == 1
    assert [row["outcome"] for row in evaluation["outcome_precedence"]] == [
        "HALT_AND_REVIEW", "EXTEND_OBSERVATION", "NO_PROMOTION", "ELIGIBLE_FOR_LIVE_REVIEW",
    ]
    assert "official-cost and operating-cost cumulative account return > 0" in evaluation[
        "eligibility_conditions"]
    assert "INCONCLUSIVE_BENCHMARK" in evaluation["benchmark"]["spy_dividend_rule"]


def test_markdown_preserves_contract_boundaries():
    doc = DOC_PATH.read_text(encoding="utf-8")
    for text in (
        "clean-room 연구가 아니다",
        "SHORT_WATCH",
        "USD 2,000",
        "−0.75%",
        "+1.5%",
        "75%",
        "0.51%p",
        "실주문 승인이 아니며 자동 승격은 없다",
    ):
        assert text in doc
