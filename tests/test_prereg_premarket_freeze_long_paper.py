from __future__ import annotations

import json
import random
from decimal import ROUND_FLOOR, Decimal, localcontext
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


def _size(contract: dict, equity: float, reference_opens: list[float]) -> list[Decimal]:
    """Reference reading of portfolio.sizing_procedure for invariant checks (0 = skipped)."""
    portfolio = contract["portfolio"]
    step = Decimal(1).scaleb(-portfolio["quantity_decimals"])
    minimum = Decimal(str(portfolio["minimum_order_notional_usd"]))
    slip = contract["execution_costs"]["paper_slippage_bps_per_side"] / 10_000
    quantities = []
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_FLOOR
        cap = Decimal(str(portfolio["max_total_effective_exposure_pct"])) * Decimal(str(equity))
        used = Decimal(0)
        for index, reference_open in enumerate(reference_opens):
            per_share = Decimal(str(_cash_per_share(contract, reference_open)))
            budget = (cap - used) / (len(reference_opens) - index)
            qty = (budget / per_share).quantize(step, rounding=ROUND_FLOOR)
            if qty * Decimal(str(reference_open * (1 + slip))) < minimum:
                qty = Decimal(0)
            used += qty * per_share
            quantities.append(qty)
    return quantities


def test_contract_is_prospective_and_non_executable():
    contract = _contract()
    scope = contract["implementation_scope"]
    assert contract["schema"] == "premarket-freeze-long-paper/contract/v1"
    assert contract["version"] == "1.3"
    assert contract["issue"] == 163
    assert contract["strategy_template_id"] == "premarket_freeze_long_intraday_v1"
    assert contract["status"] == "PAPER_CONTRACT_LOCKED_PENDING_IMPLEMENTATION_REVIEW"
    assert contract["revision_history"][-1]["version"] == "1.3"
    rows = {row["version"]: row for row in contract["revision_history"]}
    assert rows["1.1"]["applied_before_any_paper_session"] is True
    assert rows["1.2"]["applied_before_any_paper_session"] is True
    # v1.3 was decided after the first v1.2 session: allowed only with the disclosure.
    assert rows["1.3"]["applied_before_any_paper_session"] is False
    assert "2026-10-01" in rows["1.3"]["outcome_dependence"]
    assert "after seeing" in rows["1.3"]["outcome_dependence"]
    assert "PAPER_V1_2_CLOSED_RECORD" in rows["1.3"]["outcome_dependence"]
    assert "fractional" in rows["1.3"]["reason"]
    assert scope["paper_strategy_code_in_this_change"] is True
    assert "fractional" in scope["paper_strategy_code_scope"]
    assert scope["runtime_config_activation_in_this_change"] is False
    assert scope["orders_created_in_this_change"] is False
    assert scope["live_broker_access_in_this_change"] is False
    assert scope["blind_oos_opened_in_this_change"] is False


def test_prior_observations_are_disclosed_without_claiming_clean_room():
    disclosure = _contract()["prior_access_disclosure"]
    assert disclosure["premarket_shadow_days_examined"] == [
        "2026-09-15", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-28", "2026-09-29",
        "2026-10-01",
    ]
    assert disclosure["postclose_outcomes_examined"] == [
        "2026-09-21", "2026-09-28", "2026-09-29", "2026-10-01"]
    assert disclosure["paper_sessions_examined"] == ["2026-10-01"]
    assert disclosure["parameter_tuning_from_those_outcomes"] is False
    v11 = next(row for row in _contract()["revision_history"] if row["version"] == "1.1")
    assert "META" in v11["sizing_change_trigger_disclosure"]
    assert "not clean-room" in disclosure["evidence_label"]


def test_long_positions_only_scope_and_decision_gates_name_artifact_paths():
    contract = _contract()
    direction = contract["direction_scope"]
    assert direction["long_watch"] == "PAPER_ELIGIBLE"
    assert direction["short_watch"] == "OBSERVATION_ONLY"
    assert "negative-position accounting" in direction["short_successor_requires"]
    gates = {gate["path"]: gate["rule"] for gate in contract["decision_input"]["gates"]}
    regime_rule = gates["snapshot.payload.input.market_regime.label"]
    assert "RISK_ON selects the LONG leg" in regime_rule
    assert "RISK_OFF selects the INVERSE leg" in regime_rule
    assert "SHORT_WATCH" in gates["brief.watchlist[i].bias"]
    assert "approved_short_strategy_templates" in gates["brief.watchlist[i].strategy_template_approved"]
    assert gates["snapshot.payload.input.provenance.run_mode"] == "== LIVE_WINDOW"
    assert gates["snapshot.payload.quality_defects"] == "== []"
    assert gates["brief.status"] == "== SHADOW_READY"
    assert gates["brief.watchlist[i].strategy_template_id"] == "== premarket_freeze_long_intraday_v1"


def test_inverse_mapping_is_limited_to_index_3x_etfs_and_is_a_long_position():
    contract = _contract()
    direction = contract["direction_scope"]
    assert direction["inverse_mapping"] == {"TQQQ": "SQQQ", "SOXL": "SOXS"}
    assert direction["index_3x_etfs_traded_as_long_watch"] == ["TQQQ", "SOXL"]
    assert "long position in an inverse ETF" in direction["inverse_leg"]
    assert "every other SHORT_WATCH item stays OBSERVATION_ONLY" in direction["inverse_leg"]
    assert direction["single_stock_leveraged_etfs"].startswith("never traded")
    assert "must be verified before any live review" in direction["leveraged_etp_disclosure"]
    assert "long positions only" in contract["decision_input"]["template_id_meaning"]
    assert "INVERSE leg" in contract["evaluation"]["leg_reporting"]
    assert "never approved as LONG performance" in contract["evaluation"]["leg_reporting"]


