"""Gap+RVOL Continuation 사전등록 계약 검증 — 이슈 #98.

가격·성과 DB는 읽지 않는다. 추적 문서/receipt/캘린더만 대조해 사전등록이
결과 전에 고정됐고 ORB를 이름만 바꾼 재탐색이 아님을 기계적으로 검증한다.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.history.exchange_calendar import trading_days
from src.history.oos_guard import TRACK_INTRADAY, blind_oos_window

REPO = Path(__file__).resolve().parents[1]
RESEARCH = REPO / "docs" / "research"
PREREG_MD = RESEARCH / "PREREG_P0_US30_GAP_RVOL_V1.md"
PREREG_JSON = RESEARCH / "PREREG_P0_US30_GAP_RVOL_V1.json"
DECISION = RESEARCH / "DECISION_DAYTRADING_GAP_RVOL_NEXT.md"
PLAN = RESEARCH / "PLAN_ONE_MONTH_STRATEGY_SELECTION_AI_SHADOW.md"
SNAPSHOT_RECEIPT = RESEARCH / "P0_US30_ORB_CLEAN_SNAPSHOT_RECEIPT.json"
MANIFEST = RESEARCH / "P0_US30_ORB_CLEAN_1M_MANIFEST.json"
ORB_PREREG = RESEARCH / "PREREG_P0_US30_ORB_V1.json"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def contract() -> dict:
    return _json(PREREG_JSON)


@pytest.fixture(scope="module")
def doc() -> str:
    return _read(PREREG_MD)


def test_artifacts_exist_and_declare_pre_result_scope(contract, doc):
    assert DECISION.is_file() and PLAN.is_file()
    assert contract["schema"] == "prereg-gap-rvol/contract/v1"
    assert contract["version"] == "1.0"
    assert contract["issue"] == 98
    assert contract["results_examined"] is False
    assert contract["implementation_scope"] == {
        "strategy_code_in_this_change": False,
        "development_run_in_this_change": False,
        "forward_returns_in_this_change": False,
        "blind_oos_opened_in_this_change": False,
        "next_step": "separate issue and branch after this prereg merges",
    }
    assert "Gap+RVOL standalone 결과 미열람" in doc


def test_prior_access_is_disclosed_without_claiming_clean_room(contract, doc):
    p = contract["prior_access_disclosure"]
    assert p["orb_development_result_examined"] is True
    assert p["gap_rvol_standalone_results_examined"] is False
    assert p["gap_rvol_signals_or_returns_computed"] is False
    assert p["evidence_label"].startswith("post-ORB disclosed candidate-2")
    assert p["strategy_order_bias_claimed_zero"] is False
    assert "ORB development 결과" in doc
    assert "Gap+RVOL standalone" in doc
    assert "clean-room" in doc


def test_fixed_hashes_match_snapshot_receipt_and_document(contract, doc):
    rec = _json(SNAPSHOT_RECEIPT)
    ident = contract["data_identity"]
    assert ident["snapshot_basename"] == rec["snapshot"]["basename"]
    assert ident["snapshot_file_sha256"] == rec["snapshot"]["file_sha256"]
    assert ident["snapshot_content_sha256"] == rec["snapshot"]["content_sha256"]
    assert ident["raw_content_sha256"] == rec["raw_content_sha256"]
    assert ident["as_of"] == rec["contract"]["as_of"]
    assert ident["collection_run_id"] == rec["contract"]["collection_run_id"]
    assert sorted(ident["allowed_sessions"]) == sorted(rec["contract"]["allowed_sessions"])
    for key in (
        "snapshot_file_sha256",
        "snapshot_content_sha256",
        "manifest_data_sha256",
        "raw_content_sha256",
    ):
        assert ident[key] in doc


def test_manifest_hash_and_interval_match(contract):
    manifest = _json(MANIFEST)
    ident = contract["data_identity"]
    assert ident["manifest_data_sha256"] == manifest["data_content_sha256"]
    assert ident["source_interval"] == manifest["interval"] == "1m"
    assert ident["derived_interval"] == "5m"


def test_universe_matches_snapshot_receipt(contract):
    rec = _json(SNAPSHOT_RECEIPT)
    u = contract["universe"]
    symbols = (
        u["market_etf"] + u["sector_etf"] + u["large_cap"] + u["leveraged_inverse"]
    )
    assert len(symbols) == len(set(symbols)) == 30
    assert sorted(symbols) == sorted(rec["contract"]["symbols"])
    for family in u["exposure_families"].values():
        assert set(family) <= set(symbols)


def test_blind_oos_window_matches_code_gate(contract):
    lo, hi = blind_oos_window(TRACK_INTRADAY)
    w = contract["windows"]
    assert (w["blind_oos_start"], w["blind_oos_end"]) == (
        lo.isoformat(),
        hi.isoformat(),
    )
    assert w["blind_oos_trading_days"] == len(trading_days(lo, hi)) == 125


def test_development_window_and_folds_tile_calendar_exactly(contract):
    w = contract["windows"]
    eval_days = trading_days(
        date.fromisoformat(w["development_eval_start"]),
        date.fromisoformat(w["development_eval_end"]),
    )
    assert len(eval_days) == w["development_eval_trading_days"] == 670
    seen: list[date] = []
    folds = contract["folds"]["boundaries"]
    assert [f["fold"] for f in folds] == [f"F{i:02d}" for i in range(1, 11)]
    assert [f["trading_days"] for f in folds] == [103] + [63] * 9
    for fold in folds:
        days = trading_days(
            date.fromisoformat(fold["start"]),
            date.fromisoformat(fold["end"]),
        )
        assert len(days) == fold["trading_days"]
        seen.extend(days)
    assert seen == eval_days
    assert contract["folds"]["development_may_use_oos_bars"] is False
    assert contract["folds"]["oos_warmup_may_use_pre_oos_bars"] is True


def test_primary_thresholds_are_exact_and_documented(contract, doc):
    f = contract["features"]
    assert f["gap"]["direction"] == "up_only"
    assert f["gap"]["threshold_gte"] == 0.003
    assert f["gap"]["uses_absolute_value"] is False
    assert f["rvol"]["window_minutes"] == 15
    assert f["rvol"]["threshold_gte"] == 1.5
    assert f["rvol"]["lookback_trading_days"] == 20
    assert f["rvol"]["min_valid_lookback_days"] == 15
    assert f["rvol"]["today_excluded_from_denominator"] is True
    assert f["rvol"]["future_volume_forbidden"] is True
    assert "**gap ≥ +0.30%**" in doc
    assert "**RVOL ≥ 1.5**" in doc


def test_vwap_is_causal_and_regular_only(contract):
    v = contract["features"]["vwap"]
    assert v["causal"] is True
    assert v["premarket_included"] is False
    assert v["postmarket_included"] is False
    assert v["zero_volume"] == "data_exclusion"
    assert "through current completed 5m bar" in v["definition"]


def test_signal_state_machine_is_fully_pinned(contract):
    s = contract["signal"]
    assert s["decision_bar_minutes"] == 5
    assert s["decision_bar_min_bars"] == 5
    assert s["closed_only"] is True
    assert s["observation_start_et"] == "09:45"
    assert s["retention_fraction"] == 0.5
    assert s["retention_equality_holds"] is True
    assert s["retention_break_invalidates_day"] is True
    assert s["armed_and_trigger_same_bar"] is False
    assert s["pullback_low"].endswith("inclusive")
    assert s["trigger"] == {
        "close_gt_previous_bar_high": True,
        "close_gt_causal_session_vwap": True,
        "close_gt_open": True,
        "close_gte_retention_floor": True,
        "strict_comparisons": [
            "previous_bar_high",
            "causal_session_vwap",
            "bar_open",
        ],
    }


def test_signal_fill_boundaries_and_one_trade_rule_are_pinned(contract, doc):
    s = contract["signal"]
    assert s["fill"] == "next completed 5m bar open"
    assert s["signal_bar_fill"] is False
    assert s["latest_signal_bar_start_et"] == "10:25"
    assert s["latest_fill_bar_start_et"] == "10:30"
    assert s["latest_fill_boundary_inclusive"] is True
    assert s["missing_next_scheduled_bar"] == "signal_expired"
    assert s["capacity_skipped_signal_requeued"] is False
    assert s["max_trades_per_symbol_per_day"] == 1
    assert s["same_day_reentry"] is False
    assert "**10:30 ET 이하**" in doc


def test_strategy_is_structurally_independent_from_orb(contract, doc):
    independent = contract["independence"]
    assert independent["orb_oos_must_remain_closed"] is True
    assert set(independent["does_not_share_with_orb"]) == {
        "entry_event",
        "stop_level",
        "time_stop",
    }
    executable_contract = json.dumps(
        {
            "features": contract["features"],
            "signal": contract["signal"],
            "risk": contract["risk"],
        },
        sort_keys=True,
    ).lower()
    for forbidden in independent["forbidden_entry_or_risk_inputs"]:
        assert forbidden not in executable_contract
    assert "OR high/low 및 OR breakout은 보지 않는다" in doc


def test_risk_and_exit_semantics_are_pinned(contract, doc):
    r = contract["risk"]
    assert r["one_r"] == "entry - pullback_low"
    assert r["stop"] == "pullback_low"
    assert r["target_r_multiple"] == 2.0
    assert r["risk_budget_pct_of_equity"] == 0.01
    assert r["time_stop_minutes"] == 60
    assert r["time_stop_at"] == "entry bar start timestamp + 60 scheduled minutes"
    assert r["time_stop_fill"] == (
        "first scheduled REGULAR 5m bar open at or after time_stop_at"
    )
    assert r["missing_bars_do_not_extend_time_stop"] is True
    assert r["eod_flatten_minutes_before_close"] == 10
    assert r["overnight"] is False
    assert r["stale_bars_to_force_exit"] == 3
    assert r["entry_bar_is_scanned_for_exits"] is True
    assert r["averaging_down"] is False and r["scale_in_max"] == 0
    assert r["open_exit_priority"] == [
        "gap_through_stop",
        "gap_through_target",
        "stale",
        "eod",
        "time",
    ]
    assert r["intrabar_exit_priority"] == ["stop_touch", "target_touch"]
    assert r["eod_wins_time_tie"] is True
    assert "**60분 time stop**" in doc


def test_portfolio_ranking_and_caps_are_pinned(contract):
    p = contract["portfolio"]
    assert p["rank_keys"] == [
        {"key": "rvol", "order": "desc"},
        {"key": "gap", "order": "desc"},
        {"key": "symbol", "order": "asc"},
    ]
    assert p["max_positions"] == 2
    assert p["max_portfolio_effective_pct"] == 0.5
    assert p["max_symbol_effective_pct"] == 0.3
    assert p["max_symbol_notional_pct"] == 0.2
    assert p["max_positions_per_exposure_family"] == 1
    assert p["same_bar_fill_order"] == ["exits", "entries"]
    assert p["family_cap_includes_pending_same_bar_picks"] is True


def test_costs_are_identical_to_frozen_common_contract(contract, doc):
    c = contract["costs"]
    assert c["round_trip_pct_points"] == {
        "low": 0.14,
        "official": 0.34,
        "stress": 0.68,
    }
    assert c["allocation"] == {"entry": 0.5, "exit": 0.5}
    assert c["additional_fee_on_top"] is False
    assert c["official_level"] == "official"
    for value in ("0.14", "0.34", "0.68"):
        assert value in doc


def test_development_and_oos_economic_gates_are_not_looser_than_orb(contract):
    orb = _json(ORB_PREREG)
    dev = contract["development_gate"]
    for key in (
        "D1_min_trades",
        "D2_min_trading_days_with_trade",
        "D3_net_expectancy_gt",
        "D4_profit_factor_gte",
        "D5_abs_mdd_lte",
        "D6_min_positive_folds",
        "D6_total_folds",
        "D7_stress_cumulative_pnl_gte",
        "D8_parameter_cliff",
    ):
        assert dev[key] == orb["development_gate"][key]
    oos = contract["oos_gate"]
    assert oos["O1_net_expectancy_gt"] == orb["oos_gate"]["O1_net_expectancy_gt"]
    assert oos["O2_profit_factor_gte"] == orb["oos_gate"]["O2_profit_factor_gte"]
    assert oos["O3_min_trading_days_with_trade"] == orb["oos_gate"]["O3_min_trading_days_with_trade"]
    assert oos["O4_min_trades"] == orb["oos_gate"]["O4_min_trades"]
    assert oos["O5_min_positive_months"] == orb["oos_gate"]["O5_min_positive_months"]
    assert oos["O6_abs_mdd_lte"] == orb["oos_gate"]["O6_abs_mdd_lte"]
    assert oos["O7_stress_cumulative_pnl_gte"] == orb["oos_gate"]["O7_stress_cumulative_pnl_gte"]
    assert oos["O9_max_single_day_profit_share"] == orb["oos_gate"]["O9_max_single_day_profit_share"]
    assert oos["O11_major_data_defects"] == orb["oos_gate"]["O11_major_data_defects"]


def test_development_sample_gates_are_rate_consistent_with_oos(contract):
    g = contract["development_gate"]
    o = contract["oos_gate"]
    w = contract["windows"]
    scale = w["development_eval_trading_days"] / w["blind_oos_trading_days"]
    assert g["D1_min_trades"] >= o["O4_min_trades"] * scale
    assert g["D2_min_trading_days_with_trade"] >= o["O3_min_trading_days_with_trade"] * scale


def test_development_failure_verdict_separates_performance_from_defects(contract):
    g = contract["development_gate"]
    performance = g["economic_or_statistical_failure"]
    defect = g["data_or_operational_defect_failure"]
    assert performance["applies_to"] == [f"D{i}" for i in range(1, 9)]
    assert performance["verdict"] == "NO_SELECTION"
    assert performance["next_candidate_allowed"] == "VWAP_ATR_MEAN_REVERSION"
    assert defect["applies_to"] == ["D9"]
    assert defect["verdict"] == "INCONCLUSIVE_DATA"
    assert defect["repair_requires_independent_review"] is True
    assert defect["next_candidate_allowed_before_repair"] is None
    assert performance["open_blind_oos"] is defect["open_blind_oos"] is False


def test_oos_anchor_months_match_orb_preregistration(contract):
    orb = _json(ORB_PREREG)
    assert contract["oos_gate"]["O5_month_boundaries"] == orb["oos_gate"][
        "O5_month_boundaries"
    ]


def test_parameter_neighbors_are_diagnostic_only_and_symmetric(contract):
    n = contract["parameter_cliff_neighborhood"]
    assert n["primary_only_decides"] is True
    assert n["diagnostics_may_rescue_primary"] is False
    assert n["cliff_if_opposite_sign_variants_gte"] == 2
    assert n["current_oos_ineligible"] is True
    axes = {v["axis"]: v for v in n["variants"]}
    assert axes == {
        "gap_threshold_gte": {
            "axis": "gap_threshold_gte",
            "primary": 0.003,
            "neighbors": [0.002, 0.004],
        },
        "rvol_threshold_gte": {
            "axis": "rvol_threshold_gte",
            "primary": 1.5,
            "neighbors": [1.3, 1.7],
        },
        "retention_fraction": {
            "axis": "retention_fraction",
            "primary": 0.5,
            "neighbors": [0.4, 0.6],
        },
        "target_r_multiple": {
            "axis": "target_r_multiple",
            "primary": 2.0,
            "neighbors": [1.5, 2.5],
        },
    }


def test_defect_policy_keeps_strategy_filters_out_of_data_numerator(contract):
    d = contract["defect_policy"]
    assert d["data_exclusion_ratio_lte"] == 0.05
    assert d["operational_failure_ratio_lte"] == 0.2
    assert d["major_defects_must_equal"] == 0
    assert set(d["data_exclusion_reasons"]).isdisjoint(d["strategy_filters_not_defects"])
    assert {"gap_below_threshold", "rvol_below_threshold"} <= set(
        d["strategy_filters_not_defects"]
    )


def test_blind_loader_and_unlock_remain_fail_closed(contract):
    b = contract["blind_enforcement"]
    assert b["loader"] == "src.history.oos_guard.research_load"
    assert b["track"] == "intraday"
    assert b["development_purpose"] == "performance"
    assert b["direct_load_candles_forbidden"] is True
    assert b["coverage_purpose_forbidden_in_performance_code"] is True
    assert b["unlock_verifier_required_before_oos"] == "#78"
    assert b["unlock_artifact_created_in_this_change"] is False
    assert b["bypass_is_invalidation_grade"] is True


def test_process_preconditions_cannot_masquerade_as_strategy_failure(contract):
    p = contract["preconditions_not_result_criteria"]
    assert set(p) == {
        "P1_prereg_merge_commit",
        "P2_implementation_commit_and_map",
        "P3_independent_reviews",
        "P4_snapshot_identity",
        "P5_loader_boundary",
        "unmet_verdict",
    }
    assert p["unmet_verdict"] == "NOT_RUN"
    assert contract["verdict_definitions"]["pick_least_bad"] is False


def test_plan_revision_records_orb_stop_and_gap_rvol_next():
    plan = _read(PLAN)
    assert "| r3 | 2026-07-30 | #98 |" in plan
    assert "ORB는 development-only `INCONCLUSIVE_DATA`로 중단" in plan
    assert "Gap + RVOL Continuation 사전등록" in plan
    assert "VWAP/ATR Mean Reversion" in plan
    assert "r1의 일괄 토너먼트 순서를 대체" in plan
    assert "각 후보 blind OOS 이전 해당 후보 prereg·구현·독립 리뷰 커밋" in plan


def test_no_gap_rvol_result_implementation_or_unlock_artifact_exists():
    forbidden = [
        RESEARCH / "RESULT_P0_US30_GAP_RVOL_DEVELOPMENT.md",
        RESEARCH / "RESULT_P0_US30_GAP_RVOL_DEVELOPMENT.json",
        RESEARCH / "OOS_UNLOCK_P0_US30_GAP_RVOL.json",
        REPO / "src" / "research" / "gap_rvol",
        REPO / "analysis" / "run_gap_rvol_development.py",
    ]
    assert [str(path) for path in forbidden if path.exists()] == []