def test_leveraged_etfs_use_price_notional_so_risk_matches_sizing():
    contract = _contract()
    binding = contract["implementation_binding"]
    assert binding["risk_leverage_factor"] == 1
    assert "leverage_for" in binding["risk_leverage_rule"]
    assert "leverage factor 1" in contract["portfolio"]["leveraged_etp_exposure_basis"]
    assert binding["eod_flatten_minutes_before_close"] == 10
    assert "SELL-{n}" in binding["client_order_id"]
    assert "treated exactly as a missing bar" in binding["bar_validity"]
    assert "remainder stays open" in binding["exit_fill_quantity"]
    assert "allowed_symbols" in contract["direction_scope"]["long_leg_allowlist"]
    added = next(row for row in contract["revision_history"]
                 if row["version"] == "1.2")["rules_added_or_changed"]
    assert any("instead of truncation" in rule for rule in added)
    assert any("bar validity" in rule for rule in added)


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
        used = sum(float(q) * _cash_per_share(contract, o) for q, o in zip(quantities, opens))
        assert used <= cap_pct * equity + 1e-9
        assert all(q == q.quantize(Decimal("0.000001")) for q in quantities)


def test_sizing_procedure_text_is_pinned_to_the_reference_helper():
    # _size mirrors these exact steps; a wording change must update both.
    assert _contract()["portfolio"]["sizing_procedure"] == [
        "cap = 0.75 * E; used = 0",
        "eligible = gated candidates that have the 09:35 bar, in rank order (known at the 09:35 bar open)",
        ("for each eligible candidate with k eligible candidates remaining (including itself): "
         "room = cap - used; budget = room / k"),
        "qty = floor(budget / cash_per_share) to 6 decimal places",
        "if qty == 0 then NO_TRADE_SYMBOL with reason EXPOSURE_ROOM",
        ("if qty * entry_fill_price < minimum_order_notional_usd then NO_TRADE_SYMBOL "
         "with reason BELOW_MIN_ORDER_NOTIONAL"),
        "used += qty * cash_per_share",
    ]
    portfolio = _contract()["portfolio"]
    assert portfolio["whole_shares_only"] is False
    assert portfolio["quantity_decimals"] == 6
    assert portfolio["minimum_order_notional_usd"] == 1.0
    assert _contract()["implementation_binding"]["quantity_floor"] == (
        "decimal.Decimal on str() of the float inputs with every rounding toward floor, "
        "quantized to 6 decimal places; no epsilon")


def test_fractional_positions_close_inside_the_toss_fractional_window():
    contract = _contract()
    portfolio = contract["portfolio"]
    assert "close minus 60 minutes" in portfolio["fractional_execution_window"]
    assert "backstop" in portfolio["fractional_execution_window"]
    assert "unverified" in portfolio["fractional_leveraged_etp_eligibility"]
    # 09:35 entry + 60-minute time exit = 10:35 ET, before 15:00 (12:00 on early closes).
    entry_hour, entry_minute = (int(x) for x in contract["entry"]["entry_time_et"].split(":"))
    exit_minutes = entry_hour * 60 + entry_minute + contract["exits"]["time_exit_minutes"]
    assert exit_minutes == 10 * 60 + 35
    assert exit_minutes < 12 * 60


def test_implementation_binding_keeps_exits_unblocked_and_single_pnl_engine():
    contract = _contract()
    binding = contract["implementation_binding"]
    risk_config = binding["risk_config"]
    portfolio = contract["portfolio"]
    assert risk_config["max_portfolio_effective_pct"] == portfolio["max_total_effective_exposure_pct"]
    assert risk_config["max_symbol_notional_pct"] == portfolio["max_total_effective_exposure_pct"]
    assert risk_config["max_positions"] == portfolio["max_positions"]
    assert risk_config["daily_loss_limit_pct"] == contract["risk"]["daily_loss_halt_pct_of_prior_close_equity"]
    assert risk_config["max_drawdown_pct"] == contract["risk"]["high_water_drawdown_halt_pct"]
    assert risk_config["max_scale_ins"] == 0
    sessions = contract["evaluation"]["scheduled_trading_sessions"]
    assert risk_config["max_consecutive_losses"] > sessions * portfolio["max_positions"]
    assert "intent.price = reference_open" in binding["entry_risk_inputs"]
    kinds = binding["order_kinds"]
    assert {kinds[key] for key in ("stop_loss", "take_profit", "time_exit", "eod_flatten",
                                   "risk_halt_liquidation")} == {
        "STOP_LOSS", "TAKE_PROFIT", "TIME_EXIT", "EOD_FLATTEN", "EXIT"}
    from src.risk.models import RISK_REDUCING
    assert {kinds[key] for key in kinds if key != "rule"} <= RISK_REDUCING
    assert "net_pnl == gross_pnl - slippage - commission" in binding["fees_in_fills"]
    assert "never re-match lots" in binding["derived_ledgers"]


def test_sizing_example_from_observed_prices_buys_every_candidate():
    # 2026-10-01 09:35 opens of MSFT, AMZN, SPY: v1.2 bought 1, 1, 0 shares (~38% deployed).
    quantities = _size(_contract(), 2000.0, [517.56, 250.895, 764.0])
    assert all(q > 0 for q in quantities)
    used = sum(float(q) * _cash_per_share(_contract(), o)
               for q, o in zip(quantities, [517.56, 250.895, 764.0]))
    assert 1499.99 < used <= 1500.0


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
        "SQQQ",
        "SOXS",
        "롱 포지션",
    ):
        assert text in doc
